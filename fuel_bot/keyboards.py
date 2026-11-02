"""Инлайн- и реплай-клавиатуры бота."""

from __future__ import annotations

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)

from .models import City

PAGE_SIZE = 6

STATUS_FILTERS = [
    ("all", "Все"),
    ("ok", "🟢 Есть"),
    ("queue", "🟠 Очередь"),
    ("no", "🔴 Нет"),
]

FUEL_FILTERS = [
    ("any", "Любое"),
    ("92", "АИ-92"),
    ("95", "АИ-95"),
    ("98", "АИ-98"),
    ("ДТ", "ДТ"),
]


def start_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="📍 АЗС рядом со мной", request_location=True)],
            [KeyboardButton(text="🏙 Выбрать город")],
        ],
        resize_keyboard=True,
    )


def cities_keyboard(cities: list[City]) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            text=f"{c.name} ({c.sub})" if c.sub and c.sub != c.name else c.name,
            callback_data=f"city:{i}",
        )]
        for i, c in enumerate(cities)
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _mark(current: str, value: str) -> str:
    return "✅ " if current == value else ""


def stations_keyboard(
    status: str, fuel: str, page: int, pages: int,
    subscribed: bool = False, webapp_url: str = "",
) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []

    rows.append([
        InlineKeyboardButton(
            text=f"{_mark(status, v)}{label}",
            callback_data=f"flt:s={v}:f={fuel}:p=0",
        )
        for v, label in STATUS_FILTERS
    ])
    rows.append([
        InlineKeyboardButton(
            text=f"{_mark(fuel, v)}{label}",
            callback_data=f"flt:s={status}:f={v}:p=0",
        )
        for v, label in FUEL_FILTERS
    ])

    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton(
            text="⬅️", callback_data=f"flt:s={status}:f={fuel}:p={page - 1}"
        ))
    nav.append(InlineKeyboardButton(text=f"{page + 1}/{pages}", callback_data="noop"))
    if page < pages - 1:
        nav.append(InlineKeyboardButton(
            text="➡️", callback_data=f"flt:s={status}:f={fuel}:p={page + 1}"
        ))
    if pages > 1:
        rows.append(nav)

    if webapp_url:
        rows.append([InlineKeyboardButton(
            text="🗺 Открыть карту",
            web_app=WebAppInfo(url=webapp_url),
        )])
    rows.append([
        InlineKeyboardButton(text="✍️ Отметить статус", callback_data="rep:list:0"),
        InlineKeyboardButton(text="💰 Следить за ценой", callback_data="psub:menu"),
    ])
    rows.append([InlineKeyboardButton(
        text="☕ Поддержать проект", callback_data="donate:menu"
    )])
    sub_text = "🔕 Отписаться от уведомлений" if subscribed else "🔔 Уведомить, когда появится топливо"
    rows.append([InlineKeyboardButton(text=sub_text, callback_data="sub:toggle")])
    rows.append([
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"flt:s={status}:f={fuel}:p={page}:r=1"),
        InlineKeyboardButton(text="🏙 Сменить город", callback_data="change_city"),
    ])
    return InlineKeyboardMarkup(inline_keyboard=rows)
