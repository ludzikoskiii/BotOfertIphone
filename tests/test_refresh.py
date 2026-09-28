"""Zadanie 4: odświeżanie przyrostowe — harmonogram per portal, tylko nowe oferty, blokady, stare oferty,
archiwum, znacznik „NOWE”."""
from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from phonebot.core.models import Mode, OfferStatus
from phonebot.core.refresh import RefreshConfig, RefreshScheduler, is_new
from phonebot.core.settings import Settings
from phonebot.net.http import HostRateLimiter, HttpClient
from phonebot.services.offer_checks import check_pages
from phonebot.services.scanner import Scanner
from phonebot.sources.allegro_lokalnie import AllegroLokalnieAdapter
from phonebot.sources.base import SearchQuery
from phonebot.sources.pages import PageResult
from phonebot.storage.db import open_database
from phonebot.storage.repositories import OfferRepository

from .conftest import make_raw

FIX = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)


# ---------------------------------------------------------------- harmonogram ---

def sched(**kw):
    cfg = RefreshConfig(**kw)
    return RefreshScheduler(cfg, ["vinted", "allegro_lokalnie", "olx"], T0, rng=random.Random(1), first_delay_s=0)


def test_each_portal_independent_with_jitter():
    s = sched()
    assert set(s.due(T0 + timedelta(seconds=15))) == {"vinted", "allegro_lokalnie", "olx"}  # start rozłożony
    for src in ("vinted", "allegro_lokalnie"):
        s.started(src)
    assert s.due(T0 + timedelta(seconds=20)) == ["olx"]  # w toku — nie drugi raz
    s.record("vinted", "ok", T0)
    delay = (s.states["vinted"].next_due - T0).total_seconds()
    assert 100 <= delay <= 140  # 2 min ±20 s
    delays = set()
    for _ in range(5):
        s.record("vinted", "ok", T0)
        delays.add(round((s.states["vinted"].next_due - T0).total_seconds()))
    assert len(delays) > 1  # nie w równym rytmie
    # wolny portal (wciąż „w toku”) nie wstrzymuje pozostałych
    s.record("olx", "ok", T0)
    assert "vinted" in s.due(T0 + timedelta(minutes=3)) and "allegro_lokalnie" not in s.due(T0 + timedelta(minutes=3))


def test_backoff_after_blocks_and_recovery():
    s = sched(jitter_s=0)
    intervals = []
    for _ in range(4):
        s.record("vinted", "blocked", T0)
        intervals.append(s.interval("vinted") // 60)
    assert intervals == [5, 15, 60, 60]  # 2 → 5 → 15 → 60 min
    assert s.badge("vinted") == "co 60 min" and "blokuje" in s.describe("vinted", T0)
    s.record("vinted", "network", T0)  # błąd sieci nie resetuje ani nie wydłuża
    assert s.interval("vinted") == 3600
    s.record("vinted", "ok", T0)
    assert s.interval("vinted") == 120 and s.badge("vinted") == "" and "co ~2 min" in s.describe("vinted", T0)


def test_min_interval_per_portal():
    s = sched(jitter_s=0, min_interval_s={"olx": 300, "vinted": 240})
    assert s.interval("olx") == 300 and s.interval("vinted") == 240 and s.interval("allegro_lokalnie") == 120


def test_periodic_jobs():
    s = sched(nightly_hour=3, watch_check_minutes=60)
    assert s.watch_due(T0, None) and not s.watch_due(T0, T0 - timedelta(minutes=30))
    assert s.watch_due(T0, T0 - timedelta(minutes=61))
    night = datetime(2026, 9, 29, 3, 5)
    assert s.nightly_due(night, None) and s.nightly_due(night, datetime(2026, 9, 28, 3, 1))
    assert not s.nightly_due(night, datetime(2026, 9, 29, 3, 1))  # raz na dobę
    assert not s.nightly_due(datetime(2026, 9, 29, 2, 59), None)


def test_page_done_stops_on_known():
    q = SearchQuery(mode=Mode.RESELL, known={"1", "2", "3"}, known_stop=3)
    assert not q.page_done(["9", "8", "1"]) and q.page_done(["9", "1", "2", "3"]) and q.page_done(["1", "2"])
    assert not SearchQuery(mode=Mode.RESELL).page_done(["1", "2", "3"])  # pełne pobranie


# ------------------------------------------------------ skan: tylko nowe oferty ---

PAGES = {"1": (FIX / "allegro_lokalnie_search_p1.html").read_text(encoding="utf-8"),
         "2": (FIX / "allegro_lokalnie_search_p2.html").read_text(encoding="utf-8")}


def portal(requests: list):
    def handler(request):
        requests.append(str(request.url))
        page = request.url.params.get("page", "1")
        return httpx.Response(200, text=PAGES["1" if page == "1" else "2"])  # dalsze strony = te same ogłoszenia
    return handler


def scanner(conn, handler, **settings_kw):
    s = Settings(mode=Mode.RESELL.value, max_pages_per_query=3, watched_models=["iPhone 13", "iPhone 12"],
                 **settings_kw)
    limiter = HostRateLimiter(0)
    return Scanner(conn, s, limiter,
                   http_factory=lambda: HttpClient(limiter, transport=httpx.MockTransport(handler), wait=lambda x: 0),
                   adapter_factory=lambda http, st: [AllegroLokalnieAdapter(http, st)])


def test_incremental_fetches_only_until_known(tmp_path):
    conn = open_database(tmp_path / "r.sqlite3")
    full_req, quick_req = [], []
    full = asyncio.run(scanner(conn, portal(full_req)).run(force=True))
    assert full.sources[0].saved == 12 and len(full_req) >= 4  # 3 frazy, kolejne strony
    quick = asyncio.run(scanner(conn, portal(quick_req)).run(incremental=True))
    assert len(quick_req) == 1 and quick.requests == 1  # jedna fraza, jedna strona — dalej już znane
    assert quick.incremental and quick.new_count == 0


def test_incremental_does_not_mark_others_inactive(tmp_path):
    conn = open_database(tmp_path / "r.sqlite3")
    repo = OfferRepository(conn)
    raw = make_raw("iPhone 11 64GB", 900, source="allegro_lokalnie", source_id="old1")
    from phonebot.core.normalizer import parse_offer

    repo.upsert(raw, parse_offer(raw), seen_at=datetime.now(UTC) - timedelta(days=30))
    asyncio.run(scanner(conn, portal([])).run(incremental=True))
    assert repo.get(repo.list()[0].id) is not None
    assert any(o.raw.source_id == "old1" and o.active for o in repo.list(include_archived=True))
    asyncio.run(scanner(conn, portal([])).run(force=True))  # pełne pobranie oznacza zniknięte
    assert not any(o.raw.source_id == "old1" for o in repo.list(include_archived=True))


# --------------------------------------------------------- stare oferty, archiwum ---

@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "a.sqlite3")
    yield conn
    conn.close()


def add(conn, sid, days_ago, price=1500.0, source="vinted"):
    from phonebot.core.normalizer import parse_offer

    raw = make_raw("iPhone 13 128GB", price, source=source, source_id=sid, url=f"https://www.vinted.pl/items/{sid}")
    res = OfferRepository(conn).upsert(raw, parse_offer(raw), seen_at=datetime.now(UTC) - timedelta(days=days_ago))
    return res.offer_id


def test_archive_keeps_watched_and_picked_and_stats(db):
    repo = OfferRepository(db)
    old, watched, picked, fresh = add(db, "o", 5, 1500), add(db, "w", 5, 1510), add(db, "p", 5, 1520), add(db, "f", 1, 1530)
    db.execute("UPDATE offers SET last_seen = ? WHERE id IN (?, ?, ?, ?)",
               (datetime.now(UTC).isoformat(), old, watched, picked, fresh))
    repo.set_status(watched, OfferStatus.WATCHED)
    repo.mark_picked([picked])
    assert repo.archive_old(3) == 1
    shown = {o.raw.source_id for o in repo.list()}
    assert shown == {"w", "p", "f"}
    assert "o" in {o.raw.source_id for o in repo.list(include_archived=True)}
    assert len(repo.market_observations("iPhone 13", 30)) == 4  # archiwalne dalej w statystykach i wycenie


class FakeFetcher:
    def __init__(self, results):
        self.results = results
        self.blocked_sources = set()
        self.urls = []

    def check(self, source, url):
        self.urls.append(url)
        return self.results[url.rsplit("/", 1)[-1]]

    def close(self):
        pass


def test_page_checks_mark_gone_and_record_price_drop(db):
    repo = OfferRepository(db)
    gone, cheaper, same, blocked = add(db, "g", 0), add(db, "c", 0), add(db, "s", 0), add(db, "b", 0)
    for oid in (gone, cheaper, same, blocked):
        repo.set_status(oid, OfferStatus.WATCHED)
    fetcher = FakeFetcher({"g": PageResult(gone=True), "c": PageResult(price=1300.0), "s": PageResult(price=1500.0),
                           "b": PageResult(blocked=True, error="HTTP 403")})
    rep = check_pages(db, Settings(), watched_only=True, limit=50, fetcher=fetcher)
    assert rep.gone == 1 and rep.price_changed == 1 and rep.price_drop_ids == [cheaper] and rep.blocked == {"vinted"}
    assert not repo.get(gone).active and repo.get(cheaper).price == 1300.0
    history = [r[0] for r in db.execute("SELECT price FROM price_history WHERE offer_id = ? ORDER BY id", (cheaper,))]
    assert history == [1500.0, 1300.0]
    # sprawdzone niedawno — przy kolejnym przebiegu pominięte
    fetcher.urls.clear()
    check_pages(db, Settings(), watched_only=True, limit=50, fetcher=fetcher,
                older_than=datetime.now(UTC) - timedelta(minutes=55))
    assert fetcher.urls == [f"https://www.vinted.pl/items/{s}" for s in ("b",)] or len(fetcher.urls) <= 1


def test_page_price_parsing():
    from phonebot.sources.pages import page_price_state

    ld = '<script type="application/ld+json">{"offers":{"price":"1299.00","availability":"%s"}}</script>'
    assert page_price_state(ld % "https://schema.org/InStock") == (1299.0, False)
    assert page_price_state(ld % "https://schema.org/SoldOut") == (1299.0, True)
    assert page_price_state("<h1>Ogłoszenie zostało zakończone</h1>")[1]


# ------------------------------------------------------------------ okno ---

def test_new_badge(db, tmp_path):
    from PySide6.QtWidgets import QApplication

    from phonebot.ui.main_window import MainWindow
    from phonebot.ui.table_model import NEW_MARK, Col

    QApplication.instance() or QApplication([])
    assert is_new(datetime.now(UTC) - timedelta(minutes=10), datetime.now(UTC), 30)
    assert not is_new(datetime.now(UTC) - timedelta(minutes=40), datetime.now(UTC), 30)
    add(db, "n", 0)
    add(db, "o", 0.5)
    win = MainWindow(db, tmp_path / "a.sqlite3", thumbs_dir=tmp_path)
    texts = {win.model.row_at(r)[0].raw.source_id: win.model.data(win.model.index(r, Col.ADDED))
             for r in range(win.model.rowCount())}
    assert texts["n"].startswith(NEW_MARK) and not texts["o"].startswith(NEW_MARK)
    win._quitting = True
    win.close()


def test_window_runs_portals_independently(db, tmp_path, monkeypatch):
    import time

    from PySide6.QtWidgets import QApplication

    from phonebot.services.scanner import ScanReport, SourceReport
    from phonebot.ui import workers
    from phonebot.ui.main_window import MainWindow

    app = QApplication.instance() or QApplication([])
    calls = []

    class FakeScanner:
        def __init__(self, *a, **kw):
            pass

        async def run(self, progress, force=False, sources=None, incremental=False):
            src = next(iter(sources))
            calls.append((src, incremental))
            if src == "vinted":
                await asyncio.sleep(1.0)  # wolny portal
                return ScanReport(sources=[SourceReport(src, src, kind="blocked", error="HTTP 403")])
            return ScanReport(sources=[SourceReport(src, src, found=3, saved=3, new=1)], new_offer_ids=[])

    monkeypatch.setattr(workers, "Scanner", FakeScanner)
    monkeypatch.setattr(workers, "run_post_scan", lambda *a, **k: None)
    s = Settings()
    s.enabled_sources = {k: k in ("vinted", "allegro_lokalnie") for k in s.enabled_sources}
    from phonebot.storage.repositories import SettingsRepository

    SettingsRepository(db).save(s)
    win = MainWindow(db, tmp_path / "a.sqlite3", thumbs_dir=tmp_path)
    win.start_refresh_scheduler()
    win.schedule_timer.stop()
    for st in win.scheduler.states.values():
        st.next_due = datetime.now(UTC) - timedelta(seconds=1)
    # tylko nowe oferty w tym teście: sprawdzanie starych i nocne pobranie niedawno wykonane
    win._last_watch_check = datetime.now(UTC)
    win.settings_repo.set_value("nightly_last", datetime.now(UTC).isoformat())
    win._schedule_tick()
    assert set(win._quick) == {"vinted", "allegro_lokalnie"}
    deadline = time.time() + 10
    while "allegro_lokalnie" in win._quick and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert "vinted" in win._quick  # wolny portal jeszcze pracuje, szybki już skończył
    assert win.scheduler.interval("allegro_lokalnie") == 120
    while win._quick and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert not win._quick and all(inc for _, inc in calls)
    assert win.scheduler.interval("vinted") == 300  # blokada → 5 min
    from phonebot.storage.repositories import FetchRunRepository

    runs = FetchRunRepository(db)
    runs.finish(runs.start("vinted"), found=0, new=0, error="HTTP 403", status="blocked")
    win.refresh_source_status()
    assert "co 5 min" in win.source_status.text_of("vinted")
    assert "wydłużony" in win.source_status._labels["vinted"].toolTip()
    win._quitting = True
    win.close()
