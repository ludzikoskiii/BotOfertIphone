"""Sortowanie ofert: wiele poziomów (np. werdykt, potem zysk malejąco), każdy rosnąco albo malejąco.

Działa na gotowych parach (oferta, wycena) — sortowanie nigdy nie wycenia ofert od nowa. Oferty
bez wartości (np. brak wyceny zysku, nieznany model) są zawsze na końcu, niezależnie od kierunku.

Model jest sortowany w kolejności generacji (jak w katalogu: … 11, 11 Pro, 11 Pro Max, SE (2020),
12 mini, 12, 12 Pro, 12 Pro Max, 13 mini, 13…), a w ramach modelu — po pamięci.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .catalog import IPHONE_MODELS
from .models import Condition, Offer, Valuation

ASC, DESC = "asc", "desc"
MAX_LEVELS = 3


@dataclass(frozen=True)
class SortField:
    key: str
    label: str
    default: str  # kierunek po wybraniu pola
    asc_label: str
    desc_label: str
    value: Callable[[Offer, Valuation], Any]  # None = brak wartości (na koniec)


def _added(offer: Offer) -> float | None:
    dt = offer.raw.created_at or offer.first_seen
    return dt.timestamp() if dt else None


_MODEL_INDEX = {m.name: i for i, m in enumerate(IPHONE_MODELS)}
_VARIANTS = {"": 0, "mini": 1, "e": 2, "plus": 3, "air": 4, "pro": 5, "pro max": 6}
_FUTURE = re.compile(r"iphone (\d+)\s*(pro max|pro|plus|mini|air|e)?$")


def model_rank(model: str | None) -> float | None:
    """Pozycja modelu w kolejności generacji; modele spoza katalogu (nowsze) — po numerze generacji."""
    if not model:
        return None
    if model in _MODEL_INDEX:
        return float(_MODEL_INDEX[model])
    m = _FUTURE.match(model.strip().lower())
    if m:
        return len(IPHONE_MODELS) + int(m.group(1)) * 10 + _VARIANTS.get(m.group(2) or "", 0)
    return None


_CONDITION_RANK = {Condition.FOR_PARTS: 0, Condition.DAMAGED: 1, Condition.GOOD: 2, Condition.LIKE_NEW: 3,
                   Condition.NEW: 4}


_UNKNOWN_STORAGE = 10**7  # nieznana pamięć — za znanymi pojemnościami tego modelu


def _model(offer: Offer, val: Valuation) -> tuple[float, int] | None:
    """Generacja, a w ramach modelu pamięć (64 → 128 → 256 GB…)."""
    rank = model_rank(offer.parsed.model)
    return None if rank is None else (rank, offer.parsed.storage_gb or _UNKNOWN_STORAGE)


FIELDS: dict[str, SortField] = {f.key: f for f in (
    SortField("verdict", "Werdykt", DESC, "najsłabszy najpierw", "najlepszy najpierw",
              lambda o, v: v.verdict.rank),
    SortField("profit", "Zysk", DESC, "najmniejszy", "największy", lambda o, v: v.expected_profit),
    SortField("score", "Ocena", DESC, "najniższa", "najwyższa", lambda o, v: v.score),
    SortField("price", "Cena", ASC, "od najtańszych", "od najdroższych", lambda o, v: o.price),
    SortField("model", "Model (generacje)", ASC, "od najstarszego", "od najnowszego", _model),
    SortField("storage", "Pamięć", DESC, "od najmniejszej", "od największej",
              lambda o, v: o.parsed.storage_gb),
    SortField("added", "Data dodania", DESC, "najstarsze", "najnowsze", lambda o, v: _added(o)),
    SortField("distance", "Odległość", ASC, "najbliżej", "najdalej", lambda o, v: o.distance_km),
    SortField("battery", "Bateria", DESC, "najsłabsza", "najlepsza", lambda o, v: o.parsed.battery_health),
    SortField("market", "Wartość rynkowa", DESC, "najniższa", "najwyższa", lambda o, v: v.market.value),
    SortField("max_buy", "Max cena zakupu", DESC, "najniższa", "najwyższa", lambda o, v: v.max_buy_price),
    SortField("condition", "Stan", DESC, "najgorszy", "najlepszy",
              lambda o, v: _CONDITION_RANK.get(o.parsed.condition, 0)),
    SortField("flags", "Czerwone flagi", DESC, "najmniej", "najwięcej",
              lambda o, v: len(set(v.flags)) + (100 if v.has_hard_flag else 0)),
    SortField("source", "Portal", ASC, "A → Z", "Z → A", lambda o, v: o.raw.source),
    SortField("photos", "Liczba zdjęć", DESC, "najmniej", "najwięcej", lambda o, v: len(o.raw.photos)),
    SortField("risk", "Ryzyko oszustwa", DESC, "najmniejsze", "największe",
              lambda o, v: getattr(v.risk, "score", None)),
)}


@dataclass(frozen=True)
class SortLevel:
    field: str
    order: str = DESC

    @property
    def descending(self) -> bool:
        return self.order == DESC

    def toggled(self) -> SortLevel:
        return SortLevel(self.field, ASC if self.descending else DESC)

    def describe(self) -> str:
        f = FIELDS[self.field]
        return f"{f.label} ({f.desc_label if self.descending else f.asc_label})"


DEFAULT_SORT: tuple[SortLevel, ...] = (SortLevel("verdict", DESC), SortLevel("profit", DESC))

# gotowe zestawy do wyboru jednym kliknięciem
PRESETS: dict[str, tuple[SortLevel, ...]] = {
    "Najlepsze okazje: werdykt, potem zysk": DEFAULT_SORT,
    "Największy zysk": (SortLevel("profit", DESC),),
    "Najwyższa ocena": (SortLevel("score", DESC), SortLevel("profit", DESC)),
    "Najtańsze": (SortLevel("price", ASC),),
    "Model i pamięć (generacje)": (SortLevel("model", ASC), SortLevel("price", ASC)),
    "Najnowsze ogłoszenia": (SortLevel("added", DESC),),
}


def level(field: str, order: str | None = None) -> SortLevel:
    return SortLevel(field, order if order in (ASC, DESC) else FIELDS[field].default)


def normalize(spec: Iterable[SortLevel]) -> tuple[SortLevel, ...]:
    """Bez powtórzeń pól i nieznanych pól, najwyżej ``MAX_LEVELS`` poziomów."""
    out: list[SortLevel] = []
    for lv in spec:
        if lv.field in FIELDS and all(x.field != lv.field for x in out):
            out.append(lv)
    return tuple(out[:MAX_LEVELS])


def spec_to_json(spec: Iterable[SortLevel]) -> list[list[str]]:
    return [[lv.field, lv.order] for lv in spec]


def spec_from_json(data: Any) -> tuple[SortLevel, ...]:
    """Zapis z ustawień; uszkodzony albo pusty → sortowanie domyślne."""
    levels = []
    if isinstance(data, list):
        for item in data:
            if isinstance(item, (list, tuple)) and len(item) == 2 and item[0] in FIELDS:
                levels.append(level(str(item[0]), str(item[1])))
    return normalize(levels) or DEFAULT_SORT


def describe(spec: Iterable[SortLevel]) -> str:
    return ", potem ".join(lv.describe() for lv in spec)


def sort_rows(rows: list[tuple[Offer, Valuation]], spec: Iterable[SortLevel]) -> None:
    """Sortuje listę w miejscu. Kolejne poziomy rozstrzygają remisy poprzednich."""
    for lv in reversed(normalize(spec)):
        value = FIELDS[lv.field].value
        desc = lv.descending

        def key(row, value=value, desc=desc):
            v = value(row[0], row[1])
            if v is None:  # brak wartości — zawsze na końcu
                return (0, 0) if desc else (1, 0)
            return (1, v) if desc else (0, v)

        rows.sort(key=key, reverse=desc)  # sortowanie Pythona jest stabilne także przy reverse=True
