"""Модель данных и метаинформация о статусах АЗС."""

from __future__ import annotations

from dataclasses import dataclass, field

# Статусы в терминах gdebenz.ru → эмодзи + человекочитаемое название
STATUS_META: dict[str, tuple[str, str]] = {
    "yes":     ("🟢", "Есть топливо"),
    "queue":   ("🟠", "Очередь"),
    "low":     ("🟡", "Осталось мало"),
    "no":      ("🔴", "Топлива нет"),
    "dtonly":  ("🔵", "Только ДТ"),
    "changing":("🟣", "Статус меняется"),
    "none":    ("⚪", "Нет данных"),
    "unknown": ("⚪", "Нет данных"),
}

# Статусы, означающие «заправиться скорее всего получится»
GOOD_STATUSES = {"yes", "low", "queue"}

FUEL_LABELS = {
    "92": "АИ-92",
    "95": "АИ-95",
    "95+": "АИ-95+",
    "98": "АИ-98",
    "100": "АИ-100",
    "ДТ": "ДТ",
    "dt": "ДТ",
    "gas": "Газ",
}


@dataclass
class City:
    name: str
    sub: str
    lat: float
    lon: float
    zoom: int = 13


@dataclass
class Station:
    osm_id: str
    name: str
    brand: str
    lat: float
    lon: float
    addr: str = ""
    status: str = "none"
    fuels_now: list[str] = field(default_factory=list)   # марки топлива, которое отметили как есть
    prices: dict[str, float] = field(default_factory=dict)  # марка → цена
    distance_km: float | None = None
    local: bool = False        # статус переопределён отметкой пользователя бота
    local_votes: int = 0       # сколько свежих отметок пользователей
    yandex_fuel: bool = False  # станция есть в категории «АЗС с топливом» Яндекса
    rb_fuel: bool = False      # станция подтверждена данными RussiaBase (цены/наличие)

    @property
    def status_emoji(self) -> str:
        return STATUS_META.get(self.status, STATUS_META["none"])[0]

    @property
    def status_text(self) -> str:
        return STATUS_META.get(self.status, STATUS_META["none"])[1]

    @property
    def title(self) -> str:
        return self.brand or self.name or "АЗС"

    def yandex_link(self) -> str:
        return f"https://yandex.ru/maps/?pt={self.lon},{self.lat}&z=17&l=map"

    def gis2_link(self) -> str:
        return f"https://2gis.ru/search/{self.lon}%2C{self.lat}"

    def route_link(self, from_lat: float | None = None, from_lon: float | None = None) -> str:
        if from_lat is not None and from_lon is not None:
            return f"https://yandex.ru/maps/?rtext={from_lat},{from_lon}~{self.lat},{self.lon}&rtt=auto"
        return f"https://yandex.ru/maps/?rtext=~{self.lat},{self.lon}&rtt=auto"
