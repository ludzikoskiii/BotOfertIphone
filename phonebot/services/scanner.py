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
from datetime import timedelta

from ..core.filters import listing_rejection_reason
from ..core.models import RawOffer
from ..core.normalizer import parse_offer
from ..core.settings import Settings
from ..net.http import HostRateLimiter, HttpClient, ResponseCache
from ..sources import REGISTRY, SearchQuery, SourceAdapter, search_phrases
from ..storage.repositories import FetchRunRepository, OfferRepository, utcnow

log = logging.getLogger(__name__)

Progress = Callable[[str], None]


@dataclass
class SourceReport:
    key: str
    name: str
    found: int = 0
    saved: int = 0
    new: int = 0
    skipped: int = 0
    error: str | None = None
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

    async def run(self, progress: Progress | None = None) -> ScanReport:
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
            if not adapters:
                progress("Brak włączonych portali.")
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
            runs.finish(run_id, found=src_report.found, new=src_report.new, error=src_report.error)
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
        except Exception as e:  # izolacja awarii źródła
            log.exception("Błąd źródła %s", adapter.key)
            rep.error = str(e) or e.__class__.__name__
        if rep.error:
            progress(f"{adapter.display_name}: błąd — {rep.error}")
        rep.seconds = round(time.monotonic() - start, 1)
        return offers, rep

    def _store(self, raw_offers: list[RawOffer], rep: SourceReport, report: ScanReport) -> None:
        repo = OfferRepository(self.conn)
        threshold = self.settings.battery_health_threshold
        self.conn.execute("BEGIN")
        try:
            for raw in raw_offers:
                if listing_rejection_reason(raw.title) or raw.price < self.settings.min_valid_price:
                    rep.skipped += 1
                    continue
                parsed = parse_offer(raw, battery_threshold=threshold)
                if not parsed.model:
                    rep.skipped += 1
                    continue
                res = repo.upsert(raw, parsed)
                rep.saved += 1
                if res.is_new:
                    rep.new += 1
                    report.new_offer_ids.append(res.offer_id)
                elif res.price_changed and res.old_price and raw.price < res.old_price:
                    report.price_drop_ids.append(res.offer_id)
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
