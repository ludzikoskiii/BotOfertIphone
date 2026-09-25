"""Łączy bazę danych z logiką wyceny: wycenia listę ofert jednym przebiegiem."""
from __future__ import annotations

import sqlite3

from ..core.geo import road_distance_km
from ..core.market import estimate_market_value
from ..core.models import MarketEstimate, MarketObservation, Mode, Offer, Valuation
from ..core.parts import PartsCatalog
from ..core.places import find_place
from ..core.settings import Settings
from ..core.valuation import evaluate, target_market_class
from ..storage.repositories import OfferRepository, PartsRepository


class Evaluator:
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.settings = settings
        self.offers = OfferRepository(conn)
        self.parts = PartsCatalog(PartsRepository(conn).all())
        self._obs_cache: dict[str, list[MarketObservation]] = {}
        self._market_cache: dict[tuple, MarketEstimate] = {}

    def _observations(self, model: str) -> list[MarketObservation]:
        if model not in self._obs_cache:
            self._obs_cache[model] = self.offers.market_observations(model, self.settings.market_window_days)
        return self._obs_cache[model]

    def market_for(self, offer: Offer, mode: Mode) -> MarketEstimate:
        cls = target_market_class(offer, mode)
        key = (offer.parsed.model, offer.parsed.storage_gb, cls)
        if key not in self._market_cache:
            obs = self._observations(offer.parsed.model) if offer.parsed.model else []
            self._market_cache[key] = estimate_market_value(
                offer.parsed.model, offer.parsed.storage_gb, cls, obs, self.settings
            )
        return self._market_cache[key]

    def evaluate(self, offer: Offer, mode: Mode | None = None) -> Valuation:
        mode = mode or self.settings.mode_enum
        s = self.settings
        lat, lon = offer.raw.lat, offer.raw.lon
        if lat is None or lon is None:
            place = find_place(offer.raw.city)  # np. Allegro Lokalnie podaje tylko miasto
            lat, lon = (place.lat, place.lon) if place else (None, None)
        offer.distance_km = road_distance_km(s.home_lat, s.home_lon, lat, lon)
        return evaluate(offer, self.market_for(offer, mode), self.parts, s, mode)

    def evaluate_all(self, offers: list[Offer], mode: Mode | None = None) -> list[tuple[Offer, Valuation]]:
        return [(o, self.evaluate(o, mode)) for o in offers]
