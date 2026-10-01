"""Stare oferty: sprawdzanie stron ogłoszeń, archiwum i nocne pełne pobranie kontrolne.

* „Wybrane” i obserwowane — co godzinę: czy ogłoszenie istnieje i czy zmieniła się cena; w tym samym przebiegu
  okazje (werdykt KUPUJ / NEGOCJUJ z chwili pojawienia się), z osobnym limitem ``good_check_limit``.
* Otwarcie szczegółów oferty / powiadomienie Telegram czekające w kolejce: pojedyncza strona (``check_one``),
  jeśli nie sprawdzano jej od ``open_check_minutes`` minut.
* Pozostałe aktywne oferty — raz na dobę w nocy, najdawniej sprawdzane najpierw, z limitem stron na noc.
* Zniknięte (404/410, „sprzedane”, „zakończone”) → nieaktualne; nowa cena → historia cen i powiadomienie
  o obniżce (jak przy skanie).
* Strony ofert pobiera ``PageFetcher`` (ten sam limit zapytań na portal co wyszukiwanie; blokada wstrzymuje
  dany portal). Portale bez obsługi stron (Allegro API, eBay, Lento, OLX z maili) sprawdza nocne pełne pobranie:
  oferta niewidziana od ``offer_stale_days`` dni staje się nieaktualna.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from ..core.settings import Settings
from ..net.http import HostRateLimiter
from ..sources.pages import PageFetcher, page_supported
from ..storage.repositories import OfferRepository, utcnow
from .scanner import Scanner, ScanReport

log = logging.getLogger(__name__)


@dataclass
class CheckReport:
    checked: int = 0
    gone: int = 0
    price_changed: int = 0
    price_drop_ids: list[int] = field(default_factory=list)
    blocked: set[str] = field(default_factory=set)
    requests: int = 0
    archived: int = 0
    scan: ScanReport | None = None  # nocne pełne pobranie
    post: object = None


def check_pages(conn: sqlite3.Connection, settings: Settings, *, watched_only: bool, limit: int,
                fetcher: PageFetcher | None = None, older_than: datetime | None = None,
                stop=lambda: False, good_only: bool = False, rep: CheckReport | None = None) -> CheckReport:
    repo = OfferRepository(conn)
    rep = rep or CheckReport()
    own = fetcher is None
    fetcher = fetcher or PageFetcher(HostRateLimiter(settings.request_delay_s), stop=stop)
    try:
        for offer in repo.for_page_check(watched_only=watched_only, limit=limit, older_than=older_than,
                                         good_only=good_only):
            if stop():
                break
            if not page_supported(offer.raw.source) or offer.raw.source in fetcher.blocked_sources:
                continue
            res = fetcher.check(offer.raw.source, offer.raw.url)
            rep.requests += 1
            if res.blocked:
                rep.blocked.add(offer.raw.source)
                continue
            exists = False if res.gone else (None if res.error else True)
            old = offer.price
            outcome = repo.apply_page_check(offer.id, exists=exists, price=res.price, reason=res.reason)
            rep.checked += outcome != "unknown"
            if outcome == "gone":
                rep.gone += 1
            elif outcome == "price":
                rep.price_changed += 1
                if res.price is not None and res.price < old:
                    rep.price_drop_ids.append(offer.id)
    finally:
        if own:
            fetcher.close()
    if rep.checked:
        log.info("Sprawdzono %d stron ofert: %d zniknęło, %d zmian cen", rep.checked, rep.gone, rep.price_changed)
    return rep


def notify_drops(conn: sqlite3.Connection, settings: Settings, rep: CheckReport) -> None:
    """Obniżki cen znalezione na stronach ofert → powiadomienia (jak po skanie)."""
    if not rep.price_drop_ids:
        return
    from .post_scan import run_post_scan

    report = ScanReport(price_drop_ids=list(rep.price_drop_ids))
    try:
        rep.post = run_post_scan(conn, settings, report)
    except Exception:  # noqa: BLE001 — powiadomienia nie mogą zepsuć sprawdzania
        log.exception("Powiadomienia po sprawdzeniu ofert nie powiodły się")


def check_one(conn: sqlite3.Connection, settings: Settings, offer_id: int, *, fetcher: PageFetcher | None = None,
              force: bool = False, now: datetime | None = None) -> str:
    """Jedna oferta (otwarcie szczegółów, powiadomienie z kolejki): „gone” | „price” | „ok” | „unknown” |
    „skipped” (sprawdzana niedawno, portal bez obsługi stron albo oferta już nieaktualna)."""
    repo = OfferRepository(conn)
    offer = repo.get(offer_id)
    if offer is None or not offer.active or not page_supported(offer.raw.source):
        return "skipped"
    last = repo.checked_at(offer_id)
    if not force and last is not None and (now or utcnow()) - last < timedelta(
            minutes=max(1, settings.refresh.open_check_minutes)):
        return "skipped"
    own = fetcher is None
    fetcher = fetcher or PageFetcher(HostRateLimiter(settings.request_delay_s))
    try:
        res = fetcher.check(offer.raw.source, offer.raw.url)
    finally:
        if own:
            fetcher.close()
    if res.blocked:
        return "unknown"
    exists = False if res.gone else (None if res.error else True)
    return repo.apply_page_check(offer_id, exists=exists, price=res.price, now=now, reason=res.reason)


def check_one_in_background(db_path, settings: Settings, offer_id: int) -> tuple[int, str]:
    """Wątek roboczy: (id oferty, wynik)."""
    from ..storage.db import connect

    conn = connect(db_path)
    try:
        return offer_id, check_one(conn, settings, offer_id)
    finally:
        conn.close()


def watched_check(db_path, settings: Settings, stop=lambda: False) -> CheckReport:
    """Co godzinę (w tle): „Wybrane” i obserwowane."""
    from ..storage.db import connect

    conn = connect(db_path)
    try:
        cutoff = utcnow() - timedelta(minutes=max(5, settings.refresh.watch_check_minutes - 5))
        fetcher = PageFetcher(HostRateLimiter(settings.request_delay_s), stop=stop)
        try:
            rep = check_pages(conn, settings, watched_only=True, limit=200, older_than=cutoff, stop=stop,
                              fetcher=fetcher)
            if settings.refresh.good_check_limit > 0 and not stop():  # okazje — ten sam pobieracz (blokady, tempo)
                check_pages(conn, settings, watched_only=False, good_only=True, limit=settings.refresh.good_check_limit,
                            older_than=cutoff, stop=stop, fetcher=fetcher, rep=rep)
        finally:
            fetcher.close()
        notify_drops(conn, settings, rep)
        rep.archived = OfferRepository(conn).archive_old(settings.refresh.archive_days)
        return rep
    finally:
        conn.close()


def nightly(db_path, settings: Settings, limiter: HostRateLimiter, stop=lambda: False) -> CheckReport:
    """Raz na dobę (w nocy): pełne pobranie kontrolne wszystkich portali (wszystkie frazy i strony — wyłapuje
    oferty pominięte przez szybkie odświeżanie, oznacza zniknięte), potem strony pozostałych ofert i archiwum."""
    from ..storage.db import connect
    from .post_scan import run_post_scan

    conn = connect(db_path)
    try:
        report = asyncio.run(Scanner(conn, settings, limiter).run(force=False))
        try:
            report.post = run_post_scan(conn, settings, report)
        except Exception:  # noqa: BLE001
            log.exception("Powiadomienia po nocnym pobraniu nie powiodły się")
        cutoff = utcnow() - timedelta(hours=20)
        rep = check_pages(conn, settings, watched_only=False, limit=settings.refresh.nightly_page_checks,
                          older_than=cutoff, stop=stop)
        notify_drops(conn, settings, rep)
        rep.scan = report
        rep.requests += getattr(report, "requests", 0)
        rep.archived = OfferRepository(conn).archive_old(settings.refresh.archive_days)
        return rep
    finally:
        conn.close()
