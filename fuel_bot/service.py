"""Сервисный слой: кэш, фильтрация и агрегация станций."""

from __future__ import annotations

import math
import time
from collections import Counter

from .models import GOOD_STATUSES, STATUS_META, Station
from .sources.gdebenz import GdebenzClient
from .sources.russiabase import RussiaBaseClient, RussiaBaseError
from .sources.yandex_maps import YandexMapsClient, YandexMapsError

CACHE_TTL_SEC = 90  # статусы живут недолго, но ддосить источник нельзя
YANDEX_MATCH_KM = 0.15  # радиус склейки станций Яндекса и gdebenz
RB_MATCH_KM = 0.15  # радиус склейки станций RussiaBase и gdebenz


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


class FuelService:
    def __init__(self, client: GdebenzClient | None = None) -> None:
        self.client = client or GdebenzClient()
        self.yandex = YandexMapsClient()
        self.russiabase = RussiaBaseClient()
        self._cache: dict[str, tuple[float, list[Station]]] = {}

    async def close(self) -> None:
        await self.client.close()
        await self.yandex.close()
        await self.russiabase.close()

    def _cached(self, key: str) -> list[Station] | None:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < CACHE_TTL_SEC:
            return hit[1]
        return None

    def _store(self, key: str, stations: list[Station]) -> None:
        self._cache[key] = (time.monotonic(), stations)

    async def stations_for_city(
        self, lat: float, lon: float, span: float = 0.18, force: bool = False
    ) -> list[Station]:
        """Станции в квадрате вокруг центра города (span — половина стороны в градусах)."""
        key = f"bbox:{lat:.3f}:{lon:.3f}:{span}"
        if not force:
            cached = self._cached(key)
            if cached is not None:
                return cached
        stations = await self.client.stations_bbox(
            lat - span, lon - span * 1.4, lat + span, lon + span * 1.4
        )
        for s in stations:
            s.distance_km = round(haversine_km(lat, lon, s.lat, s.lon), 1)
        stations.sort(key=lambda s: s.distance_km or 999)
        self._store(key, stations)
        return stations

    async def stations_near(
        self, lat: float, lon: float, radius_km: int = 20, force: bool = False
    ) -> list[Station]:
        key = f"near:{lat:.2f}:{lon:.2f}:{radius_km}"
        if not force:
            cached = self._cached(key)
            if cached is not None:
                return cached
        stations = await self.client.nearby(lat, lon, radius_km)
        stations.sort(key=lambda s: s.distance_km if s.distance_km is not None else 999)
        self._store(key, stations)
        return stations


def filter_stations(
    stations: list[Station], status: str = "all", fuel: str = "any"
) -> list[Station]:
    """status: all/ok/no/queue; fuel: any/92/95/98/ДТ ..."""
    out = stations
    if status == "ok":
        out = [s for s in out if s.status in GOOD_STATUSES]
    elif status == "no":
        out = [s for s in out if s.status == "no"]
    elif status == "queue":
        out = [s for s in out if s.status == "queue"]
    if fuel != "any":
        fuel_l = fuel.lower()

        def has_fuel(s: Station) -> bool:
            fuels = {f.lower() for f in s.fuels_now}
            prices = {k.lower() for k in s.prices}
            return fuel_l in fuels or fuel_l in prices

        out = [s for s in out if has_fuel(s)]
    return out


def summary_counts(stations: list[Station]) -> str:
    """Сводка по статусам: 🟢 12 · 🟠 3 · 🔴 5 · ⚪ 40"""
    counter = Counter(s.status for s in stations)
    order = ["yes", "queue", "low", "dtonly", "no", "none", "unknown"]
    parts = []
    for st in order:
        n = counter.get(st, 0)
        if n:
            emoji = STATUS_META.get(st, ("⚪", st))[0]
            parts.append(f"{emoji} {n}")
    return " · ".join(parts) if parts else "нет данных"


def apply_reports(
    stations: list[Station],
    reports: dict[str, tuple[str, float, int]],
) -> None:
    """Накладывает свежие отметки пользователей бота поверх статусов источника."""
    for s in stations:
        hit = reports.get(s.osm_id)
        if hit:
            status, _ts, votes = hit
            s.status = status
            s.local = True
            s.local_votes = votes


async def enrich_with_yandex(
    service: "FuelService",
    stations: list[Station],
    city: str,
    center_lat: float,
    center_lon: float,
) -> None:
    """Обогащает список данными Яндекса («АЗС с топливом»).

    Совпавшие станции получают флаг yandex_fuel, несовпавшие добавляются
    в конец списка как станции со статусом «есть топливо».
    При любой ошибке источника просто ничего не делает.
    """
    try:
        items = await service.yandex.fuel_stations(city)
    except YandexMapsError:
        return
    for item in items:
        best: Station | None = None
        best_d = YANDEX_MATCH_KM
        for s in stations:
            d = haversine_km(item["lat"], item["lon"], s.lat, s.lon)
            if d < best_d:
                best = s
                best_d = d
        if best is not None:
            best.yandex_fuel = True
        else:
            stations.append(Station(
                osm_id=f"ym_{item['id']}",
                name=item["title"],
                brand=item["title"],
                lat=item["lat"],
                lon=item["lon"],
                addr=item["address"],
                status="yes",
                distance_km=round(
                    haversine_km(center_lat, center_lon, item["lat"], item["lon"]), 1
                ),
                yandex_fuel=True,
            ))


async def enrich_with_russiabase(
    service: "FuelService",
    stations: list[Station],
    city: str,
    center_lat: float,
    center_lon: float,
) -> None:
    """Обогащает список данными RussiaBase (цены и наличие топлива).

    Совпавшие станции получают флаг rb_fuel, недостающие цены и марки
    топлива; несовпавшие добавляются в конец списка. Статус gdebenz
    не понижаем — он свежее (народные отметки). При любой ошибке
    источника просто ничего не делает.
    """
    try:
        items = await service.russiabase.stations(city)
    except RussiaBaseError:
        return
    for item in items:
        best: Station | None = None
        best_d = RB_MATCH_KM
        for s in stations:
            d = haversine_km(item["lat"], item["lon"], s.lat, s.lon)
            if d < best_d:
                best = s
                best_d = d
        if best is not None:
            best.rb_fuel = True
            for fuel, price in item["prices"].items():
                best.prices.setdefault(fuel, price)
            for fuel in item["fuels_ok"]:
                if fuel not in best.fuels_now:
                    best.fuels_now.append(fuel)
            if best.status in ("none", "unknown") and item["has_fuel"]:
                best.status = "yes"
        else:
            stations.append(Station(
                osm_id=f"rb_{item['poiid']}",
                name=item["name"],
                brand=item["brand"] or item["name"],
                lat=item["lat"],
                lon=item["lon"],
                addr=item["address"],
                status="yes" if item["has_fuel"] else "unknown",
                fuels_now=list(item["fuels_ok"]),
                prices=dict(item["prices"]),
                distance_km=round(
                    haversine_km(center_lat, center_lon, item["lat"], item["lon"]), 1
                ),
                rb_fuel=True,
            ))
