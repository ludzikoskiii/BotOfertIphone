"""Strażnik ofert: wszystkie reguły odrzucania w jednym miejscu.

Używany przy zapisie pobranych ofert (``Scanner``) i przy ponownym sprawdzeniu ofert już
zapisanych w bazie, gdy zmienią się reguły (``refilter_stored``). Reguły:

1. filtr tekstu (kategoria, „kupię”, kilka generacji, akcesoria/części, model) i minimalna cena,
2. kraj i język (portale międzynarodowe, np. Vinted): tytuł w obcym języku albo sprzedawca
   spoza Polski → flaga „Sprzedawca z zagranicy” (domyślnie) albo odrzucenie (tryb „tylko z Polski”);
   po włączeniu ofert z zagranicy oferty odrzucone wcześniej za kraj wracają do wyników,
3. sprzedawcy seryjni: jeden sprzedawca z wieloma tanimi „iPhone'ami” → ukrycie jego ofert,
4. test ceny względem mediany rynkowej (``ListingFilter.check_price``).
"""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from ..core.language import detect_language
from ..core.listing_filter import FilterDecision, ListingFilter
from ..core.models import ParsedInfo, RawOffer, RedFlag
from ..core.normalizer import parse_offer
from ..core.settings import Settings
from ..sources import REGISTRY
from ..storage.repositories import (
    OfferRepository,
    RejectedRepository,
    SellerInfo,
    SellerRepository,
    SettingsRepository,
    raw_from_json,
)

log = logging.getLogger(__name__)

# zmiana logiki reguł (nie tylko ustawień) → ponowne sprawdzenie zapisanych ofert
RULES_VERSION = 2
_SIGNATURE_KEY = "filter_signature"


def seller_key(raw: RawOffer) -> str | None:
    """Identyfikator sprzedawcy z portalu (Vinted go podaje; Allegro Lokalnie i Sprzedajemy.pl — nie).

    Celowo bez zgadywania po tytule: ten sam tytuł w jednym mieście może pochodzić od różnych osób.
    """
    sid = raw.params.get("seller_id")
    return str(sid) if sid else None


def is_international(source: str) -> bool:
    cls = REGISTRY.get(source)
    return bool(cls and getattr(cls, "international", False))


@dataclass
class Prepared:
    raw: RawOffer
    parsed: ParsedInfo
    decision: FilterDecision


class OfferGuard:
    def __init__(self, conn: sqlite3.Connection, settings: Settings):
        self.conn = conn
        self.settings = settings
        self.rejected = RejectedRepository(conn)
        self.sellers = SellerRepository(conn)
        self.whitelist = self.rejected.whitelist()
        self.listing_filter = ListingFilter(settings.listing_filter, self.whitelist)
        self._medians: dict[tuple[str, int | None], float | None] = {}
        self.restored = 0  # ile ofert wróciło z odrzuconych przy ostatnim ``refilter_stored``

    # ------------------------------------------------------------ etapy ---

    def prepare(self, raw: RawOffer) -> Prepared:
        """Rozpoznanie + filtr tekstu + minimalna cena."""
        s = self.settings
        parsed = parse_offer(raw, battery_threshold=s.battery_health_threshold)
        decision = self.listing_filter.check(raw.title, model=parsed.model, category=raw.params.get("category"),
                                             source=raw.source, source_id=raw.source_id)
        if decision.accepted and raw.price < s.min_valid_price:
            decision = FilterDecision(False, "price", f"cena {raw.price:.0f} zł poniżej minimalnej "
                                                      f"({s.min_valid_price:.0f} zł) — zwykle „za darmo” lub zamiana")
        return Prepared(raw, parsed, decision)

    def whitelisted(self, raw: RawOffer) -> bool:
        return (raw.source, raw.source_id) in self.whitelist

    def foreign_reason(self, raw: RawOffer, known: dict[str, SellerInfo]) -> tuple[str, str] | None:
        """(powód, słowo-klucz), jeśli oferta pochodzi z zagranicy: język tytułu albo kraj sprzedawcy."""
        lang = detect_language(raw.title)
        if lang.foreign:
            return f"tytuł w obcym języku ({lang.language}: {lang.evidence})", lang.language or "język"
        info = known.get(str(raw.params.get("seller_id") or ""))
        if info and info.country_code and info.country_code != "PL":
            return f"sprzedawca spoza Polski (kraj z profilu: {info.country_code})", info.country_code
        return None

    def seller_decision(self, p: Prepared, known: dict[str, SellerInfo], serial: dict[str, str],
                        international: bool) -> FilterDecision | None:
        """Etapy sprzedawcy: seryjny → odrzuć; zagraniczny → odrzuć (tryb „tylko Polska”) lub oflaguj."""
        raw = p.raw
        if self.whitelisted(raw):
            return None
        key = seller_key(raw)
        if key and key in serial:
            return FilterDecision(False, "seller", f"sprzedawca seryjny — {serial[key]}",
                                  raw.params.get("seller") or None)
        if international:
            foreign = self.foreign_reason(raw, known)
            if foreign:
                if self.settings.vinted_country_mode == "pl":
                    return FilterDecision(False, "country", foreign[0], foreign[1])
                p.parsed.flags.append(RedFlag.FOREIGN_SELLER)
        return None

    def market_median(self, model: str, storage_gb: int | None) -> float | None:
        """Mediana cen modelu (ta sama pojemność, gdy jest dość danych) — do testów ceny."""
        key = (model, storage_gb)
        if key not in self._medians:
            obs = OfferRepository(self.conn).market_observations(model, self.settings.market_window_days)
            floor = self.settings.min_valid_price
            prices = [o.price for o in obs if o.price >= floor]
            same = [o.price for o in obs if o.storage_gb == storage_gb and o.price >= floor]
            use = same if len(same) >= 3 else prices
            self._medians[key] = statistics.median(use) if len(use) >= 3 else None
        return self._medians[key]

    def price_decision(self, p: Prepared) -> FilterDecision:
        if not p.parsed.model:
            return p.decision
        median = self.market_median(p.parsed.model, p.parsed.storage_gb)
        return self.listing_filter.check_price(p.raw.price, median, p.raw.description,
                                               source=p.raw.source, source_id=p.raw.source_id)

    # ------------------------------------------------- sprzedawcy seryjni ---

    def detect_serial(self, items: list[Prepared], source: str) -> dict[str, str]:
        """Sprzedawcy seryjni (zapamiętani wcześniej + nowo wykryci w tej partii). Nowych zapisuje w bazie
        i przenosi ich wcześniej zapisane oferty do odrzuconych."""
        cfg = self.settings.sanity
        if not cfg.serial_enabled:
            return {}
        found = {sid: info.serial_reason or "oznaczony wcześniej"
                 for (_, sid), info in self.sellers.serial_sellers(source).items()}
        groups: dict[str, list[Prepared]] = defaultdict(list)
        for p in items:
            key = seller_key(p.raw)
            if key and p.decision.accepted and p.parsed.model and not self.whitelisted(p.raw):
                groups[key].append(p)
        offers = OfferRepository(self.conn)
        for key, group in groups.items():
            if key in found:
                continue
            batch_ids = {p.raw.source_id for p in group}
            candidates = [(p.parsed.model, p.parsed.storage_gb, p.raw.price) for p in group]
            # oferty tego sprzedawcy zapisane przy poprzednich skanach
            candidates += [(o.parsed.model, o.parsed.storage_gb, o.price) for o in offers.by_seller(source, key)
                           if o.raw.source_id not in batch_ids and o.parsed.model]
            if len(candidates) < cfg.serial_min_offers:
                continue
            cheap = [price for model, storage, price in candidates
                     if (m := self.market_median(model, storage)) and price < m * cfg.serial_price_ratio]
            if len(cheap) < cfg.serial_min_offers:
                continue
            reason = (f"{len(cheap)} ofert „iPhone'ów” po {min(cheap):.0f}–{max(cheap):.0f} zł "
                      f"(poniżej {cfg.serial_price_ratio:.0%} wartości rynkowej)")
            login = group[0].raw.params.get("seller")
            self.sellers.mark_serial(source, key, reason, login)
            found[key] = reason
            log.info("Sprzedawca seryjny %s/%s: %s", source, login or key, reason)
            for o in offers.by_seller(source, key):
                if (o.raw.source, o.raw.source_id) not in self.whitelist and o.status.value != "watched":
                    self.rejected.reject_stored(o, "seller", f"sprzedawca seryjny — {reason}", login)
        return found

    # ------------------------------------- ponowne sprawdzenie zapisanych ---

    def signature(self) -> str:
        s = self.settings
        payload = {"rules": RULES_VERSION, "filter": json.loads(json.dumps(s.listing_filter.__dict__, default=str)),
                   "country": s.vinted_country_mode, "min_price": s.min_valid_price,
                   "serial": [s.sanity.serial_enabled, s.sanity.serial_min_offers, s.sanity.serial_price_ratio]}
        return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    def refilter_stored(self, *, force: bool = False) -> int:
        """Sprawdza zapisane aktywne oferty nowymi regułami; odrzucone trafiają do „Odrzucone”.

        Działa tylko, gdy reguły lub ustawienia filtra się zmieniły (albo ``force``). Zwraca liczbę
        przeniesionych ofert.
        """
        settings_repo = SettingsRepository(self.conn)
        sig = self.signature()
        if not force and settings_repo.get_value(_SIGNATURE_KEY) == sig:
            return 0
        offers = OfferRepository(self.conn)
        moved = 0
        in_tx = self.conn.in_transaction
        if not in_tx:
            self.conn.execute("BEGIN")
        try:
            stored = offers.list(include_hidden=True)
            by_source: dict[str, list[tuple[Prepared, object]]] = defaultdict(list)
            for offer in stored:
                if offer.status.value == "watched":
                    continue
                by_source[offer.raw.source].append((self.prepare(offer.raw), offer))
            for source, pairs in by_source.items():
                items = [p for p, _ in pairs]
                serial = self.detect_serial(items, source)
                known = self.sellers.get_many(source, [str(p.raw.params.get("seller_id") or "") for p in items])
                international = is_international(source)
                for p, offer in pairs:
                    decision = p.decision
                    if decision.accepted:
                        decision = self.seller_decision(p, known, serial, international) or decision
                    if not decision.accepted and not self.whitelisted(p.raw):
                        still_there = self.conn.execute("SELECT 1 FROM offers WHERE id = ?", (offer.id,)).fetchone()
                        if still_there:
                            self.rejected.reject_stored(offer, decision.stage, decision.reason, decision.keyword)
                            moved += 1
            if self.settings.vinted_country_mode != "pl":
                self.restored = self.restore_country_rejected()
            settings_repo.set_value(_SIGNATURE_KEY, sig)
            if not in_tx:
                self.conn.execute("COMMIT")
        except Exception:
            if not in_tx:
                self.conn.execute("ROLLBACK")
            raise
        if moved:
            log.info("Nowe reguły filtra: %d zapisanych ofert przeniesiono do odrzuconych", moved)
        if self.restored:
            log.info("Oferty z zagranicy: %d odrzuconych wcześniej ofert wróciło do wyników", self.restored)
        return moved

    def restore_country_rejected(self) -> int:
        """Oferty odrzucone wcześniej za kraj/język sprzedawcy → z powrotem do wyników (z flagą „z zagranicy”).

        Każda przechodzi pozostałe reguły (filtr tekstu, sprzedawcy seryjni, test ceny) tak jak przy skanie.
        Bez białej listy — to zmiana ustawień, nie Twoja poprawka. Ostatnie „widziano” = chwila odrzucenia,
        więc oferty dawno niewidziane na portalu znikną same (``offer_stale_days``)."""
        rows = self.conn.execute(
            "SELECT raw_json, rejected_at FROM rejected_offers WHERE stage = 'country'").fetchall()
        by_source: dict[str, list[tuple[Prepared, str]]] = defaultdict(list)
        for row in rows:
            raw = raw_from_json(row["raw_json"])
            by_source[raw.source].append((self.prepare(raw), row["rejected_at"]))
        offers = OfferRepository(self.conn)
        restored = 0
        for source, pairs in by_source.items():
            serial = {sid: info.serial_reason or "oznaczony wcześniej"
                      for (_, sid), info in self.sellers.serial_sellers(source).items()}
            known = self.sellers.get_many(source, [str(p.raw.params.get("seller_id") or "") for p, _ in pairs])
            for p, rejected_at in pairs:
                decision = p.decision
                if decision.accepted:
                    decision = self.seller_decision(p, known, serial, is_international(source)) or decision
                if decision.accepted:
                    decision = self.price_decision(p)
                if not decision.accepted:
                    if decision.stage != "country":  # inny powód odrzucenia — zapisz go
                        self.rejected.add(p.raw, decision.stage, decision.reason, decision.keyword)
                    continue
                if decision.suspicious:
                    p.parsed.flags.append(RedFlag.PRICE_UNREALISTIC)
                offers.upsert(p.raw, p.parsed, seen_at=_parse_time(rejected_at))
                self.rejected.remove(p.raw.source, p.raw.source_id)
                restored += 1
        return restored


def _parse_time(value: str | None) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None
