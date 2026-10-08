"""Profile powiadomień Telegram: dopasowanie ofert do filtrów, deduplikacja (oferta z kilku profili wysyłana raz),
migracja dotychczasowych ustawień, cisza i limit profilu, pauza, podgląd i test, komendy bota."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from phonebot.core.catalog import generation_of, generations
from phonebot.core.models import Condition, Mode, OfferStatus, RedFlag, Verdict
from phonebot.core.notify_profiles import NotifyProfile, describe, from_legacy, matches, reject_reason
from phonebot.services.telegram_bot import TelegramBot, parse_duration
from phonebot.services.telegram_queue import TelegramQueue, paused_until, set_pause
from phonebot.storage.db import open_database
from phonebot.storage.repositories import NotifyProfileRepository, OfferRepository

from .conftest import make_offer
from .test_sorting import _val
from .test_telegram_queue import NOON, FakeClient, _offer, _settings

# ------------------------------------------------------------------ filtry ---


def offer(title="iPhone 13 128GB", price=1000.0, **kw):
    o = make_offer(title, price, kw.pop("description", ""), **kw)
    o.distance_km = kw.get("distance", 20.0)
    return o


def test_empty_profile_lets_everything_but_risk_and_hard_flags():
    p = NotifyProfile(verdicts=[])
    assert matches(offer(), _val(Verdict.SKIP, 10), p)
    risky = _val(Verdict.BUY, 500)
    risky.risk = type("R", (), {"level": "medium", "label": "średnie"})()
    assert reject_reason(offer(), risky, p) == "ryzyko oszustwa średnie"  # domyślnie tylko niskie
    assert matches(offer(), risky, NotifyProfile(verdicts=[], max_risk="medium"))
    flagged = _val(Verdict.BUY, 500)
    flagged.flags = [RedFlag.ICLOUD_LOCK]
    assert reject_reason(offer(), flagged, p) == "poważna flaga"


@pytest.mark.parametrize("profile, o, val, reason", [
    (NotifyProfile(verdicts=["KUPUJ"]), offer(), _val(Verdict.NEGOTIATE, 300), "werdykt NEGOCJUJ"),
    (NotifyProfile(min_profit=200), offer(), _val(Verdict.BUY, 150), "zysk poniżej progu"),
    (NotifyProfile(min_profit=200), offer(), _val(Verdict.BUY, None), "zysk poniżej progu"),
    (NotifyProfile(min_score=70), offer(), _val(Verdict.BUY, 300, score=60), "ocena poniżej progu"),
    (NotifyProfile(models=["iPhone 14"]), offer(), _val(Verdict.BUY, 300), "model"),
    (NotifyProfile(storages=[256]), offer(), _val(Verdict.BUY, 300), "pamięć"),
    (NotifyProfile(price_min=1200), offer(), _val(Verdict.BUY, 300), "cena poniżej zakresu"),
    (NotifyProfile(price_max=900), offer(), _val(Verdict.BUY, 300), "cena powyżej zakresu"),
    (NotifyProfile(conditions=[Condition.DAMAGED.value]), offer(), _val(Verdict.BUY, 300), "stan"),
    (NotifyProfile(sources=["vinted"]), offer(), _val(Verdict.BUY, 300), "portal"),
    (NotifyProfile(shipping="no"), offer(), _val(Verdict.BUY, 300), "z wysyłką"),
    (NotifyProfile(shipping="yes"), offer(shipping_available=False), _val(Verdict.BUY, 300), "bez wysyłki"),
    (NotifyProfile(radius_km=10, radius_keeps_shipping=False), offer(), _val(Verdict.BUY, 300), "za daleko"),
    (NotifyProfile(only_with_parts=True), offer(), _val(Verdict.BUY, 300), "brak części w magazynie"),
    (NotifyProfile(exclude_words=["iCloud"]), offer(description="blokada icloud"), _val(Verdict.BUY, 300),
     "słowo „iCloud”"),
])
def test_each_filter_rejects(profile, o, val, reason):
    profile.verdicts = profile.verdicts if profile.verdicts != ["KUPUJ", "NEGOCJUJ"] else []
    assert reject_reason(o, val, profile) == reason


def test_filters_pass_and_special_cases():
    val = _val(Verdict.BUY, 300, score=80)
    val.profit_per_hour = 90.0
    val.parts_in_stock = ["screen"]
    p = NotifyProfile(models=["iPhone 13"], storages=[128], price_min=900, price_max=1100, min_profit=250,
                      min_profit_per_hour=60, min_score=75, conditions=[Condition.GOOD.value], sources=["test"],
                      radius_km=30, only_with_parts=True, exclude_words=["etui"], shipping="yes", country="pl")
    assert matches(offer(), val, p)
    far = offer()
    far.distance_km = 300
    assert matches(far, val, p)  # dalej, ale z wysyłką (radius_keeps_shipping)
    val.profit_per_hour = 30
    assert reject_reason(offer(), val, p) == "zysk na godzinę poniżej progu"
    foreign = _val(Verdict.BUY, 300)
    foreign.flags = [RedFlag.FOREIGN_SELLER]
    assert reject_reason(offer(), foreign, NotifyProfile(verdicts=[], country="pl")) == "z zagranicy"
    assert matches(offer(), foreign, NotifyProfile(verdicts=[], country="all"))
    assert reject_reason(offer(), _val(Verdict.BUY, 300), NotifyProfile(verdicts=[], require_picked=True),
                         picked=False) == "poza „Wybrane”"


def test_generations_and_description():
    assert generation_of("iPhone 13 Pro Max") == "13" and generation_of("iPhone XR") == "X"
    assert generation_of("iPhone SE (2020)") == "SE" and generation_of("iPhone 16e") == "16"
    assert generations()["15"] == ["iPhone 15", "iPhone 15 Plus", "iPhone 15 Pro", "iPhone 15 Pro Max"]
    p = NotifyProfile(models=[*generations()["13"], "iPhone 14 Pro"], radius_km=30, max_per_hour=3)
    text = describe(p)
    assert "13 (cała gen.)" in text and "14 Pro" in text and "do 30 km" in text and "≤ 3/h" in text
    assert "cena do 3000 zł" in describe(NotifyProfile(price_max=3000))
    assert "cena 800–3000 zł" in describe(NotifyProfile(price_min=800, price_max=3000))


# ------------------------------------------------------- kolejka i profile ---

@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "n.sqlite3")
    yield conn
    conn.close()


def _profiles(db, *profiles):
    repo = NotifyProfileRepository(db)
    repo.ensure_default(_settings())
    for p in repo.all():
        repo.delete(p.id)
    for p in profiles:
        repo.save(p)
    return repo


def test_legacy_settings_become_default_profile(db):
    s = _settings()
    s.telegram_criteria.min_profit = 333
    s.telegram_criteria.verdicts = ["KUPUJ"]
    repo = NotifyProfileRepository(db)
    repo.ensure_default(s)
    (p,) = repo.all()
    assert p.name == "Domyślny" and p.min_profit == 333 and p.verdicts == ["KUPUJ"] and p.require_picked
    assert p.max_risk == "medium"  # jak dotąd: pomijane tylko wysokie ryzyko
    repo.delete(p.id)
    repo.ensure_default(s)
    assert repo.all() == []  # migracja raz — usunięty profil nie wraca
    assert from_legacy(s).min_profit == 333


def test_offer_matching_several_profiles_sent_once_with_names(db):
    _profiles(db, NotifyProfile(name="Naprawa – blisko domu", verdicts=[], radius_km=50),
              NotifyProfile(name="Resell – iPhone 13–15", verdicts=[], models=[*generations()["13"]]),
              NotifyProfile(name="Wyłączony", enabled=False, verdicts=[]),
              NotifyProfile(name="Drogie", verdicts=[], price_min=5000))
    s = _settings()
    q = TelegramQueue(db, s)
    q.ensure_since(NOON - timedelta(days=1))
    oid = _offer(db, "a", 1000)
    db.execute("UPDATE offers SET first_seen = ?", (NOON.isoformat(),))

    def ev(o, mode=None):
        v = _val(Verdict.BUY, 400)
        o.distance_km = 20
        return v

    assert q.enqueue_scan([oid], [], ev, now=NOON) == 1
    assert q.enqueue_scan([oid], [], ev, now=NOON) == 0  # deduplikacja: drugi raz nie
    row = db.execute("SELECT * FROM telegram_outbox").fetchone()
    names = [r[0] for r in db.execute("SELECT p.name FROM telegram_outbox_profiles l JOIN notify_profiles p "
                                      "ON p.id = l.profile_id ORDER BY p.id")]
    assert names == ["Naprawa – blisko domu", "Resell – iPhone 13–15"]
    assert "📋 Profil: Naprawa – blisko domu · Resell – iPhone 13–15" in row["text"]
    client = FakeClient()
    q.flush(client, NOON)
    assert len(client.sent) == 1  # jedna wiadomość, choć pasuje do dwóch profili
    week = NotifyProfileRepository(db).sent_counts(NOON - timedelta(days=7))
    assert sorted(week.values()) == [1, 1] and len(week) == 2


def test_profile_mode_evaluates_offer_in_that_mode(db):
    _profiles(db, NotifyProfile(name="Resell", mode=Mode.RESELL.value, verdicts=["KUPUJ"]))
    s = _settings(mode=Mode.REPAIR.value)
    q = TelegramQueue(db, s)
    q.ensure_since(NOON - timedelta(days=1))
    oid = _offer(db, "m", 1000)
    db.execute("UPDATE offers SET first_seen = ?", (NOON.isoformat(),))
    seen = []

    def ev(o, mode=None):
        seen.append(mode)
        return _val(Verdict.BUY if mode is Mode.RESELL else Verdict.SKIP, 300)

    assert q.enqueue_scan([oid], [], ev, now=NOON) == 1 and seen == [Mode.RESELL]


def test_hidden_and_excluded_offers_never_sent(db):
    _profiles(db, NotifyProfile(name="Wszystko", verdicts=[], max_risk="high", skip_hard_flags=False))
    q = TelegramQueue(db, _settings())
    q.ensure_since(NOON - timedelta(days=1))
    hidden, excluded = _offer(db, "h"), _offer(db, "x")
    db.execute("UPDATE offers SET first_seen = ?", (NOON.isoformat(),))
    OfferRepository(db).set_status(hidden, OfferStatus.HIDDEN)
    db.execute("UPDATE offers SET pick_excluded = 1 WHERE id = ?", (excluded,))
    assert q.enqueue_scan([hidden, excluded], [], lambda o, m=None: _val(Verdict.BUY, 500), now=NOON) == 0


def _queued(db, q, sid, profiles, when=NOON):
    oid = _offer(db, sid)
    o = OfferRepository(db).get(oid)
    q.enqueue(o, _val(Verdict.BUY, 400), "new", now=when, profiles=profiles)
    return oid


def test_profile_quiet_hours_and_limit(db):
    repo = _profiles(db, NotifyProfile(name="Nocny", verdicts=[], own_quiet=True, quiet_enabled=True,
                                       quiet_start=0, quiet_end=23),  # cisza prawie cały dzień
                     NotifyProfile(name="Zawsze", verdicts=[], own_quiet=True, quiet_enabled=False, max_per_hour=1))
    night, always = repo.all()
    q = TelegramQueue(db, _settings())
    now = NOON.astimezone().replace(hour=12).astimezone(UTC)
    _queued(db, q, "n1", [night], now)  # tylko profil w ciszy — czeka
    _queued(db, q, "b1", [night, always], now)  # drugi profil pozwala — idzie
    _queued(db, q, "a2", [always], now)  # limit profilu „Zawsze” (1/h) już wyczerpany — czeka
    client = FakeClient()
    res = q.flush(client, now)
    assert res.sent == 1 and res.waiting == 2 and len(client.sent) == 1
    statuses = [r[0] for r in db.execute("SELECT status FROM telegram_outbox ORDER BY id")]
    assert statuses == ["pending", "sent", "pending"]  # cisza / wysłana przez drugi profil / limit profilu
    repo.set_enabled(night.id, False)  # wyłączony profil — jego zaległe wiadomości pomijane
    res = q.flush(client, now + timedelta(days=1))
    assert res.skipped == 1


def test_pause_skips_and_resume(db):
    _profiles(db, NotifyProfile(name="A", verdicts=[]))
    (p,) = NotifyProfileRepository(db).all()
    q = TelegramQueue(db, _settings())
    set_pause(db, NOON + timedelta(hours=2))
    assert paused_until(db, NOON) is not None and paused_until(db, NOON + timedelta(hours=3)) is None
    _queued(db, q, "p1", [p])
    res = q.flush(FakeClient(), NOON)
    assert res.paused == 1
    set_pause(db, None)
    _queued(db, q, "p2", [p])
    client = FakeClient()
    q.flush(client, NOON)
    assert len(client.sent) == 1  # z pauzy nie przyszło później


# ------------------------------------------------------------ podgląd, test ---

def test_preview_and_test_notification(db):
    from phonebot.services.notify_service import preview, send_test

    s = _settings()
    now = datetime.now(UTC)
    cheap, _pricey = _offer(db, "c", 700), _offer(db, "d", 3000)
    db.execute("UPDATE offers SET first_seen = ?", (now.isoformat(),))
    p = NotifyProfile(name="Tanie", verdicts=[], max_risk="high", skip_hard_flags=False, price_max=1000)
    pv = preview(db, s, p, now=now + timedelta(minutes=1))
    assert pv.count == 1 and pv.matched[0][0].id == cheap and pv.reasons["cena powyżej zakresu"] == 1
    assert pv.summary().startswith("Z ostatnich 24 godzin ten profil wysłałby 1 powiadomienie (z 2 nowych ofert)")
    client = FakeClient()
    msg = send_test(db, s, p, client, now=now + timedelta(minutes=1))
    assert "wysłano test" in msg and "TEST" in client.sent[0][0] and "Tanie" in client.sent[0][0]
    assert db.execute("SELECT COUNT(*) FROM telegram_outbox").fetchone()[0] == 0  # test nie trafia do kolejki
    none = NotifyProfile(name="Nic", verdicts=[], price_min=99999)
    send_test(db, s, none, client, now=now)
    assert "żadna oferta" in client.sent[-1][0]


# -------------------------------------------------------------------- bot ---

class BotClient(FakeClient):
    def __init__(self, updates=()):
        super().__init__()
        self.updates = list(updates)
        self.edits, self.answers, self.buttons = [], [], []

    def send(self, text, *, preview_url=None, buttons=None):
        self.sent.append((text, preview_url))
        self.buttons.append(buttons)
        return {"message_id": 1}

    def get_updates(self, offset, timeout=25):
        out, self.updates = self.updates, []
        return out

    def edit(self, chat_id, message_id, text, buttons=None):
        self.edits.append((chat_id, text, buttons))

    def answer_button(self, callback_id, text=""):
        self.answers.append(text)


def msg(text, chat=1, uid=1):
    return {"update_id": uid, "message": {"chat": {"id": chat}, "text": text}}


def test_parse_duration():
    assert parse_duration("2h") == timedelta(hours=2) and parse_duration("30m") == timedelta(minutes=30)
    assert parse_duration("1d") == timedelta(days=1) and parse_duration("1h30m") == timedelta(minutes=90)
    assert parse_duration("2") == timedelta(hours=2) and parse_duration("") is None and parse_duration("xyz") is None


def test_bot_commands_only_from_own_chat(db):
    _profiles(db, NotifyProfile(name="Naprawa", verdicts=[]), NotifyProfile(name="Resell", verdicts=[]))
    s = _settings()  # chat ID „1”
    client = BotClient([msg("/pauza 2h", chat=999, uid=5)])  # obcy czat — ignorowany
    bot = TelegramBot(db, s, client)
    assert bot.poll_once() == 1 and client.sent == [] and paused_until(db) is None
    client.updates = [msg("/pauza 2h", uid=6)]
    bot.poll_once()
    assert paused_until(db) is not None and "wstrzymane" in client.sent[-1][0]
    assert bot.handle(msg("/wznow")) == "wznow" and paused_until(db) is None and "wznowione" in client.sent[-1][0]
    assert bot.handle(msg("/pauza")) == "pauza" and paused_until(db) == "forever"
    assert bot.handle(msg("/pauza jutro")) == "pauza" and "Nie rozumiem" in client.sent[-1][0]
    bot.handle(msg("/wznow"))
    # /profile — przyciski; kliknięcie wyłącza profil i odświeża listę
    assert bot.handle(msg("/profile")) == "profile"
    buttons = client.buttons[-1]
    assert [b[0]["text"] for b in buttons] == ["✅ Naprawa", "✅ Resell"]
    pid = int(buttons[0][0]["callback_data"][2:])
    click = {"update_id": 9, "callback_query": {"id": "q", "data": f"p:{pid}",
                                                 "message": {"chat": {"id": 1}, "message_id": 7}}}
    assert bot.handle(click) == "toggle"
    assert not NotifyProfileRepository(db).get(pid).enabled and "wyłączony" in client.answers[-1]
    assert client.edits[-1][2][0][0]["text"] == "⛔ Naprawa"
    foreign_click = {**click, "callback_query": {**click["callback_query"], "message": {"chat": {"id": 5}}}}
    assert bot.handle(foreign_click) is None and NotifyProfileRepository(db).get(pid).enabled is False
    # /status
    from phonebot.storage.repositories import FetchRunRepository

    run = FetchRunRepository(db).start("vinted")
    FetchRunRepository(db).finish(run, found=10, new=3)
    bot.app_state = lambda: {"green": 4, "last_refresh": "12:00"}
    assert bot.handle(msg("/status")) == "status"
    text = client.sent[-1][0]
    assert "Vinted" in text and "3 nowych" in text and "zielonych: 4" in text and "włączone profile: 1" in text
    assert bot.handle(msg("cześć")) == "help" and "/pauza" in client.sent[-1][0]
    assert db.execute("SELECT value FROM settings WHERE key = 'telegram_update_offset'").fetchone()[0] == "7"
