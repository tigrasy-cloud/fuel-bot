"""Асинхронный клиент к народной карте АЗС gdebenz.ru.

Использует те же публичные JSON-эндпоинты, что и фронтенд сайта:
  GET /api/cities?q=...            — поиск города
  GET /api/stations?lat1&lon1&lat2&lon2 — АЗС в прямоугольнике (bbox)
  GET /api/nearby?lat&lon&radius_km&full=1 — ближайшие АЗС к точке
Сервер требует заголовок Referer (проверено опытным путём).
"""

from __future__ import annotations

import logging

import httpx

from ..models import City, Station

log = logging.getLogger(__name__)

API_BASE = "https://gdebenz.ru"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://gdebenz.ru/",
    "Accept": "application/json",
}


class GdebenzError(Exception):
    pass


class GdebenzClient:
    def __init__(self, timeout: float = 12.0) -> None:
        self._client = httpx.AsyncClient(
            base_url=API_BASE,
            headers=_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _get_json(self, path: str, params: dict) -> object:
        try:
            resp = await self._client.get(path, params=params)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("gdebenz request failed: %s %s -> %s", path, params, exc)
            raise GdebenzError(str(exc)) from exc

    # ---------- города ----------

    async def search_cities(self, query: str) -> list[City]:
        data = await self._get_json("/api/cities", {"q": query})
        results = (data or {}).get("results") or []
        cities = []
        for item in results[:8]:
            try:
                cities.append(
                    City(
                        name=str(item["name"]),
                        sub=str(item.get("sub", "")),
                        lat=float(item["lat"]),
                        lon=float(item["lon"]),
                        zoom=int(item.get("zoom", 13)),
                    )
                )
            except (KeyError, TypeError, ValueError):
                continue
        return cities

    # ---------- станции ----------

    @staticmethod
    def _parse_station(raw: dict) -> Station | None:
        try:
            prices_raw = raw.get("prices_now") or {}
            prices: dict[str, float] = {}
            for fuel, info in prices_raw.items():
                try:
                    prices[str(fuel)] = float(info["p"])
                except (KeyError, TypeError, ValueError):
                    continue
            fuels_now = [
                f.strip() for f in str(raw.get("fuels_now") or "").split(",") if f.strip()
            ]
            return Station(
                osm_id=str(raw["osm_id"]),
                name=str(raw.get("name") or ""),
                brand=str(raw.get("brand") or ""),
                lat=float(raw["lat"]),
                lon=float(raw["lon"]),
                addr=str(raw.get("addr") or ""),
                status=str(raw.get("status") or "none"),
                fuels_now=fuels_now,
                prices=prices,
                distance_km=(
                    round(float(raw["distance_km"]), 1)
                    if raw.get("distance_km") is not None
                    else None
                ),
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def stations_bbox(
        self, lat1: float, lon1: float, lat2: float, lon2: float
    ) -> list[Station]:
        """АЗС в прямоугольнике (юг/запад/север/восток)."""
        data = await self._get_json(
            "/api/stations",
            {"lat1": f"{lat1:.4f}", "lon1": f"{lon1:.4f}",
             "lat2": f"{lat2:.4f}", "lon2": f"{lon2:.4f}"},
        )
        if not isinstance(data, list):
            return []
        return [s for s in (self._parse_station(x) for x in data) if s]

    async def nearby(self, lat: float, lon: float, radius_km: int = 20) -> list[Station]:
        """Ближайшие АЗС к точке. Сервер сам округляет координаты до сетки 0.05."""
        grid = lambda v: f"{round(20 * v) / 20:.2f}"  # noqa: E731
        data = await self._get_json(
            "/api/nearby",
            {"lat": grid(lat), "lon": grid(lon), "radius_km": radius_km, "full": 1},
        )
        raw_stations = (data or {}).get("stations") if isinstance(data, dict) else None
        if not raw_stations:
            return []
        return [s for s in (self._parse_station(x) for x in raw_stations) if s]
