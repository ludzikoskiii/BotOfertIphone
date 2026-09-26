"""Pobieranie ofert ze wszystkich włączonych portali równolegle.

Każdy adapter działa w osobnym zadaniu asyncio z limitem czasu; wyjątek
jednego źródła jest zapisywany w raporcie i nie przerywa pozostałych.
Skaner działa w wątku roboczym i ma własne połączenie z bazą.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import statistics
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.listing_filter import FilterDecision, ListingFilter
from ..core.models import RawOffer, RedFlag
from ..core.normalizer import parse_offer
from ..core.settings import Settings
from ..net.http import HostRateLimiter, HttpClient, ResponseCache
from ..sources import REGISTRY, SearchQuery, SourceAdapter, search_phrases
from ..storage.repositories import FetchRunRepository, OfferRepository, RejectedRepository, utcnow

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

        runs = FetchRunRepository(self.conn)
        for adapter, (raw_offers, src_report) in zip(adapters, results, strict=True):
            run_id = runs.start(adapter.key)
            if raw_offers:
                self._store(raw_offers, src_report, report)
                OfferRepository(self.conn).deactivate_missing(
                    adapter.key, utcnow() - timedelta(days=s.offer_stale_days)
                )
            if src_report.ok and src_report.saved == 0:
                src_report.kind = "empty"  # działa technicznie, ale nic nie zwrócił — możliwa zmiana formatu
            runs.finish(run_id, found=src_report.found, new=src_report.new, error=src_report.error,
                        status=src_report.kind)
            report.sources.append(src_report)
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

    def _market_median(self, model: str, storage_gb: int | None, cache: dict) -> float | None:
        """Mediana cen modelu (ta sama pojemność, gdy jest dość danych) — do testu ceny."""
        key = (model, storage_gb)
        if key not in cache:
            obs = OfferRepository(self.conn).market_observations(model, self.settings.market_window_days)
            prices = [o.price for o in obs if o.price >= self.settings.min_valid_price]
            same = [o.price for o in obs if o.storage_gb == storage_gb and o.price >= self.settings.min_valid_price]
            use = same if len(same) >= 3 else prices
            cache[key] = statistics.median(use) if len(use) >= 3 else None
        return cache[key]

    def _store(self, raw_offers: list[RawOffer], rep: SourceReport, report: ScanReport) -> None:
        repo = OfferRepository(self.conn)
        rejected = RejectedRepository(self.conn)
        listing_filter = ListingFilter(self.settings.listing_filter, rejected.whitelist())
        threshold = self.settings.battery_health_threshold
        medians: dict = {}
        self.conn.execute("BEGIN")
        try:
            for raw in raw_offers:
                parsed = parse_offer(raw, battery_threshold=threshold)
                decision = listing_filter.check(raw.title, model=parsed.model, category=raw.params.get("category"),
                                                source=raw.source, source_id=raw.source_id)
                if decision.accepted and raw.price < self.settings.min_valid_price:
                    decision = FilterDecision(False, "price", f"cena {raw.price:.0f} zł poniżej minimalnej "
                                                              f"({self.settings.min_valid_price:.0f} zł) — "
                                                              "zwykle „za darmo” lub zamiana")
                if decision.accepted and parsed.model:
                    median = self._market_median(parsed.model, parsed.storage_gb, medians)
                    decision = listing_filter.check_price(raw.price, median, raw.description,
                                                          source=raw.source, source_id=raw.source_id)
                if not decision.accepted:
                    rejected.add(raw, decision.stage, decision.reason, decision.keyword)
                    rep.skipped += 1
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
