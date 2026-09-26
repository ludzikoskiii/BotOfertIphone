"""Zadanie 4: powiadomienia Telegram — kolejka bez duplikatów, kryteria, cisza nocna, limit, ponowienia."""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from phonebot.core.models import OfferStatus, Verdict
from phonebot.core.normalizer import parse_offer
from phonebot.core.settings import Settings
from phonebot.services.notifications import NotificationError
from phonebot.services.telegram_queue import TelegramQueue, format_offer, in_quiet_hours
from phonebot.storage.db import open_database
from phonebot.storage.repositories import OfferRepository, SettingsRepository

from .conftest import make_raw
from .test_sorting import _val

NOON = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)  # 12:00 w Polsce


class FakeClient:
    def __init__(self, fail: int = 0):
        self.sent: list[tuple[str, str | None]] = []
        self.fail = fail

    def send(self, text, *, preview_url=None):
        if self.fail:
            self.fail -= 1
            raise NotificationError("Telegram: błąd połączenia (ConnectError)")
        self.sent.append((text, preview_url))


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "t.sqlite3")
    yield conn
    conn.close()


def _settings(**kw):
    kw.setdefault("telegram_quiet_enabled", False)
    return Settings(telegram_enabled=True, telegram_bot_token="t", telegram_chat_id="1", **kw)


def _offer(db, sid, price=1000, title="iPhone 13 128GB"):
    raw = make_raw(title, price, source="allegro_lokalnie", source_id=sid, city="Nowy Targ",
                   photos=[f"https://img/{sid}.jpg"])
    return OfferRepository(db).upsert(raw, parse_offer(raw)).offer_id


def test_message_content_for_negotiation():
    from .test_stage3 import valued

    offer, val = valued("iPhone 13 128GB", 1650, "Bateria 83%, bez pudełka.")
    offer.distance_km = 24
    offer.raw.source = "vinted"
    assert val.verdict is Verdict.NEGOTIATE
    text = format_offer(offer, val, Settings(), details_url="http://pc:8765/oferta/5")
    for part in ("NEGOCJUJ", "<b>iPhone 13</b> · 128 GB", "1 650 zł", "Szacowany zysk", "Vinted", "(24 km)",
                 "Proponowana cena", "<pre>Dzień dobry", "Otwórz ogłoszenie", "/oferta/5"):
        assert part in text, part
    assert len(text) < 4096


def test_enqueue_rules(db):
    s = _settings()
    q = TelegramQueue(db, s)
    old = _offer(db, "old")  # sprzed włączenia powiadomień
    since = q.ensure_since(datetime.now(UTC) + timedelta(seconds=1))
    db.execute("UPDATE offers SET first_seen = ? WHERE id != ?", ((since + timedelta(minutes=1)).isoformat(), old))
    good, weak, excluded = _offer(db, "good"), _offer(db, "weak"), _offer(db, "excl")
    db.execute("UPDATE offers SET first_seen = ? WHERE id IN (?, ?, ?)",
               ((since + timedelta(minutes=1)).isoformat(), good, weak, excluded))
    OfferRepository(db).set_pick_excluded(excluded, True)
    vals = {"old": _val(Verdict.BUY, 500), "good": _val(Verdict.BUY, 500),
            "weak": _val(Verdict.BUY, 200),  # w „Wybrane” (≥150), ale nie spełnia ostrzejszych kryteriów (≥250)
            "excl": _val(Verdict.BUY, 500)}
    added = q.enqueue_scan([old, good, weak, excluded], [], lambda o: vals[o.raw.source_id])
    assert added == 1
    assert q.enqueue_scan([good], [], lambda o: vals[o.raw.source_id]) == 0  # każda oferta raz
    assert OfferRepository(db).get(good).picked_at is not None


def test_price_drop_once_per_price(db):
    s = _settings()
    q = TelegramQueue(db, s)
    q.ensure_since(NOON - timedelta(days=1))
    oid = _offer(db, "d", 1200)
    OfferRepository(db).set_status(oid, OfferStatus.WATCHED)  # ręcznie w „Wybrane”
    raw = make_raw("iPhone 13 128GB", 1100, source="allegro_lokalnie", source_id="d")
    OfferRepository(db).upsert(raw, parse_offer(raw))
    ev = lambda o: _val(Verdict.SKIP, -50)  # noqa: E731 — obniżka zgłaszana także dla ręcznie dodanych
    assert q.enqueue_scan([], [oid], ev) == 1 and q.enqueue_scan([], [oid], ev) == 0
    text = db.execute("SELECT text FROM telegram_outbox").fetchone()[0]
    assert "1 200 zł → <b>1 100 zł</b>" in text
    s.telegram_price_drops = False
    raw.price = 1000
    OfferRepository(db).upsert(raw, parse_offer(raw))
    assert q.enqueue_scan([], [oid], ev) == 0


def _fill(db, q, n):
    for i in range(n):
        oid = _offer(db, f"o{i}", 1000 + i)
        q.enqueue(OfferRepository(db).get(oid), _val(Verdict.BUY, 400), "new", now=NOON)


def test_hourly_limit_merges_overflow_into_summary(db):
    q = TelegramQueue(db, _settings(telegram_max_per_hour=5))
    _fill(db, q, 12)
    client = FakeClient()
    res = q.flush(client, NOON)
    assert res.sent == 5 and res.summarized == 8
    assert "Kolejne 8 ofert" in client.sent[-1][0]
    assert client.sent[0][1] == "https://img/o0.jpg"  # miniatura zdjęcia
    _fill(db, q, 14)  # 2 nowe (o12, o13) — limit na tę godzinę wyczerpany
    assert q.flush(client, NOON + timedelta(minutes=10)).waiting == 2
    assert q.flush(client, NOON + timedelta(minutes=61)).sent == 2
    assert q.stats() == {"sent": 6, "summarized": 8}


def test_quiet_hours_batch_or_skip(db):
    night = datetime(2026, 9, 26, 22, 30, tzinfo=UTC)  # 0:30 w Polsce
    s = _settings(telegram_quiet_enabled=True, telegram_quiet_start=22, telegram_quiet_end=7)
    assert in_quiet_hours(night.astimezone(), s) == (night.astimezone().hour >= 22 or night.astimezone().hour < 7)
    q = TelegramQueue(db, s)
    _fill(db, q, 2)
    client = FakeClient()
    if not in_quiet_hours(night.astimezone(), s):
        pytest.skip("strefa czasowa testu poza ciszą nocną")
    assert q.flush(client, night).waiting == 2 and client.sent == []
    morning = night.astimezone().replace(hour=7, minute=1) + (timedelta(days=1) if night.astimezone().hour >= 7 else
                                                              timedelta())
    assert q.flush(client, morning).sent == 2  # zebrane w nocy — rano
    s.telegram_quiet_mode = "skip"
    _fill(db, q, 4)
    assert q.flush(client, night + timedelta(days=1)).skipped == 2
    assert q.flush(client, morning + timedelta(days=1)).sent == 0


def test_retry_without_duplicates(db):
    q = TelegramQueue(db, _settings())
    _fill(db, q, 2)
    client = FakeClient(fail=1)
    res = q.flush(client, NOON)
    assert res.error and res.waiting == 2 and client.sent == []
    assert q.flush(client, NOON + timedelta(seconds=30)).sent == 0  # czeka na ponowienie
    assert q.flush(client, NOON + timedelta(minutes=2)).sent == 2
    assert q.flush(client, NOON + timedelta(minutes=10)).sent == 0
    assert len(client.sent) == 2


def test_token_stored_outside_settings_json(db):
    repo = SettingsRepository(db)
    repo.save(Settings(telegram_bot_token="123:SECRET", telegram_chat_id="42"))
    assert "123:SECRET" not in repo.get_value(repo.KEY) and '"42"' not in repo.get_value(repo.KEY)
    assert "SECRET" not in (repo.get_value("secret:telegram_bot_token") or "")
    loaded = repo.load()
    assert loaded.telegram_bot_token == "123:SECRET" and loaded.telegram_chat_id == "42"
    # starsza wersja trzymała token jawnie w JSON-ie — przeniesienie przy pierwszym odczycie
    db.execute("DELETE FROM settings WHERE key LIKE 'secret:%'")
    data = json.loads(repo.get_value(repo.KEY))
    data["telegram_bot_token"] = "999:OLD"
    repo.set_value(repo.KEY, json.dumps(data))
    assert repo.load().telegram_bot_token == "999:OLD"
    assert "999:OLD" not in repo.get_value(repo.KEY)
