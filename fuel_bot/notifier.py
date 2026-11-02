"""Фоновая задача: периодически проверяет подписки и шлёт уведомления."""

from __future__ import annotations

import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from .models import FUEL_LABELS, GOOD_STATUSES
from .render import station_line
from .service import FuelService, filter_stations
from .sources.gdebenz import GdebenzError
from .storage import Storage, Subscription

log = logging.getLogger(__name__)

MAX_NEW_IN_MSG = 5
PRICE_ALERT_THRESHOLD = 1.0  # ₽ — изменение минимальной цены для уведомления


class Notifier:
    def __init__(
        self,
        bot: Bot,
        service: FuelService,
        storage: Storage,
        interval_sec: int = 300,
    ) -> None:
        self.bot = bot
        self.service = service
        self.storage = storage
        self.interval = interval_sec
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            try:
                await self.check_all()
            except Exception:
                log.exception("notifier tick failed")

    async def check_all(self) -> None:
        for sub in self.storage.all():
            try:
                await self._check(sub)
            except TelegramForbiddenError:
                log.info("chat %s blocked the bot, dropping sub %s", sub.chat_id, sub.id)
                self.storage.delete(sub.id)
            except GdebenzError:
                log.warning("source unavailable, skip sub %s", sub.id)
            except TelegramRetryAfter as exc:
                log.warning("rate limited, sleeping %s s", exc.retry_after)
                await asyncio.sleep(exc.retry_after)
            except Exception:
                log.exception("failed to check sub %s", sub.id)
            await asyncio.sleep(1)  # не ддосим ни источник, ни Telegram

        for psub in self.storage.all_price_subs():
            try:
                await self._check_price(psub)
            except TelegramForbiddenError:
                log.info("chat %s blocked, dropping price sub %s",
                         psub["chat_id"], psub["id"])
                self.storage.delete_price_sub(psub["id"])
            except GdebenzError:
                log.warning("source unavailable, skip price sub %s", psub["id"])
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
            except Exception:
                log.exception("failed to check price sub %s", psub["id"])
            await asyncio.sleep(1)

    async def _check_price(self, psub) -> None:
        if psub["mode"] == "geo":
            stations = await self.service.stations_near(
                psub["lat"], psub["lon"], force=True
            )
        else:
            stations = await self.service.stations_for_city(
                psub["lat"], psub["lon"], force=True
            )

        fuel_l = psub["fuel"].lower()
        priced = [
            (v, s)
            for s in stations
            for k, v in s.prices.items()
            if k.lower() == fuel_l
        ]
        if not priced:
            self.storage.touch_price_check(psub["id"])
            return

        cur, best = min(priced, key=lambda t: t[0])
        base = psub["baseline"]
        if abs(cur - base) < PRICE_ALERT_THRESHOLD:
            self.storage.touch_price_check(psub["id"])
            return

        arrow = "📈 выросла" if cur > base else "📉 упала"
        cheapest = sorted(priced, key=lambda t: t[0])[:3]
        lines = "\n".join(
            f"  · {s.title} — {s.addr or 'без адреса'}: {p:g}₽"
            for p, s in cheapest
        )
        fuel_label = FUEL_LABELS.get(psub["fuel"], psub["fuel"])
        text = (
            f"💰 <b>{psub['place']}</b>: цена на {fuel_label} {arrow}\n"
            f"Минимум: было {base:g}₽ → стало {cur:g}₽ ({cur - base:+g}₽)\n\n"
            f"Самые дешёвые сейчас:\n{lines}"
            "\n\n<i>Отписаться: /subscriptions</i>"
        )
        await self.bot.send_message(
            psub["chat_id"], text, disable_web_page_preview=True
        )
        self.storage.update_price_baseline(psub["id"], cur)
        log.info(
            "price alert chat %s: %s %s -> %s",
            psub["chat_id"], fuel_label, base, cur,
        )

    async def _check(self, sub: Subscription) -> None:
        if sub.mode == "geo":
            stations = await self.service.stations_near(sub.lat, sub.lon, force=True)
        else:
            stations = await self.service.stations_for_city(sub.lat, sub.lon, force=True)

        if sub.fuel != "any":
            stations = filter_stations(stations, fuel=sub.fuel)

        green = {s.osm_id: s for s in stations if s.status in GOOD_STATUSES}
        old_ids = set(sub.green_ids)
        new = [s for oid, s in green.items() if oid not in old_ids]

        self.storage.update_green(sub.id, list(green.keys()))

        if not new:
            return

        lines = [
            station_line(i + 1, s, sub.lat, sub.lon)
            for i, s in enumerate(new[:MAX_NEW_IN_MSG])
        ]
        extra = len(new) - len(lines)
        fuel_note = (
            f" — {FUEL_LABELS.get(sub.fuel, sub.fuel)}" if sub.fuel != "any" else ""
        )
        text = (
            f"🔔 <b>{sub.place}</b>: появилось топливо{fuel_note}!\n\n"
            + "\n\n".join(lines)
            + (f"\n\n…и ещё {extra} АЗС." if extra > 0 else "")
            + "\n\n<i>Отписаться: /subscriptions</i>"
        )
        await self.bot.send_message(
            sub.chat_id, text, disable_web_page_preview=True
        )
        log.info("notified chat %s about %d stations", sub.chat_id, len(new))
