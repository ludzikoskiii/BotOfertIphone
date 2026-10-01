"""Sprzedane / zarezerwowane ogłoszenia: rozpoznawanie na stronie (Vinted), sprawdzanie okazji co godzinę,
sprawdzenie przy otwarciu szczegółów, pomijanie zaległych powiadomień o sprzedanych ofertach."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from phonebot.core.settings import Settings
from phonebot.services.offer_checks import check_one, check_pages
from phonebot.sources.pages import PageResult, page_price_state, vinted_state
from phonebot.storage.db import open_database
from phonebot.storage.repositories import OfferRepository

from .test_refresh import FakeFetcher, add

# fragmenty prawdziwych stron Vinted (sonda scripts/probe_sold.py, 1.10.2026): dane przedmiotu na końcu strony,
# cudzysłowy poprzedzone ukośnikiem; strona ma kod 200 także dla sprzedanych i zarezerwowanych
RESERVED = (r'''\"is_hated\":false,\"hates_you\":false,\"can_buy\":false,'''
    r'''\"instant_buy\":false,\"is_reserved\":true,\"is_hidden\":false}''')
SOLD = (r'''\"is_hated\":false,\"hates_you\":false,\"can_buy\":false,'''
    r'''\"instant_buy\":false,\"is_reserved\":false,\"is_hidden\":false}''')
AVAILABLE = (r'''\"is_hated\":false,\"hates_you\":false,\"can_buy\":true,'''
    r'''\"instant_buy\":true,\"is_reserved\":false,\"is_hidden\":false}''')
# napisy z tłumaczeń, które Vinted wstawia na KAŻDĄ stronę przedmiotu (także dostępnego)
TRANSLATIONS = r'''\"flash_messages.no_longer_available_sold.title\":\"Przedmiot został sprzedany\",'''


def test_vinted_state_from_item_data():
    assert vinted_state("x" * 1000 + RESERVED) == "reserved"
    assert vinted_state(SOLD) == "sold"
    assert vinted_state(AVAILABLE) == "available"
    assert vinted_state(TRANSLATIONS) is None
    assert vinted_state('"hates_you":false,"can_buy":true,"instant_buy":true,"is_reserved":false') == "available"


def test_fetcher_check_vinted_uses_item_data_not_translations(monkeypatch):
    import httpx

    from phonebot.net.http import HostRateLimiter
    from phonebot.sources.pages import PageFetcher

    pages = {"/items/1": TRANSLATIONS + "x" * 300_000 + SOLD, "/items/2": TRANSLATIONS + AVAILABLE,
             "/items/3": TRANSLATIONS + "x" * 500_000 + RESERVED, "/items/4": TRANSLATIONS, "/items/5": ""}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/items/5":
            return httpx.Response(404)
        return httpx.Response(200, text=pages[request.url.path])

    f = PageFetcher(HostRateLimiter(0), client=httpx.Client(transport=httpx.MockTransport(handler)))
    try:
        sold = f.check("vinted", "https://www.vinted.pl/items/1")  # flagi daleko za 160 kB — czytana cała strona
        assert sold.gone and sold.reason == "sold"
        ok = f.check("vinted", "https://www.vinted.pl/items/2")
        assert not ok.gone and ok.error is None  # napis z tłumaczeń nie oznacza sprzedaży
        res = f.check("vinted", "https://www.vinted.pl/items/3")
        assert res.gone and res.reason == "reserved"
        unknown = f.check("vinted", "https://www.vinted.pl/items/4")
        assert not unknown.gone and unknown.error  # brak danych przedmiotu — nie ukrywamy oferty
        removed = f.check("vinted", "https://www.vinted.pl/items/5")
        assert removed.gone and removed.reason == "removed"
    finally:
        f.close()
    assert page_price_state("<h1>Ogłoszenie zostało zakończone</h1>")[1]  # inne portale: napisy nadal działają


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "s.sqlite3")
    yield conn
    conn.close()


def test_hourly_check_includes_good_offers_and_records_reason(db):
    repo = OfferRepository(db)
    good, skip, _other = add(db, "g", 0), add(db, "s", 0), add(db, "o", 0)
    repo.set_first_verdict(good, "KUPUJ")
    repo.set_first_verdict(skip, "ODPUŚĆ")
    fetcher = FakeFetcher({"g": PageResult(gone=True, reason="reserved"), "s": PageResult(), "o": PageResult()})
    rep = check_pages(db, Settings(), watched_only=False, good_only=True, limit=40, fetcher=fetcher)
    assert fetcher.urls == ["https://www.vinted.pl/items/g"] and rep.gone == 1  # tylko okazje
    offer = repo.get(good)
    assert not offer.active and offer.inactive_reason == "reserved"
    # oferta znów w wynikach wyszukiwania (rezerwacja anulowana) — wraca jako aktywna, bez powodu
    from phonebot.core.normalizer import parse_offer

    from .conftest import make_raw

    raw = make_raw("iPhone 13 128GB", 1500, source="vinted", source_id="g", url="https://www.vinted.pl/items/g")
    repo.upsert(raw, parse_offer(raw))
    assert repo.get(good).active and repo.get(good).inactive_reason is None


def test_watched_check_runs_good_offers_with_limit(db, tmp_path, monkeypatch):
    from phonebot.services import offer_checks

    db.close()
    conn = open_database(tmp_path / "w.sqlite3")
    ids = [add(conn, f"k{i}", 0) for i in range(5)]
    for oid in ids:
        OfferRepository(conn).set_first_verdict(oid, "NEGOCJUJ")
    conn.close()
    fake = FakeFetcher({f"k{i}": PageResult() for i in range(5)})
    monkeypatch.setattr(offer_checks, "PageFetcher", lambda *a, **k: fake)
    s = Settings()
    s.refresh.good_check_limit = 3
    offer_checks.watched_check(tmp_path / "w.sqlite3", s)
    assert len(fake.urls) == 3  # limit stron okazji na godzinę


def test_check_one_respects_interval_and_marks_gone(db):
    repo = OfferRepository(db)
    oid = add(db, "x", 0)
    fetcher = FakeFetcher({"x": PageResult(gone=True, reason="sold")})
    now = datetime.now(UTC)
    db.execute("UPDATE offers SET checked_at = ? WHERE id = ?", ((now - timedelta(minutes=10)).isoformat(), oid))
    assert check_one(db, Settings(), oid, fetcher=fetcher, now=now) == "skipped" and fetcher.urls == []
    assert check_one(db, Settings(), oid, fetcher=fetcher, now=now + timedelta(hours=2)) == "gone"
    assert repo.get(oid).inactive_reason == "sold"
    assert check_one(db, Settings(), oid, fetcher=fetcher, force=True) == "skipped"  # już nieaktualna
    unsupported = add(db, "e", 0)
    db.execute("UPDATE offers SET source = 'ebay' WHERE id = ?", (unsupported,))
    assert check_one(db, Settings(), unsupported, fetcher=fetcher) == "skipped"


def test_telegram_skips_queued_message_when_offer_sold(db):
    from phonebot.services.telegram_queue import TelegramQueue

    oid_sold, oid_ok, oid_fresh = add(db, "a", 0), add(db, "b", 0), add(db, "c", 0)
    now = datetime.now(UTC)
    for oid, age in ((oid_sold, 180), (oid_ok, 180), (oid_fresh, 1)):
        db.execute("INSERT INTO telegram_outbox (offer_id, kind, price, headline, text, next_try, created_at) "
                   "VALUES (?, 'new', 1500, 'h', ?, ?, ?)", (oid, f"oferta {oid}",
                                                           (now - timedelta(minutes=age)).isoformat(),
                                                           (now - timedelta(minutes=age)).isoformat()))

    class Client:
        sent: list[str] = []

        def send(self, text, preview_url=None):
            self.sent.append(text)

    checked = []

    def checker(offer_id):
        checked.append(offer_id)
        return "gone" if offer_id == oid_sold else "ok"

    s = Settings(telegram_enabled=True, telegram_bot_token="t", telegram_chat_id="1", telegram_quiet_enabled=False)
    client = Client()
    res = TelegramQueue(db, s).flush(client, now, checker=checker)
    assert res.gone == 1 and res.sent == 2 and f"oferta {oid_sold}" not in client.sent
    assert sorted(checked) == sorted([oid_sold, oid_ok])  # świeża wiadomość bez sprawdzania strony
    row = db.execute("SELECT status, error FROM telegram_outbox WHERE offer_id = ?", (oid_sold,)).fetchone()
    assert row["status"] == "skipped" and "sprzedana" in row["error"]


def test_window_checks_opened_offer_and_shows_reason(tmp_path, monkeypatch):
    import time

    from PySide6.QtWidgets import QApplication

    from phonebot.core.models import OfferStatus
    from phonebot.services import offer_checks
    from phonebot.ui.details_html import build_details_html
    from phonebot.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    conn = open_database(tmp_path / "u.sqlite3")
    oid = add(conn, "z", 0)
    OfferRepository(conn).set_status(oid, OfferStatus.WATCHED)  # obserwowana: zostaje w „Wybrane” jako ⌛
    win = MainWindow(conn, tmp_path / "u.sqlite3", thumbs_dir=tmp_path)
    try:
        calls = []

        def fake_check(db_path, settings, offer_id):  # zamiast strony portalu: rezerwacja
            calls.append(offer_id)
            c = open_database(db_path)
            OfferRepository(c).apply_page_check(offer_id, exists=False, price=None, reason="reserved")
            c.close()
            return offer_id, "gone"

        monkeypatch.setattr(offer_checks, "check_one_in_background", fake_check)
        win.check_offer_page(oid, delay=False)
        deadline = time.monotonic() + 5
        while (win._open_check_worker is not None or not calls) and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        QApplication.processEvents()
        assert calls == [oid] and win._open_check_worker is None
        assert "ogłoszenie zniknęło z portalu" in win._status.text()
        offer, val = win.model.row_at(win.model.row_of(oid))
        assert not offer.active and offer.inactive_reason == "reserved"
        assert "⌛ Nieaktualna — zarezerwowana" in build_details_html(offer, val, win.settings)
    finally:
        win._quitting = True
        win.close()
        conn.close()
