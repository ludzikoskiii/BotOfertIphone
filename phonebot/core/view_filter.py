"""Filtry widoku tabeli (działają natychmiast, bez ponownego pobierania ofert)."""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Offer, Valuation
from .text import normalize


@dataclass
class ViewFilter:
    models: list[str] = field(default_factory=list)  # puste = wszystkie
    price_min: float = 0.0
    price_max: float = 0.0  # 0 = bez limitu
    radius_km: int = 0  # 0 = bez limitu (odległość od Twojej miejscowości)
    radius_keeps_shipping: bool = True  # dalsze oferty z wysyłką i tak pokazuj
    conditions: list[str] = field(default_factory=list)  # wartości Condition; puste = wszystkie
    sources: list[str] = field(default_factory=list)  # puste = wszystkie
    min_profit_enabled: bool = False
    min_profit: float = 150.0
    shipping_only: bool = False
    colors: list[str] = field(default_factory=list)  # np. ["green"]; puste = wszystkie
    text: str = ""

    def is_active(self) -> bool:
        return self != ViewFilter()


def matches(offer: Offer, val: Valuation, f: ViewFilter) -> bool:
    p = offer.parsed
    if f.models and p.model not in f.models:
        return False
    if f.price_min and offer.price < f.price_min:
        return False
    if f.price_max and offer.price > f.price_max:
        return False
    if f.conditions and p.condition.value not in f.conditions:
        return False
    if f.sources and offer.raw.source not in f.sources:
        return False
    if f.shipping_only and offer.raw.shipping_available is False:
        return False
    if f.radius_km:
        ships = f.radius_keeps_shipping and offer.raw.shipping_available is True
        if not ships and (offer.distance_km is None or offer.distance_km > f.radius_km):
            return False
    if f.min_profit_enabled and (val.expected_profit is None or val.expected_profit < f.min_profit):
        return False
    if f.colors and val.color.value not in f.colors:
        return False
    if f.text:
        haystack = normalize(f"{offer.raw.title} {offer.raw.city or ''}")
        if not all(word in haystack for word in normalize(f.text).split()):
            return False
    return True
