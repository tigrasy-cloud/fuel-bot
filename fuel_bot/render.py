"""Формирование текстов сообщений."""

from __future__ import annotations

from .keyboards import PAGE_SIZE
from .models import FUEL_LABELS, Station
from .service import summary_counts

FUEL_ORDER = ["92", "95", "95+", "98", "100", "ДТ", "dt", "gas"]


def _fuel_badge(s: Station) -> str:
    """Какие марки отметили в наличии / с ценами."""
    bits = []
    seen = set()
    for fuel in FUEL_ORDER:
        label = FUEL_LABELS.get(fuel, fuel)
        if label in seen:
            continue
        key_l = fuel.lower()
        price = None
        for k, v in s.prices.items():
            if k.lower() == key_l or k == fuel:
                price = v
                break
        in_now = any(f.lower() == key_l or f == fuel for f in s.fuels_now)
        if in_now or price is not None:
            seen.add(label)
            bits.append(f"{label} {price:g}₽" if price is not None else label)
    return ", ".join(bits)


def station_line(idx: int, s: Station, user_lat: float | None = None,
                 user_lon: float | None = None) -> str:
    dist = f" · {s.distance_km:g} км" if s.distance_km is not None else ""
    addr = f"\n    📍 {s.addr}" if s.addr else ""
    fuels = _fuel_badge(s)
    fuels_line = f"\n    ⛽ {fuels}" if fuels else ""
    if s.local:
        votes = f" ×{s.local_votes}" if s.local_votes > 1 else ""
        local_mark = f" ✍️ от водителей бота{votes}"
    elif s.osm_id.startswith("ym_"):
        local_mark = " · по данным Яндекса"
    elif s.osm_id.startswith("rb_"):
        local_mark = " · по данным RussiaBase"
    elif s.yandex_fuel and s.rb_fuel:
        local_mark = " ✅ Яндекс + RussiaBase"
    elif s.yandex_fuel:
        local_mark = " ✅ Яндекс подтверждает"
    elif s.rb_fuel:
        local_mark = " ✅ RussiaBase подтверждает"
    else:
        local_mark = ""
    return (
        f"{idx}. {s.status_emoji} <b>{s.title}</b>{dist} — {s.status_text}{local_mark}"
        f"{addr}{fuels_line}\n"
        f'    <a href="{s.route_link(user_lat, user_lon)}">Маршрут</a> · '
        f'<a href="{s.gis2_link()}">2ГИС</a>'
    )


def render_stations_page(
    place: str,
    stations: list[Station],
    page: int,
    status: str,
    fuel: str,
    user_lat: float | None = None,
    user_lon: float | None = None,
    summary_all: str = "",
) -> tuple[str, int]:
    """Возвращает (текст, всего_страниц)."""
    total = len(stations)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, pages - 1))
    chunk = stations[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    header = f"⛽ <b>{place}</b>\n"
    if summary_all:
        header += f"{summary_all}\n"

    if not chunk:
        body = "\nПо этому фильтру ничего не найдено. Попробуй другой статус или марку топлива."
    else:
        lines = [
            station_line(page * PAGE_SIZE + i + 1, s, user_lat, user_lon)
            for i, s in enumerate(chunk)
        ]
        body = "\n\n".join(lines)

    footer = (
        "\n\n<i>Данные: народная карта gdebenz.ru (статусы ставят водители), "
        "Яндекс.Карты и russiabase.ru (цены и наличие). "
        "Перед заездом смотри свежесть отметки и держи запасной вариант.</i>"
    )
    return header + "\n" + body + footer, pages


def render_city_summary(place: str, stations: list[Station]) -> str:
    counts = summary_counts(stations)
    return f"⛽ <b>{place}</b> — найдено АЗС: {len(stations)}\n{counts}"
