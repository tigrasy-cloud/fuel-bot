"""Точка входа: запуск polling-бота."""

from __future__ import annotations

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ParseMode
from dotenv import load_dotenv

from .handlers import router, service
from .notifier import Notifier
from .storage import storage
from .webapp import MapServer


async def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token == "вставь_токен_от_BotFather":
        raise SystemExit(
            "Не задан BOT_TOKEN. Создай бота у @BotFather, "
            "положи токен в .env (см. .env.example) и запусти снова."
        )
    interval = int(os.getenv("CHECK_INTERVAL_SEC", "300"))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    log = logging.getLogger(__name__)

    proxy = os.getenv("PROXY_URL", "").strip()
    api_base = os.getenv("API_BASE_URL", "").strip()
    session = None
    if api_base or proxy:
        # API_BASE_URL — свой прокси к Bot API (например, Cloudflare Worker),
        # PROXY_URL — socks5/http-прокси. socks требует пакет aiohttp-socks.
        api_server = (
            TelegramAPIServer.from_base(api_base) if api_base else None
        )
        session = AiohttpSession(api=api_server, proxy=proxy or None)
        if api_base:
            log.info("Bot API через прокси-адрес: %s", api_base)
        if proxy:
            log.info("Telegram API через прокси: %s", proxy)

    bot = Bot(
        token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher()
    dp.include_router(router)

    notifier = Notifier(bot, service, storage, interval_sec=interval)
    notifier.start()

    map_server = MapServer(
        host=os.getenv("MAP_HOST", "127.0.0.1"),
        # Render и другие PaaS передают порт в переменной PORT
        port=int(os.getenv("MAP_PORT", os.getenv("PORT", "8080"))),
    )
    await map_server.start()
    logging.getLogger(__name__).info(
        "notifier started, interval=%ss; map at http://%s:%s/map",
        interval, map_server.host, map_server.port,
    )
    try:
        await dp.start_polling(bot, allowed_updates=["message", "callback_query"])
    finally:
        await notifier.stop()
        await map_server.stop()
        await service.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
