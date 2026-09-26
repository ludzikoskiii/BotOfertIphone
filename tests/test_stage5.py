import json

import httpx
import pytest

from phonebot.core.settings import Settings
from phonebot.services.notifications import NotificationError, TelegramClient
from phonebot.services.post_scan import run_post_scan
from phonebot.services.scanner import ScanReport
from phonebot.services.telegram_queue import TelegramQueue
from phonebot.storage.repositories import OfferRepository

from .conftest import make_raw
from .sample_data import build_sample_db

# ------------------------------------------------------------ Telegram ---


def tg_transport(log, ok=True, updates=None):
    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        log.append((method, json.loads(request.content)))
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": updates or []})
        return httpx.Response(200, json={"ok": ok, "description": "Unauthorized" if not ok else None})
    return httpx.MockTransport(handler)


def test_telegram_send_and_errors():
    log = []
    TelegramClient("123:ABC", "42", transport=tg_transport(log)).send("<b>hej</b>")
    assert log == [("sendMessage", {"chat_id": "42", "text": "<b>hej</b>", "parse_mode": "HTML",
                                    "link_preview_options": {"is_disabled": True}})]
    with pytest.raises(NotificationError, match="Unauthorized"):
        TelegramClient("bad", "42", transport=tg_transport([], ok=False)).send("x")
    with pytest.raises(NotificationError, match="tokenu"):
        TelegramClient("  ")
    with pytest.raises(NotificationError, match="chat ID"):
        TelegramClient("t", "", transport=tg_transport([])).send("x")


def test_telegram_find_chat_id():
    updates = [{"update_id": 1, "message": {"chat": {"id": 555}, "text": "/start"}}]
    assert TelegramClient("t", transport=tg_transport([], updates=updates)).find_chat_id() == "555"
    with pytest.raises(NotificationError, match="/start"):
        TelegramClient("t", transport=tg_transport([])).find_chat_id()


# ------------------------------------------------------- po skanowaniu ---


def test_first_scan_is_silent(tmp_path):
    conn, report = build_sample_db(tmp_path / "db.sqlite3")
    assert report.first_scan and report.post is None
    post = run_post_scan(conn, Settings(telegram_enabled=True), report, telegram=object())
    assert post.silent_first_scan and post.green == []
    # zielone oferty są oznaczone jako „zgłoszone”, więc nie wrócą przy kolejnym skanie
    notified = conn.execute("SELECT COUNT(*) FROM offers WHERE notified_at IS NOT NULL").fetchone()[0]
    assert notified > 0


def test_new_green_offer_triggers_notifications(tmp_path):
    conn, _ = build_sample_db(tmp_path / "db.sqlite3")
    settings = Settings(telegram_enabled=True, telegram_bot_token="t", telegram_chat_id="1")
    TelegramQueue(conn, settings).ensure_since()  # powiadomienia włączone przed pojawieniem się ofert
    repo = OfferRepository(conn)
    from phonebot.core.normalizer import parse_offer

    raw = make_raw("iPhone 13 128GB zbity ekran", 600, description="zbity ekran, reszta sprawna",
                   source="allegro_lokalnie", source_id="NEW1", city="Nowy Targ")
    new_id = repo.upsert(raw, parse_offer(raw)).offer_id
    bad = make_raw("iPhone 13 128GB", 3000, source="allegro_lokalnie", source_id="NEW2")
    bad_id = repo.upsert(bad, parse_offer(bad)).offer_id
    log = []
    report = ScanReport(new_offer_ids=[new_id, bad_id])
    tg = TelegramClient("t", "1", transport=tg_transport(log))
    post = run_post_scan(conn, settings, report, telegram=tg)
    assert [g.offer_id for g in post.green] == [new_id]
    assert post.telegram_sent == 1 and "<b>iPhone 13</b> · 128 GB" in log[0][1]["text"]
    # drugi raz ta sama oferta nie jest zgłaszana
    again = run_post_scan(conn, settings, report, telegram=tg)
    assert again.green == []
