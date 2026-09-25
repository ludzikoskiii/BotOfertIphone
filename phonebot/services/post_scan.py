"""Kroki po pobraniu ofert: analiza AI nowych ofert, wybór zielonych, powiadomienia Telegram.

Wykonywane w wątku roboczym (bez GUI). Powiadomienia na pulpicie pokazuje GUI
na podstawie ``PostScanResult.green``.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from ..core.models import OfferStatus, RowColor
from ..core.settings import Settings
from ..storage.repositories import OfferRepository
from .ai_analysis import AiAnalysisError, AiListing, ClaudeAnalyzer
from .evaluator import Evaluator
from .notifications import NotificationError, offer_headline, send_telegram_batch
from .scanner import ScanReport

log = logging.getLogger(__name__)

AI_BATCH = 10


@dataclass
class GreenOffer:
    offer_id: int
    headline: str
    profit: float | None
    url: str
    reason: str  # "nowa" / "obniżka ceny"


@dataclass
class PostScanResult:
    green: list[GreenOffer] = field(default_factory=list)
    ai_analyzed: int = 0
    ai_error: str | None = None
    telegram_sent: int = 0
    telegram_error: str | None = None
    silent_first_scan: bool = False


def run_ai(conn: sqlite3.Connection, settings: Settings, offer_ids: list[int], analyzer=None) -> tuple[int, str | None]:
    if not settings.llm_enabled:
        return 0, None
    repo = OfferRepository(conn)
    rows = repo.pending_ai(offer_ids, settings.llm_max_per_scan)
    if not rows:
        return 0, None
    analyzer = analyzer or ClaudeAnalyzer(settings.anthropic_api_key, settings.llm_model)
    done = 0
    for i in range(0, len(rows), AI_BATCH):
        batch = rows[i:i + AI_BATCH]
        listings = [AiListing(str(r["id"]), r["title"], r["description"]) for r in batch]
        try:
            findings = analyzer.analyze(listings)
        except AiAnalysisError as e:
            log.warning("Analiza AI: %s", e)
            return done, str(e)
        for r in batch:
            f = findings.get(str(r["id"]))
            if f is not None:
                repo.save_ai(int(r["id"]), f.defects, f.flags, f.note)
                done += 1
    log.info("Analiza AI: przeanalizowano %d ofert", done)
    return done, None


def run_post_scan(conn: sqlite3.Connection, settings: Settings, report: ScanReport, *,
                  analyzer=None, telegram=None) -> PostScanResult:
    result = PostScanResult()
    result.ai_analyzed, result.ai_error = run_ai(conn, settings, report.new_offer_ids, analyzer)

    candidates = [(i, "nowa") for i in report.new_offer_ids]
    if settings.notify_price_drops:
        candidates += [(i, "obniżka ceny") for i in report.price_drop_ids]
    repo = OfferRepository(conn)
    evaluator = Evaluator(conn, settings)
    greens = []
    for offer_id, reason in candidates:
        offer = repo.get(offer_id)
        if offer is None or offer.status is OfferStatus.HIDDEN:
            continue
        if reason == "nowa" and repo.was_notified(offer_id):
            continue
        val = evaluator.evaluate(offer)
        if val.color is RowColor.GREEN:
            greens.append((offer, val, reason))
    greens.sort(key=lambda x: x[1].expected_profit or 0, reverse=True)
    for offer, _, _ in greens:
        repo.mark_notified(offer.id)

    if report.first_scan:
        # pierwsze pobranie: wszystko jest „nowe” — nie zasypujemy powiadomieniami
        result.silent_first_scan = True
        return result

    result.green = [GreenOffer(o.id, offer_headline(o, v), v.expected_profit, o.raw.url, r) for o, v, r in greens]
    if greens and settings.telegram_enabled:
        try:
            result.telegram_sent = send_telegram_batch(settings, greens, telegram)
        except NotificationError as e:
            log.warning("Telegram: %s", e)
            result.telegram_error = str(e)
    return result
