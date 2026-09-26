"""Kroki po pobraniu ofert: zielone oferty (powiadomienia Windows) i kolejka Telegram.

Wykonywane w wątku roboczym (bez GUI). Powiadomienia na pulpicie pokazuje GUI na podstawie
``PostScanResult.green``; Telegram — kolejka ``telegram_queue`` (nowe oferty z „Wybrane”, obniżki cen).
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field

from ..core.models import OfferStatus, RowColor, Verdict
from ..core.settings import Settings
from ..storage.repositories import OfferRepository
from .evaluator import Evaluator
from .notifications import NotificationError, TelegramClient, offer_headline
from .scanner import ScanReport
from .telegram_queue import TelegramQueue

log = logging.getLogger(__name__)


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
    telegram_sent: int = 0
    telegram_queued: int = 0
    telegram_waiting: int = 0
    telegram_error: str | None = None
    silent_first_scan: bool = False
    error: str | None = None  # nieoczekiwany błąd po skanie (wyniki skanu i tak są zapisane)


def run_post_scan(conn: sqlite3.Connection, settings: Settings, report: ScanReport, *,
                  telegram=None) -> PostScanResult:
    result = PostScanResult()
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
        # tylko czyste okazje: zielona ocena i werdykt KUPUJ/NEGOCJUJ (nigdy DO WERYFIKACJI)
        if val.color is RowColor.GREEN and val.verdict in (Verdict.BUY, Verdict.NEGOTIATE):
            greens.append((offer, val, reason))
    greens.sort(key=lambda x: x[1].expected_profit or 0, reverse=True)
    for offer, _, _ in greens:
        repo.mark_notified(offer.id)

    if report.first_scan:
        # pierwsze pobranie: wszystko jest „nowe” — nie zasypujemy powiadomieniami
        result.silent_first_scan = True
        if settings.telegram_enabled:
            TelegramQueue(conn, settings).ensure_since()  # oferty z tego pobrania nie trafią na Telegram
        return result

    result.green = [GreenOffer(o.id, offer_headline(o, v), v.expected_profit, o.raw.url, r) for o, v, r in greens]
    if settings.telegram_enabled:
        try:
            queue = TelegramQueue(conn, settings)
            result.telegram_queued = queue.enqueue_scan(report.new_offer_ids, report.price_drop_ids,
                                                        evaluator.evaluate)
            client = telegram or TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
            flushed = queue.flush(client)
            result.telegram_sent, result.telegram_waiting = flushed.sent, flushed.waiting
            result.telegram_error = flushed.error
        except NotificationError as e:
            log.warning("Telegram: %s", e)
            result.telegram_error = str(e)
    return result
