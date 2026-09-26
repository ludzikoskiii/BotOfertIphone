"""Łączy bazę danych z logiką wyceny: wycenia listę ofert jednym przebiegiem."""
from __future__ import annotations

import sqlite3

from ..core.fraud import assess
from ..core.geo import road_distance_km
from ..core.market import estimate_market_value
from ..core.models import MarketEstimate, MarketObservation, Mode, Offer, Valuation
from ..core.parts import PartsCatalog
from ..core.places import find_place
from ..core.settings import Settings
from ..core.valuation import evaluate, target_market_class
from ..ml.desc_model import apply_to_offer
from ..storage.repositories import OfferRepository, PartsRepository
from .fraud_service import apply_risk, build_context
from .reference_prices import ReferenceRepository, blend, lookup


class Evaluator:
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.settings = settings
        self.conn = conn
        self._fraud_ctx = None  # kontekst oszustw (opisy, zdjęcia, sprzedający) — raz na przebieg
        self.offers = OfferRepository(conn)
        self.parts = PartsCatalog(PartsRepository(conn).all())
        self._obs_cache: dict[str, list[MarketObservation]] = {}
        self._market_cache: dict[tuple, MarketEstimate] = {}
        self.references = ReferenceRepository(conn).all() if settings.reference_enabled else {}

    def _observations(self, model: str) -> list[MarketObservation]:
        if model not in self._obs_cache:
            self._obs_cache[model] = self.offers.market_observations(model, self.settings.market_window_days)
        return self._obs_cache[model]

    def market_for(self, offer: Offer, mode: Mode) -> MarketEstimate:
        cls = target_market_class(offer, mode)
        key = (offer.parsed.model, offer.parsed.storage_gb, cls)
        if key not in self._market_cache:
            obs = self._observations(offer.parsed.model) if offer.parsed.model else []
            market = estimate_market_value(offer.parsed.model, offer.parsed.storage_gb, cls, obs, self.settings)
            if cls in set(self.settings.reference_condition_map.values()):  # sklepy z odnowionymi = „używany”
                ref = lookup(offer.parsed.model, offer.parsed.storage_gb, self.references, self.settings)
                market = blend(market, ref, self.settings)
            self._market_cache[key] = market
        return self._market_cache[key]

    def evaluate(self, offer: Offer, mode: Mode | None = None) -> Valuation:
        mode = mode or self.settings.mode_enum
        s = self.settings
        # wynik lokalnego modelu językowego z opisu (pamięć, bateria, usterki) przed wyceną
        apply_to_offer(offer, enabled=s.ml.llm_enabled, battery_threshold=s.battery_health_threshold)
        lat, lon = offer.raw.lat, offer.raw.lon
        if lat is None or lon is None:
            place = find_place(offer.raw.city)  # np. Allegro Lokalnie podaje tylko miasto
            lat, lon = (place.lat, place.lon) if place else (None, None)
        offer.distance_km = road_distance_km(s.home_lat, s.home_lon, lat, lon)
        val = evaluate(offer, self.market_for(offer, mode), self.parts, s, mode)
        if s.fraud.enabled:
            if self._fraud_ctx is None:
                self._fraud_ctx = build_context(self.conn, s)
            apply_risk(val, assess(offer, val.market.value, self._fraud_ctx, s.fraud), s)
        return val

    def evaluate_all(self, offers: list[Offer], mode: Mode | None = None) -> list[tuple[Offer, Valuation]]:
        return [(o, self.evaluate(o, mode)) for o in offers]
