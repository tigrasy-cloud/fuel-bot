"""Третий источник: russiabase.ru (АЗС-Локатор) — цены и наличие топлива.

Сайт серверно рендерит страницу /prices?city=<id> со списком станций
(JSON в __NEXT_DATA__), а справочники отдаёт открытым API
/api/backend/data/{regions,cities,azsBrands}. Капча включается только
для подозрительных клиентов — аккуратно кэшируем и не ддосим.

Даёт по каждой станции: координаты, цены по всем маркам (ai92…ai100,
dt, gas, propan) и статусы наличия (available / possibly_available /
has_limit / unknown) из поля restrictions.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
_BASE = "https://russiabase.ru"
_API = _BASE + "/api/backend/data/"
_PRICES_URL = _BASE + "/prices"
_NEXT_RE = re.compile(
    r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', re.S
)

_CACHE_TTL = 15 * 60            # станции города — 15 минут
_DICT_TTL = 30 * 24 * 3600      # справочники городов/брендов — 30 дней
_DICT_FILE = Path(__file__).resolve().parent.parent / "rb_dicts.json"

# марка russiabase → марка бота (из models.FUEL_LABELS)
_FUEL_MAP = {
    "ai92": "92",
    "ai95": "95",
    "ai95plus": "95+",
    "ai98": "98",
    "ai100": "100",
    "dt": "ДТ",
    "gas": "gas",
    "propan": "gas",
}
# вторичные марки сливаем в основные (берём минимальную цену)
_FUEL_MERGE = {"ai92plus": "92", "ai98plus": "98", "dtplus": "ДТ"}

# статусы restrictions, при которых топливо считается доступным
_GOOD_RB_STATUSES = {"available", "possibly_available", "has_limit"}


class RussiaBaseError(Exception):
    pass


def _norm_city(name: str) -> str:
    s = name.strip().lower().replace("ё", "е")
    s = re.sub(r"^(г\.?|город)\s*", "", s)
    return re.sub(r"\s+", " ", s)


class RussiaBaseClient:
    def __init__(self, timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(
            headers={
                "User-Agent": _UA,
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Referer": _PRICES_URL,
            },
            timeout=timeout,
            follow_redirects=True,
        )
        self._cache: dict[str, tuple[float, list[dict]]] = {}
        self._cities: dict[str, str] = {}   # норм. имя города → city_id
        self._brands: dict[str, str] = {}   # brand_id → название
        self._dicts_ready = False
        self._dicts_lock = asyncio.Lock()

    async def close(self) -> None:
        await self._client.aclose()

    # ---------- справочники ----------

    def _load_dicts_from_disk(self) -> bool:
        try:
            raw = json.loads(_DICT_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if time.time() - raw.get("ts", 0) > _DICT_TTL:
            return False
        self._cities = raw.get("cities", {})
        self._brands = raw.get("brands", {})
        return bool(self._cities)

    def _save_dicts_to_disk(self) -> None:
        try:
            _DICT_FILE.write_text(
                json.dumps(
                    {"ts": time.time(), "cities": self._cities, "brands": self._brands},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("russiabase: cannot save dicts: %s", exc)

    async def _ensure_dicts(self) -> None:
        if self._dicts_ready:
            return
        async with self._dicts_lock:
            if self._dicts_ready:
                return
            if self._load_dicts_from_disk():
                self._dicts_ready = True
                return
            try:
                regions = (
                    await self._client.get(_API + "regions")
                ).json().get("regions", [])
                brands = (
                    await self._client.get(_API + "azsBrands")
                ).json().get("brands", [])
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("russiabase dicts failed: %s", exc)
                raise RussiaBaseError(str(exc)) from exc
            self._brands = {str(b["id"]): b["value"] for b in brands}
            cities: dict[str, str] = {}
            for reg in regions:
                try:
                    resp = await self._client.get(
                        _API + "cities", params={"region": reg["id"]}
                    )
                    for c in resp.json().get("cities", []):
                        cities.setdefault(_norm_city(c["value"]), str(c["id"]))
                except (httpx.HTTPError, ValueError):
                    continue
                await asyncio.sleep(0.15)  # не ддосим
            if not cities:
                raise RussiaBaseError("empty city dictionary")
            self._cities = cities
            self._save_dicts_to_disk()
            self._dicts_ready = True
            log.info(
                "russiabase dicts: %d cities, %d brands",
                len(self._cities), len(self._brands),
            )

    # ---------- станции ----------

    @staticmethod
    def _parse_page(html: str) -> list[dict]:
        m = _NEXT_RE.search(html)
        if not m:
            return []
        try:
            data = json.loads(m.group(1))
            listing = (
                data["props"]["pageProps"]["listingMap"]["listing"] or []
            )
        except (ValueError, KeyError, TypeError):
            return []
        out = []
        for s in listing:
            try:
                lat, lon = float(s["Y"]), float(s["X"])
            except (TypeError, ValueError, KeyError):
                continue
            # цены
            prices: dict[str, float] = {}
            for rb_key, bot_key in _FUEL_MAP.items():
                v = s.get(rb_key)
                if v is None:
                    continue
                try:
                    price = float(v)
                except (TypeError, ValueError):
                    continue
                if price > 0:
                    prices[bot_key] = price
            for rb_key, bot_key in _FUEL_MERGE.items():
                v = s.get(rb_key)
                if v is None:
                    continue
                try:
                    price = float(v)
                except (TypeError, ValueError):
                    continue
                if price > 0 and (bot_key not in prices or price < prices[bot_key]):
                    prices[bot_key] = price
            # наличие по маркам из restrictions
            fuels_ok: list[str] = []
            any_ok = False
            try:
                restr = json.loads(s.get("restrictions") or "{}")
            except ValueError:
                restr = {}
            if isinstance(restr, dict):
                for rb_key, bot_key in {**_FUEL_MAP, **_FUEL_MERGE}.items():
                    chans = restr.get(rb_key)
                    if not isinstance(chans, dict):
                        continue
                    all_ch = chans.get("all")
                    st = all_ch.get("status") if isinstance(all_ch, dict) else None
                    if st in _GOOD_RB_STATUSES:
                        any_ok = True
                        if bot_key not in fuels_ok:
                            fuels_ok.append(bot_key)
            out.append({
                "poiid": str(s.get("poiid", "")),
                "name": str(s.get("name") or "АЗС"),
                "address": str(s.get("address") or ""),
                "lat": lat,
                "lon": lon,
                "brand_id": str(s.get("brand_id") or ""),
                "prices": prices,
                "fuels_ok": fuels_ok,
                "has_fuel": any_ok or bool(prices),
                "last_update": str(s.get("LastUpdate") or ""),
            })
        return out

    async def stations(self, city: str) -> list[dict]:
        """Станции города с ценами и наличием (кэш 15 мин)."""
        await self._ensure_dicts()
        key = _norm_city(city)
        city_id = self._cities.get(key)
        if not city_id:
            # пробуем подстроку: «набережные челны» совпадёт с «челны» и т.п.
            for name, cid in self._cities.items():
                if key in name or name in key:
                    city_id = cid
                    break
        if not city_id:
            log.info("russiabase: city not found: %s", city)
            return []
        hit = self._cache.get(city_id)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL:
            return hit[1]
        try:
            resp = await self._client.get(_PRICES_URL, params={"city": city_id})
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("russiabase request failed: %s", exc)
            raise RussiaBaseError(str(exc)) from exc
        items = self._parse_page(resp.text)
        for it in items:
            it["brand"] = self._brands.get(it["brand_id"], "")
        log.info("russiabase: %d stations for %s", len(items), city)
        self._cache[city_id] = (time.monotonic(), items)
        return items
