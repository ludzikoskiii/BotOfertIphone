"""Zadanie 8: optymalizacje nie zmieniają wyników (pamięć podręczna oceny oszustw, zapytania, start w tle)."""
from __future__ import annotations

import random

from phonebot.core.fraud import FraudConfig, FraudContext, _text_signals, assess, owner_key
from phonebot.core.models import RawOffer
from phonebot.core.normalizer import parse_offer
from phonebot.core.settings import Settings
from phonebot.services.fraud_service import build_context
from phonebot.storage.db import open_database
from phonebot.storage.repositories import OfferRepository, PartsRepository, SettingsRepository

from .conftest import make_offer


def _db(path, n=60):
    conn = open_database(path)
    PartsRepository(conn).seed_defaults_if_empty()
    repo = OfferRepository(conn)
    rng = random.Random(3)
    for i in range(n):
        raw = RawOffer("vinted" if i % 2 else "lento", f"id{i}", f"https://p.test/{i}", "iPhone 13 128GB",
                       float(rng.randrange(800, 2500, 10)), description="Sprzedam telefon w dobrym stanie. " * 4,
                       photos=[f"https://img.test/{i}.jpg"], params={"seller_id": str(i % 7), "seller": f"u{i % 7}"})
        repo.upsert(raw, parse_offer(raw))
        # co piąte zdjęcie powtarza się u innego sprzedającego
        h = 0xABCDEF0123456789 if i % 5 == 0 else rng.getrandbits(64)
        conn.execute("INSERT INTO photo_hashes (source, source_id, url, dhash, stock, computed_at) "
                     "VALUES (?, ?, ?, ?, 0, ?)", (raw.source, raw.source_id, raw.photos[0], f"{h:016x}",
                                                  f"2026-09-26T10:00:{i:02d}+00:00"))
    return conn


def test_memoized_context_gives_same_result_as_fresh(tmp_path):
    conn = _db(tmp_path / "a.sqlite3")
    s = Settings()
    cached = build_context(conn, s)
    offers = OfferRepository(conn).list()
    first = [assess(o, 2000, cached, s.fraud) for o in offers]
    again = [assess(o, 2000, cached, s.fraud) for o in offers]  # z zapamiętanych porównań
    fresh = FraudContext(**{k: getattr(cached, k) for k in
                            ("descriptions", "photos", "photo_owner", "sellers", "expensive_by_seller")})
    plain = [assess(o, 2000, fresh, s.fraud) for o in offers]  # bez skrótów opisów i bez pamięci
    as_tuples = [[(x.key, x.points, x.detail) for x in a.signals] for a in first]
    assert as_tuples == [[(x.key, x.points, x.detail) for x in a.signals] for a in again]
    assert as_tuples == [[(x.key, x.points, x.detail) for x in a.signals] for a in plain]
    assert sum("duplicate_photo" in [x.key for x in a.signals] for a in first) >= 10


def test_context_cache_invalidated_when_data_changes(tmp_path):
    conn = _db(tmp_path / "b.sqlite3")
    s = Settings()
    ctx = build_context(conn, s)
    assert build_context(conn, s) is ctx  # te same dane → z pamięci
    conn.execute("UPDATE photo_hashes SET dhash = '0000000000000000', computed_at = '2026-09-27T00:00:00+00:00' "
                 "WHERE source_id = 'id1'")
    ctx2 = build_context(conn, s)
    assert ctx2 is not ctx and ctx2.photos[("vinted", "id1")][0] == 0
    s.fraud.expensive_price += 100  # zmiana ustawień też unieważnia
    assert build_context(conn, s) is not ctx2
    # dwie osobne bazy w pamięci nie dzielą kontekstu
    m1, m2 = open_database(":memory:"), open_database(":memory:")
    assert build_context(m1, s) is not build_context(m2, s)


def test_unmemoized_context_follows_manual_changes():
    o = make_offer("iPhone 13 128GB", 1800, source="vinted", params={"seller_id": "7", "seller": "jan"})
    key = (o.raw.source, o.raw.source_id)
    ctx = FraudContext(photos={key: (0xF0, False), ("lento", "9"): (0xF1, False)},
                       photo_owner={key: owner_key(o), ("lento", "9"): "lento:@gdansk"})
    assert ctx.duplicate_of(key, 7, owner_key(o)) == ("lento", "9")
    ctx.photo_owner[("lento", "9")] = owner_key(o)
    assert ctx.duplicate_of(key, 7, owner_key(o)) is None


def test_text_signals_cache_is_immutable():
    text = "iPhone 13\nKontakt WhatsApp +44 7700 900123, przedpłata blik"
    phones, contacts, prepay, *_ = _text_signals(text)
    assert isinstance(phones, tuple) and isinstance(contacts, tuple) and prepay
    assert _text_signals(text) is _text_signals(text)
    a = assess(make_offer("iPhone 13", 1800, "Kontakt WhatsApp +44 7700 900123, przedpłata blik"), 2000,
               FraudContext(), FraudConfig())
    assert {"contact_outside", "foreign_phone", "prepayment"} <= {x.key for x in a.signals}


def test_seller_price_points_matches_by_seller(tmp_path):
    conn = _db(tmp_path / "c.sqlite3", n=40)
    repo = OfferRepository(conn)
    points = repo.seller_price_points("vinted", [str(i) for i in range(7)] + ["brak"])
    for sid in map(str, range(7)):
        expected = [(o.raw.source_id, o.parsed.model, o.parsed.storage_gb, o.price)
                    for o in repo.by_seller("vinted", sid)]
        assert points[sid] == expected
    assert points["brak"] == []


def test_window_deferred_load(tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    from phonebot.ui.main_window import MainWindow

    path = tmp_path / "d.sqlite3"
    conn = _db(path, n=20)
    s = SettingsRepository(conn).load()
    s.enabled_sources.update({"lento": True, "vinted": True})
    SettingsRepository(conn).save(s)
    win = MainWindow(conn, path, thumbs_dir=tmp_path, defer_load=True)
    assert not win.loaded and win.model.rowCount() == 0  # okno gotowe od razu, oferty jeszcze nie
    for _ in range(20):
        QApplication.processEvents()
        if win.loaded:
            break
    assert win.loaded and win.model.rowCount() > 0
    eager = MainWindow(conn, path, thumbs_dir=tmp_path)
    assert eager.loaded and eager.model.rowCount() == win.model.rowCount()
    for w in (win, eager):
        w._quitting = True
        w.close()
