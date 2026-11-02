"""Хендлеры Telegram-бота (aiogram 3)."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Message,
    PreCheckoutQuery,
)

from .keyboards import cities_keyboard, start_keyboard, stations_keyboard
from .models import FUEL_LABELS, GOOD_STATUSES, STATUS_META, City
from .render import render_city_summary, render_stations_page
from .service import FuelService, apply_reports, filter_stations, summary_counts
from .sources.gdebenz import GdebenzError
from .storage import MAX_SUBS_PER_CHAT, storage

log = logging.getLogger(__name__)

router = Router()
service = FuelService()

CITY_SPAN = 0.18  # половина стороны bbox вокруг центра города, градусы
REPORT_STATUSES = [
    ("yes", "🟢 Есть топливо"),
    ("queue", "🟠 Очередь"),
    ("low", "🟡 Осталось мало"),
    ("no", "🔴 Топлива нет"),
]
REP_PAGE = 8

WELCOME = (
    "Привет! Я покажу, где сейчас есть топливо, а где его нет — "
    "по отметкам водителей с народной карты.\n\n"
    "🏙 Напиши название города (например: <b>Казань</b>)\n"
    "📍 Или отправь геолокацию — найду АЗС рядом с тобой."
)


# ---------- вспомогательное ----------

async def _send_stations(
    target: Message,
    state: FSMContext,
    status: str = "all",
    fuel: str = "any",
    page: int = 0,
    force: bool = False,
    edit: bool = False,
) -> None:
    data = await state.get_data()
    mode = data.get("mode")
    place = data.get("place", "АЗС")
    user_lat = data.get("user_lat")
    user_lon = data.get("user_lon")

    try:
        if mode == "geo":
            stations = await service.stations_near(
                float(data["lat"]), float(data["lon"]), force=force
            )
        else:
            stations = await service.stations_for_city(
                float(data["lat"]), float(data["lon"]), force=force
            )
    except GdebenzError:
        text = "⚠️ Источник данных сейчас не отвечает. Попробуй через минуту."
        if edit:
            await target.edit_text(text)
        else:
            await target.answer(text)
        return

    if not stations:
        text = (
            f"😕 В районе «{place}» АЗС не нашлись.\n"
            "Попробуй другой город или отправь геолокацию."
        )
        if edit:
            await target.edit_text(text, reply_markup=None)
        else:
            await target.answer(text)
        return

    apply_reports(stations, storage.latest_reports([s.osm_id for s in stations]))
    storage.touch_user(target.chat.id, place)

    if mode == "city":
        from .service import enrich_with_russiabase, enrich_with_yandex
        await enrich_with_yandex(
            service, stations, place, float(data["lat"]), float(data["lon"])
        )
        await enrich_with_russiabase(
            service, stations, place, float(data["lat"]), float(data["lon"])
        )

    filtered = filter_stations(stations, status=status, fuel=fuel)
    text, pages = render_stations_page(
        place,
        filtered,
        page,
        status,
        fuel,
        user_lat=user_lat,
        user_lon=user_lon,
        summary_all=summary_counts(stations),
    )
    subscribed = storage.find(target.chat.id, place) is not None
    kb = stations_keyboard(
        status, fuel, page, pages,
        subscribed=subscribed, webapp_url=os.getenv("WEBAPP_URL", ""),
    )
    if edit:
        await target.edit_text(
            text, reply_markup=kb, disable_web_page_preview=True
        )
    else:
        await target.answer(
            text, reply_markup=kb, disable_web_page_preview=True
        )


# ---------- команды и старт ----------

@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    await message.answer(WELCOME, reply_markup=start_keyboard())


@router.message(Command("city"))
async def cmd_city(message: Message, state: FSMContext) -> None:
    await message.answer("Напиши название города 🔎")


# ---------- геолокация ----------

@router.message(F.location)
async def on_location(message: Message, state: FSMContext) -> None:
    lat, lon = message.location.latitude, message.location.longitude
    await state.update_data(
        mode="geo", lat=lat, lon=lon, user_lat=lat, user_lon=lon,
        place="АЗС рядом с тобой",
    )
    await _send_stations(message, state)


# ---------- выбор города ----------

@router.message(F.text == "🏙 Выбрать город")
async def ask_city(message: Message) -> None:
    await message.answer("Напиши название города 🔎")


@router.message(F.text, ~F.text.startswith("/"))
async def on_city_query(message: Message, state: FSMContext) -> None:
    query = (message.text or "").strip()
    if len(query) < 2:
        await message.answer("Напиши хотя бы 2 буквы названия города.")
        return
    try:
        cities = await service.client.search_cities(query)
    except GdebenzError:
        await message.answer("⚠️ Источник данных сейчас не отвечает. Попробуй позже.")
        return
    if not cities:
        await message.answer(
            f"Город «{query}» не нашёлся. Проверь написание или отправь геолокацию 📍"
        )
        return
    await state.update_data(city_results=[c.__dict__ for c in cities])
    await message.answer("Выбери город:", reply_markup=cities_keyboard(cities))


@router.callback_query(F.data.startswith("city:"))
async def on_city_pick(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.split(":", 1)[1])
    data = await state.get_data()
    raw = (data.get("city_results") or [])[idx]
    city = City(**raw)
    await state.update_data(
        mode="city", lat=city.lat, lon=city.lon,
        user_lat=city.lat, user_lon=city.lon, place=city.name,
    )
    await callback.message.edit_text(f"🏙 Город: <b>{city.name}</b>. Загружаю АЗС…")
    await _send_stations(callback.message, state, edit=True)
    await callback.answer()


# ---------- фильтры / страницы / обновление ----------

@router.callback_query(F.data.startswith("flt:"))
async def on_filter(callback: CallbackQuery, state: FSMContext) -> None:
    params = dict(
        kv.split("=", 1) for kv in callback.data[4:].split(":") if "=" in kv
    )
    status = params.get("s", "all")
    fuel = params.get("f", "any")
    page = int(params.get("p", 0))
    force = params.get("r") == "1"
    await _send_stations(
        callback.message, state, status=status, fuel=fuel,
        page=page, force=force, edit=True,
    )
    await callback.answer("Обновлено 🔄" if force else None)


@router.callback_query(F.data == "change_city")
async def on_change_city(callback: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await callback.message.edit_text(WELCOME)
    await callback.message.answer("Куда едем?", reply_markup=start_keyboard())
    await callback.answer()


@router.callback_query(F.data == "noop")
async def on_noop(callback: CallbackQuery) -> None:
    await callback.answer()


SUB_FUEL_OPTIONS = [
    ("any", "⛽ Любое топливо"),
    ("92", "АИ-92"),
    ("95", "АИ-95"),
    ("98", "АИ-98"),
    ("ДТ", "Дизель"),
]


def _fuel_label(fuel: str) -> str:
    return "любое топливо" if fuel == "any" else FUEL_LABELS.get(fuel, fuel)


# ---------- подписки ----------

@router.callback_query(F.data == "sub:toggle")
async def on_sub_toggle(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    place = data.get("place")
    if not place:
        await callback.answer("Сначала выбери город или отправь геолокацию.")
        return

    chat_id = callback.message.chat.id
    existing = storage.find(chat_id, place)
    if existing:
        storage.delete(existing.id)
        await callback.answer(f"🔕 Отписался от «{place}».")
        await _send_stations(callback.message, state, edit=True)
        return

    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"sub:fuel:{value}")]
        for value, label in SUB_FUEL_OPTIONS
    ]
    await callback.message.answer(
        f"🔔 Подписка на «{place}». Какое топливо отслеживать?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("sub:fuel:"))
async def on_sub_fuel_pick(callback: CallbackQuery, state: FSMContext) -> None:
    fuel = callback.data.rsplit(":", 1)[1]
    data = await state.get_data()
    place = data.get("place")
    if not place:
        await callback.answer("Сначала выбери город.", show_alert=True)
        return

    try:
        if data.get("mode") == "geo":
            stations = await service.stations_near(float(data["lat"]), float(data["lon"]))
        else:
            stations = await service.stations_for_city(float(data["lat"]), float(data["lon"]))
    except GdebenzError:
        await callback.answer("⚠️ Источник не отвечает, попробуй позже.")
        return

    if fuel != "any":
        stations = filter_stations(stations, fuel=fuel)
    baseline = [s.osm_id for s in stations if s.status in GOOD_STATUSES]
    sub_id = storage.add(
        chat_id=callback.message.chat.id,
        mode=data.get("mode", "city"),
        lat=float(data["lat"]),
        lon=float(data["lon"]),
        place=place,
        fuel=fuel,
        green_ids=baseline,
    )
    if sub_id is None:
        await callback.answer(
            f"Уже есть {MAX_SUBS_PER_CHAT} подписок — удали лишние в /subscriptions.",
            show_alert=True,
        )
        return
    await callback.message.edit_text(
        f"🔔 Подписан на «{place}» — отслеживаю: <b>{_fuel_label(fuel)}</b>.\n"
        "Проверяю каждые 5 минут и напишу, как только появится новая 🟢 АЗС.\n"
        "Управление: /subscriptions"
    )
    await callback.answer()


@router.message(Command("subscriptions"))
async def cmd_subscriptions(message: Message) -> None:
    subs = storage.list_for_chat(message.chat.id)
    price_subs = storage.list_price_subs(message.chat.id)
    if not subs and not price_subs:
        await message.answer(
            "У тебя пока нет подписок.\n"
            "Выбери город и нажми «🔔 Уведомить, когда появится топливо» "
            "или «💰 Следить за ценой»."
        )
        return
    buttons = [
        [InlineKeyboardButton(
            text=f"🔕 {s.place} · {_fuel_label(s.fuel)}",
            callback_data=f"sub:del:{s.id}",
        )]
        for s in subs
    ]
    buttons += [
        [InlineKeyboardButton(
            text=f"💰❌ {p['place']} · {FUEL_LABELS.get(p['fuel'], p['fuel'])}",
            callback_data=f"psub:del:{p['id']}",
        )]
        for p in price_subs
    ]
    await message.answer(
        "Твои подписки (проверяю каждые 5 минут):",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith("sub:del:"))
async def on_sub_delete(callback: CallbackQuery) -> None:
    sub_id = int(callback.data.rsplit(":", 1)[1])
    storage.delete(sub_id)
    await callback.answer("Подписка удалена 🔕")
    await cmd_subscriptions(callback.message)


# ---------- отметки пользователей ----------

async def _context_stations(state: FSMContext):
    data = await state.get_data()
    if data.get("mode") == "geo":
        return data, await service.stations_near(float(data["lat"]), float(data["lon"]))
    return data, await service.stations_for_city(float(data["lat"]), float(data["lon"]))


def _rep_picker_kb(data: dict, page: int) -> tuple[InlineKeyboardMarkup, int]:
    ids = data["rep_ids"]
    pages = max(1, (len(ids) + REP_PAGE - 1) // REP_PAGE)
    page = max(0, min(page, pages - 1))
    buttons = []
    for i in range(page * REP_PAGE, min((page + 1) * REP_PAGE, len(ids))):
        title = data["rep_titles"][i]
        addr = data["rep_addrs"][i]
        label = f"{title} — {addr}" if addr else title
        buttons.append([InlineKeyboardButton(
            text=label[:64], callback_data=f"rep:pick:{i}"
        )])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="⬅️", callback_data=f"rep:list:{page - 1}"))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(text="➡️", callback_data=f"rep:list:{page + 1}"))
    if pages > 1:
        buttons.append(nav)
    return InlineKeyboardMarkup(inline_keyboard=buttons), page


@router.callback_query(F.data.startswith("rep:list:"))
async def on_rep_list(callback: CallbackQuery, state: FSMContext) -> None:
    page = int(callback.data.rsplit(":", 1)[1])
    data = await state.get_data()
    if not data.get("place"):
        await callback.answer("Сначала выбери город или отправь геолокацию.")
        return
    if "rep_ids" not in data:
        try:
            _, stations = await _context_stations(state)
        except GdebenzError:
            await callback.answer("⚠️ Источник не отвечает, попробуй позже.")
            return
        if not stations:
            await callback.answer("АЗС рядом не найдены.")
            return
        await state.update_data(
            rep_ids=[s.osm_id for s in stations],
            rep_titles=[s.title for s in stations],
            rep_addrs=[s.addr for s in stations],
        )
        data = await state.get_data()

    kb, page = _rep_picker_kb(data, page)
    text = "✍️ На какой АЗС отметить текущий статус?"
    if data.get("rep_msg_id") == callback.message.message_id:
        await callback.message.edit_text(text, reply_markup=kb)
    else:
        msg = await callback.message.answer(text, reply_markup=kb)
        await state.update_data(rep_msg_id=msg.message_id)
    await callback.answer()


@router.callback_query(F.data.startswith("rep:pick:"))
async def on_rep_pick(callback: CallbackQuery, state: FSMContext) -> None:
    idx = int(callback.data.rsplit(":", 1)[1])
    data = await state.get_data()
    title = data["rep_titles"][idx]
    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"rep:set:{idx}:{value}")]
        for value, label in REPORT_STATUSES
    ]
    buttons.append([InlineKeyboardButton(
        text="⬅️ Назад", callback_data=f"rep:list:{idx // REP_PAGE}"
    )])
    await callback.message.edit_text(
        f"Что сейчас на «{title}»?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("rep:set:"))
async def on_rep_set(callback: CallbackQuery, state: FSMContext) -> None:
    _, _, idx_s, status = callback.data.split(":", 3)
    idx = int(idx_s)
    data = await state.get_data()
    station_id = data["rep_ids"][idx]
    title = data["rep_titles"][idx]
    storage.add_report(
        station_id=station_id,
        place=data.get("place", ""),
        status=status,
        chat_id=callback.message.chat.id,
    )
    status_text = STATUS_META.get(status, ("", status))[1]
    await callback.message.edit_text(
        f"✅ Спасибо! Отметка «{status_text}» для «{title}» сохранена.\n\n"
        "Она действует 6 часов, видна другим пользователям бота "
        "и появится в списке после нажатия 🔄."
    )
    await callback.answer()


# ---------- подписки на цены ----------

@router.callback_query(F.data == "psub:menu")
async def on_psub_menu(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    if not data.get("place"):
        await callback.answer("Сначала выбери город или отправь геолокацию.")
        return
    buttons = [
        [InlineKeyboardButton(text=label, callback_data=f"psub:fuel:{value}")]
        for value, label in SUB_FUEL_OPTIONS if value != "any"
    ]
    await callback.message.answer(
        "💰 За ценой на какое топливо следить?\n"
        "Напишу, когда минимальная цена в районе изменится на 1₽ и больше.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )
    await callback.answer()


@router.callback_query(F.data.startswith("psub:fuel:"))
async def on_psub_fuel(callback: CallbackQuery, state: FSMContext) -> None:
    fuel = callback.data.rsplit(":", 1)[1]
    data = await state.get_data()
    place = data.get("place")
    if not place:
        await callback.answer("Сначала выбери город.", show_alert=True)
        return

    if storage.find_price_sub(callback.message.chat.id, place, fuel):
        await callback.message.edit_text(
            f"Ты уже следишь за ценой на {FUEL_LABELS.get(fuel, fuel)} в «{place}»."
        )
        await callback.answer()
        return

    try:
        _, stations = await _context_stations(state)
    except GdebenzError:
        await callback.answer("⚠️ Источник не отвечает, попробуй позже.")
        return

    fuel_l = fuel.lower()
    prices = [
        v for s in stations for k, v in s.prices.items() if k.lower() == fuel_l
    ]
    if not prices:
        await callback.message.edit_text(
            f"😕 В «{place}» пока нет данных о ценах на {FUEL_LABELS.get(fuel, fuel)}."
        )
        await callback.answer()
        return

    baseline = min(prices)
    sub_id = storage.add_price_sub(
        chat_id=callback.message.chat.id,
        mode=data.get("mode", "city"),
        lat=float(data["lat"]),
        lon=float(data["lon"]),
        place=place,
        fuel=fuel,
        baseline=baseline,
    )
    if sub_id is None:
        await callback.answer(
            f"Уже есть {MAX_SUBS_PER_CHAT} подписок на цены — удали лишние в /subscriptions.",
            show_alert=True,
        )
        return
    await callback.message.edit_text(
        f"💰 Слежу за ценой на <b>{FUEL_LABELS.get(fuel, fuel)}</b> в «{place}».\n"
        f"Текущий минимум: <b>{baseline:g}₽</b>. Напишу, когда он изменится на 1₽ и больше.\n"
        "Управление: /subscriptions"
    )
    await callback.answer()


@router.callback_query(F.data.startswith("psub:del:"))
async def on_psub_delete(callback: CallbackQuery) -> None:
    sub_id = int(callback.data.rsplit(":", 1)[1])
    storage.delete_price_sub(sub_id)
    await callback.answer("Подписка на цену удалена 🔕")
    await cmd_subscriptions(callback.message)


# ---------- поддержка проекта (Telegram Stars) ----------

DONATE_AMOUNTS = [50, 100, 250]

DONATE_TEXT = (
    "☕ <b>Поддержать проект</b>\n\n"
    "Бот бесплатный и работает на народных данных. Звёзды пойдут на "
    "хостинг и развитие: больше источников, быстрее уведомления.\n"
    "Выбери сумму:"
)


async def _send_donate_menu(message: Message) -> None:
    buttons = [
        [InlineKeyboardButton(
            text=f"⭐ {amount}",
            callback_data=f"donate:pay:{amount}",
        )]
        for amount in DONATE_AMOUNTS
    ]
    await message.answer(
        DONATE_TEXT, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons)
    )


@router.message(Command("donate"))
async def cmd_donate(message: Message) -> None:
    await _send_donate_menu(message)


@router.callback_query(F.data == "donate:menu")
async def on_donate_menu(callback: CallbackQuery) -> None:
    await _send_donate_menu(callback.message)
    await callback.answer()


@router.callback_query(F.data.startswith("donate:pay:"))
async def on_donate_pay(callback: CallbackQuery) -> None:
    amount = int(callback.data.rsplit(":", 1)[1])
    await callback.bot.send_invoice(
        chat_id=callback.message.chat.id,
        title="☕ Поддержка «ГдеБензин?»",
        description=(
            "Добровольная поддержка бота: хостинг и развитие. "
            "Услуга не оказывается, это донат — спасибо!"
        ),
        payload=f"donate_{amount}",
        currency="XTR",
        prices=[LabeledPrice(label=f"{amount} звёзд", amount=amount)],
    )
    await callback.answer()


@router.pre_checkout_query()
async def on_pre_checkout(query: PreCheckoutQuery) -> None:
    await query.answer(ok=True)


@router.message(F.successful_payment)
async def on_successful_payment(message: Message) -> None:
    stars = message.successful_payment.total_amount
    await message.answer(
        f"💚 Спасибо огромное за {stars} ⭐!\n"
        "Ты помог боту жить и развиваться. Хорошей дороги и полный бак! ⛽"
    )


# ---------- статистика (только для админа) ----------

async def _track_user_middleware(handler, event, data):
    """Записывает факт активности пользователя (без персональных данных)."""
    try:
        msg = event if isinstance(event, Message) else event.message
        if msg and msg.chat:
            storage.touch_user(msg.chat.id)
    except Exception:
        pass
    return await handler(event, data)


router.message.middleware(_track_user_middleware)
router.callback_query.middleware(_track_user_middleware)


def _admin_ids() -> set[int]:
    """Читает ADMIN_IDS из переменной окружения или .env при каждом вызове — рестарт не нужен."""
    raw = os.environ.get("ADMIN_IDS")
    if raw is None:
        env_path = Path(__file__).resolve().parent / ".env"
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                if line.startswith("ADMIN_IDS="):
                    raw = line.split("=", 1)[1]
                    break
        except OSError:
            pass
    if not raw:
        return set()
    return {int(x) for x in raw.split(",") if x.strip().isdigit()}


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    admins = _admin_ids()
    if not admins:
        # Первичная настройка: сообщаем владельцу его ID
        await message.answer(
            f"👤 Твой Telegram ID: <code>{message.from_user.id}</code>\n"
            "Впиши его в fuel_bot/.env строкой\n"
            f"<code>ADMIN_IDS={message.from_user.id}</code>\n"
            "— и команда /stats станет видна только тебе."
        )
        return
    if message.from_user.id not in admins:
        return  # для остальных команды как будто нет

    s = storage.get_stats()
    places = "\n".join(
        f"  · {place}: {n}" for place, n in s["top_places"]
    ) or "  · пока нет данных"
    await message.answer(
        "📊 <b>Статистика бота</b>\n\n"
        f"👥 Пользователей всего: <b>{s['total']}</b>\n"
        f"🆕 Новых за сегодня: {s['new_today']} · за 7 дней: {s['new_week']}\n"
        f"⚡ Активных за 24 часа: {s['active_24h']}\n\n"
        f"🔔 Подписок на топливо: {s['subs']}\n"
        f"💰 Подписок на цены: {s['price_subs']}\n"
        f"✍️ Отметок за 24 часа: {s['reports_24h']}\n\n"
        f"🏙 Топ городов:\n{places}"
    )
