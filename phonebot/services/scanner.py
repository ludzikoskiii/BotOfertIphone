"""Pobieranie ofert ze wszystkich włączonych portali równolegle.

Każdy adapter działa w osobnym zadaniu asyncio z limitem czasu; wyjątek
jednego źródła jest zapisywany w raporcie i nie przerywa pozostałych.
Skaner działa w wątku roboczym i ma własne połączenie z bazą.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.language import detect_language
from ..core.models import RawOffer, RedFlag
from ..core.settings import Settings
from ..net.http import HostRateLimiter, HttpClient, ResponseCache
from ..sources import REGISTRY, SearchQuery, SourceAdapter, search_phrases
from ..sources.base import SellerProfile
from ..storage.repositories import FetchRunRepository, OfferRepository, SellerRepository, utcnow
from .offer_guard import OfferGuard

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


@dataclass
class SourceReport:
    key: str
    name: str
    found: int = 0
    saved: int = 0
    new: int = 0
    skipped: int = 0  # odrzucone przez filtr ogłoszeń (lista: widok „Odrzucone oferty”)
    foreign: int = 0  # odrzucone jako oferty z zagranicy (Vinted)
    serial: int = 0  # odrzucone jako oferty sprzedawców seryjnych
    suspicious: int = 0  # przyjęte, ale z podejrzanie niską ceną
    error: str | None = None
    kind: str = "ok"  # ok | empty | error | network | blocked | changed | timeout
    seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return self.error is None


@dataclass
class ScanReport:
    sources: list[SourceReport] = field(default_factory=list)
    new_offer_ids: list[int] = field(default_factory=list)
    price_drop_ids: list[int] = field(default_factory=list)
    first_scan: bool = False  # baza była pusta przed tym skanem
    post: object = None  # PostScanResult (uzupełnia worker)

    @property
    def new_count(self) -> int:
        return len(self.new_offer_ids)


class Scanner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        settings: Settings,
        limiter: HostRateLimiter,
        cache: ResponseCache | None = None,
        *,
        adapter_factory: Callable[[HttpClient, Settings], list[SourceAdapter]] | None = None,
        http_factory: Callable[[], HttpClient] | None = None,
    ):
        self.conn = conn
        self.settings = settings
        self.limiter = limiter
        self.cache = cache
        self._adapter_factory = adapter_factory or default_adapters
        self._http_factory = http_factory or (lambda: HttpClient(limiter, cache))

    def _cooldowns(self) -> dict[str, datetime]:
        """Portale zablokowane niedawno → do kiedy automat ma ich nie odpytywać."""
        until: dict[str, datetime] = {}
        minutes = self.settings.blocked_cooldown_minutes
        if minutes <= 0:
            return until
        for key, row in FetchRunRepository(self.conn).latest_by_source().items():
            if row["status"] == "blocked" and row["finished_at"]:
                end = datetime.fromisoformat(row["finished_at"]) + timedelta(minutes=minutes)
                if end > utcnow():
                    until[key] = end
        return until

    async def run(self, progress: Progress | None = None, *, force: bool = False) -> ScanReport:
        """``force=True`` (ręczne „Odśwież”) pomija pauzę po blokadzie."""
        progress = progress or (lambda _msg: None)
        s = self.settings
        query = SearchQuery(
            mode=s.mode_enum,
            phrases=search_phrases(s.watched_models, s.mode_enum),
            price_min=s.price_min or None,
            price_max=s.price_max or None,
            max_pages=s.max_pages_per_query,
        )
        report = ScanReport()
        report.first_scan = self.conn.execute("SELECT COUNT(*) FROM offers").fetchone()[0] == 0
        OfferGuard(self.conn, s).refilter_stored()  # nowe reguły → sprawdź też oferty zapisane wcześniej
        async with self._http_factory() as http:
            adapters = self._adapter_factory(http, s)
            paused = {} if force else self._cooldowns()
            for a in [a for a in adapters if a.key in paused]:
                until = paused[a.key].astimezone()
                report.sources.append(SourceReport(
                    a.key, a.display_name, kind="blocked",
                    error=f"portal zablokował pobieranie — pauza do {until:%H:%M} (ręczne „Odśwież” pomija pauzę)"))
            adapters = [a for a in adapters if a.key not in paused]
            if not adapters:
                progress("Brak portali do odpytania." if report.sources else "Brak włączonych portali.")
                return report
            progress("Pobieranie: " + ", ".join(a.display_name for a in adapters))
            results = await asyncio.gather(*(self._run_adapter(a, query, progress) for a in adapters))
            profiles = await self._lookup_sellers(adapters, results, progress)

        self._save_profiles(profiles)
        runs = FetchRunRepository(self.conn)
        for adapter, (raw_offers, src_report) in zip(adapters, results, strict=True):
            run_id = runs.start(adapter.key)
            if raw_offers:
                self._store(raw_offers, src_report, report, international=adapter.international)
                OfferRepository(self.conn).deactivate_missing(
                    adapter.key, utcnow() - timedelta(days=s.offer_stale_days)
                )
            if src_report.ok and src_report.saved == 0:
                src_report.kind = "empty"  # działa technicznie, ale nic nie zwrócił — możliwa zmiana formatu
            runs.finish(run_id, found=src_report.found, new=src_report.new, error=src_report.error,
                        status=src_report.kind)
            report.sources.append(src_report)
        # porządki: stare nieaktywne oferty nie są już potrzebne do wyceny (okno rynkowe × 2)
        purged = OfferRepository(self.conn).purge_inactive(max(s.market_window_days * 2, 60))
        if purged:
            log.info("Usunięto %d dawno nieaktywnych ofert", purged)
        progress(_summary(report))
        return report

    async def _run_adapter(
        self, adapter: SourceAdapter, query: SearchQuery, progress: Progress
    ) -> tuple[list[RawOffer], SourceReport]:
        rep = SourceReport(adapter.key, adapter.display_name)
        start = time.monotonic()
        offers: list[RawOffer] = []
        try:
            offers = await asyncio.wait_for(adapter.search(query), timeout=self.settings.source_timeout_s)
            rep.found = len(offers)
            progress(f"{adapter.display_name}: pobrano {len(offers)} ofert")
        except TimeoutError:
            rep.error = f"przekroczono limit czasu ({self.settings.source_timeout_s:.0f} s)"
            rep.kind = "timeout"
        except Exception as e:  # izolacja awarii źródła
            log.exception("Błąd źródła %s", adapter.key)
            rep.error = str(e) or e.__class__.__name__
            rep.kind = getattr(e, "kind", "error")
        if rep.error:
            progress(f"{adapter.display_name}: błąd — {rep.error}")
        rep.seconds = round(time.monotonic() - start, 1)
        return offers, rep

    async def _lookup_sellers(self, adapters: list[SourceAdapter], results: list, progress: Progress
                              ) -> dict[str, dict[str, SellerProfile]]:
        """Kraj sprzedawców (portale międzynarodowe): tylko dla ofert, które przeszły filtr tekstu,
        tylko nieznanych sprzedawców i z limitem zapytań na skan — reszta przy kolejnych skanach."""
        s = self.settings
        limit = max(0, int(s.seller_lookups_per_scan))
        out: dict[str, dict[str, SellerProfile]] = {}
        if not limit:
            return out
        guard = OfferGuard(self.conn, s)
        sellers = SellerRepository(self.conn)
        for adapter, (raw_offers, _rep) in zip(adapters, results, strict=True):
            if not adapter.international or not raw_offers:
                continue
            ids = []
            for raw in raw_offers:
                sid = raw.params.get("seller_id")
                if not sid or (s.vinted_country_mode == "pl" and detect_language(raw.title).foreign):
                    continue  # kraj nieistotny: oferta i tak odpadnie
                if guard.prepare(raw).decision.accepted:
                    ids.append(str(sid))
            need = sellers.needs_country(adapter.key, ids)[:limit]
            if not need:
                continue
            progress(f"{adapter.display_name}: sprawdzanie kraju {len(need)} sprzedawców…")
            budget = min(120.0, float(s.source_timeout_s))
            try:
                out[adapter.key] = await asyncio.wait_for(
                    adapter.seller_countries(need, deadline=time.monotonic() + budget), timeout=budget + 30)
            except Exception as e:  # brak kraju nie może zepsuć skanu
                log.warning("%s: nie udało się sprawdzić sprzedawców: %s", adapter.display_name, e)
        return out

    def _save_profiles(self, profiles: dict[str, dict[str, SellerProfile]]) -> None:
        if not any(profiles.values()):
            return
        sellers = SellerRepository(self.conn)
        self.conn.execute("BEGIN")
        try:
            for source, by_id in profiles.items():
                for seller_id, prof in by_id.items():
                    sellers.save_country(source, seller_id, prof.country_code, login=prof.login,
                                         business=prof.business)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def _store(self, raw_offers: list[RawOffer], rep: SourceReport, report: ScanReport, *,
               international: bool = False) -> None:
        repo = OfferRepository(self.conn)
        guard = OfferGuard(self.conn, self.settings)
        rejected = guard.rejected
        source = raw_offers[0].source
        self.conn.execute("BEGIN")
        try:
            items = [guard.prepare(raw) for raw in raw_offers]
            serial = guard.detect_serial(items, source)
            known = guard.sellers.get_many(source, [str(p.raw.params.get("seller_id") or "") for p in items])
            for p in items:
                raw, parsed, decision = p.raw, p.parsed, p.decision
                if decision.accepted:
                    decision = guard.seller_decision(p, known, serial, international) or decision
                if decision.accepted:
                    decision = guard.price_decision(p)
                if not decision.accepted:
                    rejected.add(raw, decision.stage, decision.reason, decision.keyword)
                    rep.skipped += 1
                    rep.foreign += decision.stage == "country"
                    rep.serial += decision.stage == "seller"
                    continue
                if decision.suspicious:
                    parsed.flags.append(RedFlag.PRICE_UNREALISTIC)
                    rep.suspicious += 1
                rejected.remove(raw.source, raw.source_id)
                res = repo.upsert(raw, parsed)
                rep.saved += 1
                if res.is_new:
                    rep.new += 1
                    report.new_offer_ids.append(res.offer_id)
                elif res.price_changed and res.old_price and raw.price < res.old_price:
                    report.price_drop_ids.append(res.offer_id)
            rejected.purge_older_than(max(self.settings.market_window_days, 14))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise


def default_adapters(http: HttpClient, settings: Settings) -> list[SourceAdapter]:
    adapters = []
    for key, cls in REGISTRY.items():
        if settings.enabled_sources.get(key, True):
            adapters.append(cls(http, settings))  # type: ignore[call-arg]
    return adapters


def _summary(report: ScanReport) -> str:
    parts = []
    for s in report.sources:
        parts.append(f"{s.name}: {'błąd' if s.error else f'{s.saved} ofert ({s.new} nowych)'}")
    return "Zakończono. " + "; ".join(parts)
