"""Второй источник: категория «АЗС с топливом» в Яндекс.Картах.

Яндекс вшивает результаты поиска в HTML страницы (JSON в состоянии).
Парсим страницу поиска «АЗС с топливом <город>» и извлекаем станции.

Внимание: это непубличный интерфейс — используем аккуратно
(кэш 15 минут, один запрос на город), на свой риск с точки зрения ToS.
"""

from __future__ import annotations

import json
import logging
import time

import httpx

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)
_SEARCH_URL = "https://yandex.ru/maps/"
_MARKER = '"results":{"requestId"'
_CACHE_TTL = 15 * 60


class YandexMapsError(Exception):
    pass


def _extract_results(html: str) -> list[dict]:
    i = html.find(_MARKER)
    if i < 0:
        return []
    start = html.find("{", i)
    depth = 0
    j = start
    while j < len(html):
        c = html[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    try:
        data = json.loads(html[start:j + 1])
    except (ValueError, json.JSONDecodeError):
        return []
    items = []
    for it in data.get("items", []):
        if it.get("type") != "business":
            continue
        coords = it.get("coordinates") or it.get("displayCoordinates")
        if not coords or len(coords) != 2:
            continue
        items.append({
            "id": str(it.get("id", "")),
            "title": str(it.get("title", "")),
            "address": str(it.get("address", "")),
            "lon": float(coords[0]),
            "lat": float(coords[1]),
        })
    return items


class YandexMapsClient:
    def __init__(self, timeout: float = 15.0) -> None:
        self._client = httpx.AsyncClient(
            headers={"User-Agent": _UA, "Accept-Language": "ru-RU,ru;q=0.9"},
            timeout=timeout,
            follow_redirects=True,
        )
        self._cache: dict[str, tuple[float, list[dict]]] = {}

    async def close(self) -> None:
        await self._client.aclose()

    async def fuel_stations(self, city: str) -> list[dict]:
        """Станции из категории «АЗС с топливом» для города (кэш 15 мин)."""
        key = city.strip().lower()
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < _CACHE_TTL:
            return hit[1]
        try:
            resp = await self._client.get(
                _SEARCH_URL,
                params={"text": f"АЗС с топливом {city}", "lang": "ru_RU"},
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("yandex maps request failed: %s", exc)
            raise YandexMapsError(str(exc)) from exc
        items = _extract_results(resp.text)
        log.info("yandex maps: %d stations for %s", len(items), city)
        self._cache[key] = (time.monotonic(), items)
        return items
