"""Łączy bazę danych z logiką wyceny: wycenia listę ofert jednym przebiegiem."""
from __future__ import annotations

import sqlite3

from ..core.fraud import assess
from ..core.geo import road_distance_km
from ..core.market import estimate_market_value
from ..core.models import MarketEstimate, MarketObservation, Mode, Offer, OfferStatus, Valuation
from ..core.parts import PartsCatalog
from ..core.photo_scam import config_fingerprint, detect
from ..core.places import find_place
from ..core.settings import Settings
from ..core.valuation import evaluate, target_market_class
from ..ml.desc_model import apply_to_offer
from ..storage.repositories import InventoryRepository, OfferRepository, PartsRepository, RejectedRepository
from .fraud_service import apply_photo_scam, apply_risk, build_context
from .reference_prices import ReferenceRepository, blend, lookup


class Evaluator:
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.settings = settings
        self.conn = conn
        self._fraud_ctx = None  # kontekst oszustw (opisy, zdjęcia, sprzedający) — raz na przebieg
        self.offers = OfferRepository(conn)
        stock = InventoryRepository(conn).stock(settings.inventory) if settings.inventory.enabled else None
        self.parts = PartsCatalog(PartsRepository(conn).all(), stock=stock)  # magazyn: Twoja cena zakupu
        self._obs_cache: dict[str, list[MarketObservation]] = {}
        self._market_cache: dict[tuple, MarketEstimate] = {}
        self.references = ReferenceRepository(conn).all() if settings.reference_enabled else {}
        self._whitelist: set[tuple[str, str]] | None = None  # „To jest telefon” — bez wykrywania zdjęć
        self.moved_photo_scams = 0  # ile ofert ``evaluate_visible`` przeniosło do „Odrzucone”
        self._photo_fp = config_fingerprint(settings.photo_scam)

    def _whitelisted(self, offer: Offer) -> bool:
        if self._whitelist is None:
            self._whitelist = RejectedRepository(self.conn).whitelist()
        return (offer.raw.source, offer.raw.source_id) in self._whitelist

    def photo_scam(self, offer: Offer, market_value: float | None):
        """Sprzedaż zdjęcia zamiast telefonu: tekst, kategoria, zdjęcie (CLIP) i cena."""
        s = self.settings.photo_scam
        clip = (offer.layers.photo_probs or {}).get("scam:score") if offer.layers else None
        raw = offer.raw
        result = detect(raw.title, raw.description, s, category=raw.params.get("category"),
                        category_id=raw.params.get("category_id"), source=raw.source, clip_score=clip,
                        fingerprint=self._photo_fp)
        return result.with_price(raw.price, market_value, s)

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
        photo = None
        if s.photo_scam.enabled and not self._whitelisted(offer):
            photo = self.photo_scam(offer, val.market.value)
        if s.fraud.enabled:
            if self._fraud_ctx is None:
                self._fraud_ctx = build_context(self.conn, s)
            extra = [("stock_photo_text", f"„{photo.stock_photos}”")] if photo and photo.stock_photos else None
            apply_risk(val, assess(offer, val.market.value, self._fraud_ctx, s.fraud, extra=extra), s)
        if photo is not None:
            apply_photo_scam(val, photo, s)
        return val

    def evaluate_all(self, offers: list[Offer], mode: Mode | None = None) -> list[tuple[Offer, Valuation]]:
        return [(o, self.evaluate(o, mode)) for o in offers]

    def evaluate_visible(self, offers: list[Offer], mode: Mode | None = None) -> list[tuple[Offer, Valuation]]:
        """Jak ``evaluate_all``, ale oferty z pewnym wykryciem sprzedaży zdjęcia (np. dopiero po analizie zdjęcia
        albo z nową ceną rynkową) trafiają do „Odrzucone” i znikają z listy (także z wersji na telefon).
        Obserwowane zostają (Twoja decyzja) — z werdyktem ODPUŚĆ i etykietą „MOŻLIWE OSZUSTWO”."""
        rows = self.evaluate_all(offers, mode)
        moved = [(o, v) for o, v in rows if v.photo_scam == "certain" and o.status is not OfferStatus.WATCHED
                 and o.id is not None]
        if not moved:
            return rows
        rejected = RejectedRepository(self.conn)
        for offer, val in moved:
            reason = val.reasons[0].rstrip(".") if val.reasons else "sprzedaż zdjęcia zamiast telefonu"
            rejected.reject_stored(offer, "photo_scam", reason, None)
        gone = {id(o) for o, _ in moved}
        self.moved_photo_scams = len(moved)
        return [(o, v) for o, v in rows if id(o) not in gone]
