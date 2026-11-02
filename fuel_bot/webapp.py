"""Мини-веб-сервер для Telegram Mini App: интерактивная карта АЗС.

Отдаёт статичную страницу /map (Leaflet) и проксирует /api/stations
к gdebenz.ru, добавляя нужные заголовки. Для кнопки WebApp в Telegram
нужен публичный HTTPS-URL (например, через ngrok) в переменной WEBAPP_URL.
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from aiohttp import web

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "webapp_static"

_GDEBENZ_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
    ),
    "Referer": "https://gdebenz.ru/",
    "Accept": "application/json",
}


class MapServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8080) -> None:
        self.host = host
        self.port = port
        self._client = httpx.AsyncClient(
            headers=_GDEBENZ_HEADERS, timeout=10.0, follow_redirects=True
        )
        self._runner: web.AppRunner | None = None

    async def _index(self, _request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC_DIR / "map.html")

    async def _stations(self, request: web.Request) -> web.Response:
        q = request.rel_url.query
        params = {k: q.get(k, "") for k in ("lat1", "lon1", "lat2", "lon2")}
        try:
            resp = await self._client.get(
                "https://gdebenz.ru/api/stations", params=params
            )
            resp.raise_for_status()
            return web.json_response(resp.json())
        except (httpx.HTTPError, ValueError) as exc:
            log.warning("stations proxy failed: %s", exc)
            return web.json_response({"error": "source unavailable"}, status=502)

    async def start(self) -> None:
        app = web.Application()
        app.router.add_get("/", self._index)
        app.router.add_get("/map", self._index)
        app.router.add_get("/api/stations", self._stations)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        await web.TCPSite(self._runner, self.host, self.port).start()
        log.info("map server: http://%s:%s/map", self.host, self.port)

    async def stop(self) -> None:
        await self._client.aclose()
        if self._runner:
            await self._runner.cleanup()
