"""Zadanie „sprawdzenie i optymalizacja”: porządki w bazie (VACUUM, stare przebiegi), limit miniatur, status źródeł z indeksu,
wycena przekazywana z okna na telefon, panel „Wydajność”, zapis filtra przy wpisywaniu."""
from __future__ import annotations

import os
import random
import time
from datetime import UTC, datetime, timedelta

import pytest

from phonebot.core.fraud import FraudContext
from phonebot.core.settings import Settings
from phonebot.services import maintenance
from phonebot.storage.db import open_database
from phonebot.storage.repositories import FetchRunRepository, SettingsRepository

from .test_refresh import add


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "p.sqlite3")
    yield conn
    conn.close()


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _iso(dt):
    return dt.isoformat()


def test_latest_run_per_source_uses_index(db):
    now = datetime.now(UTC)
    rows = []
    for i in range(300):
        for src in ("vinted", "allegro_lokalnie"):
            rows.append((src, _iso(now - timedelta(minutes=300 - i)), _iso(now - timedelta(minutes=300 - i)), "ok"))
    db.executemany("INSERT INTO fetch_runs (source, started_at, finished_at, status) VALUES (?, ?, ?, ?)", rows)
    db.execute("INSERT INTO fetch_runs (source, started_at, status) VALUES ('vinted', ?, 'running')", (_iso(now),))
    latest = FetchRunRepository(db).latest_by_source()
    expected = {r["source"]: r["id"] for r in db.execute(  # dawne zapytanie (pełny przegląd tabeli) — ten sam wynik
        "SELECT * FROM fetch_runs WHERE id IN (SELECT MAX(id) FROM fetch_runs WHERE finished_at IS NOT NULL "
        "GROUP BY source)")}
    assert {k: r["id"] for k, r in latest.items()} == expected
    plan = " ".join(r[3] for r in db.execute(
        "EXPLAIN QUERY PLAN SELECT * FROM fetch_runs WHERE source = 'vinted' AND finished_at IS NOT NULL "
        "ORDER BY id DESC LIMIT 1"))
    assert "idx_fetch_runs_source" in plan
    assert FetchRunRepository(open_database(":memory:")).latest_by_source() == {}


def test_prune_tables_keeps_what_program_reads(db):
    now = datetime.now(UTC)
    old, fresh = _iso(now - timedelta(days=45)), _iso(now - timedelta(days=1))
    db.executemany("INSERT INTO fetch_runs (source, started_at, finished_at, status) VALUES (?, ?, ?, 'ok')",
                   [("vinted", old, old), ("vinted", fresh, fresh), ("lento", old, old)])
    kept = add(db, "kept", 1)
    db.executemany("INSERT INTO telegram_outbox (offer_id, kind, price, headline, text, status, next_try, created_at) "
                   "VALUES (?, 'new', ?, 'h', 't', ?, ?, ?)",
                   [(kept, 1, "sent", old, _iso(now - timedelta(days=120))),
                    (kept, 2, "pending", old, _iso(now - timedelta(days=120))),
                    (kept, 3, "sent", fresh, fresh)])
    for sid in ("kept", "gone"):
        db.execute("INSERT INTO ai_results (source, source_id, text_label) VALUES ('vinted', ?, 'phone')", (sid,))
        db.execute("INSERT INTO photo_hashes (source, source_id, url, dhash, computed_at) VALUES ('vinted', ?, 'u', "
                   "'ab', ?)", (sid, fresh))
    deleted = maintenance.prune_tables(db, now)
    assert deleted == {"fetch_runs": 1, "telegram_outbox": 1, "ai_results": 1, "photo_hashes": 1}
    # ostatni przebieg każdego portalu zostaje (status źródeł), także stary (portal wyłączony)
    assert set(FetchRunRepository(db).latest_by_source()) == {"vinted", "lento"}
    assert db.execute("SELECT COUNT(*) FROM telegram_outbox WHERE status = 'pending'").fetchone()[0] == 1
    assert db.execute("SELECT source_id FROM ai_results").fetchall()[0][0] == "kept"


def test_vacuum_shrinks_database_and_report_is_saved(db, tmp_path):
    db.execute("CREATE TABLE junk (x TEXT)")
    db.executemany("INSERT INTO junk VALUES (?)", [("x" * 2000,) for _ in range(5000)])
    db.execute("DROP TABLE junk")
    before = maintenance.db_file_mb(db)
    assert maintenance.free_pct(db)[0] > 50
    rep = maintenance.nightly_maintenance(db, Settings())
    assert rep["vacuumed"] and rep["db_mb_after"] < before / 3
    assert maintenance.last_report(db)["at"] == rep["at"]
    again = maintenance.nightly_maintenance(db, Settings())
    assert not again["vacuumed"]  # mało wolnego miejsca — bez VACUUM


def test_maintenance_due_once_a_day_at_night(db):
    tz = datetime.now().astimezone().tzinfo
    night = datetime(2026, 10, 8, 3, 30, tzinfo=tz)
    assert maintenance.maintenance_due(db, night, 3)  # jeszcze nigdy
    SettingsRepository(db).set_value(maintenance.KEY, f'{{"at": "{(night - timedelta(hours=1)).isoformat()}"}}')
    assert not maintenance.maintenance_due(db, night, 3)  # dziś już były
    assert not maintenance.maintenance_due(db, night + timedelta(hours=10), 3)  # w dzień — czeka do nocy
    assert maintenance.maintenance_due(db, night + timedelta(days=1), 3)  # następna noc
    assert maintenance.maintenance_due(db, night + timedelta(days=3, hours=10), 3)  # komputer nocą wyłączony


def test_cache_dir_limit_removes_least_recently_used(tmp_path):
    now = time.time()
    for i in range(10):
        f = tmp_path / f"{i}_72.jpg"
        f.write_bytes(b"x" * 100_000)
        os.utime(f, (now - i * 3600, now - i * 3600))  # 0 = używany przed chwilą, 9 = najdawniej
    stale = tmp_path / "old_320.jpg"
    stale.write_bytes(b"x")
    os.utime(stale, (now - 40 * 86400, now - 40 * 86400))
    removed, mb = maintenance.prune_cache_dir(tmp_path, max_mb=0.5, now=now)
    left = sorted(int(p.name.split("_")[0]) for p in tmp_path.glob("*_72.jpg"))
    assert removed == 1 + 5 and left == [0, 1, 2, 3, 4] and mb <= 0.5
    assert maintenance.cache_size_mb(tmp_path) == pytest.approx(mb)


def test_similar_photos_same_as_comparing_all(qapp):
    rng = random.Random(3)
    ctx = FraudContext(memoize=True)
    base = [rng.getrandbits(64) for _ in range(40)]
    for i in range(600):  # skupiska prawie takich samych zdjęć + losowe
        h = base[i % 40] ^ (1 << rng.randrange(64)) if i % 3 else rng.getrandbits(64)
        ctx.photos[("vinted", str(i))] = (h, False)
        ctx.photo_owner[("vinted", str(i))] = f"seller{i % 7}"
    for key, (h0, _) in list(ctx.photos.items())[:150]:
        brute = {k for k, (h, _) in ctx.photos.items() if k != key and (h ^ h0).bit_count() <= 6}
        found = ctx.similar_photos(key, 6)
        assert set(found) == brute and len(found) == len(brute)
        dup = ctx.duplicate_of(key, 6, ctx.photo_owner[key])
        others = [k for k in found if ctx.photo_owner[k] != ctx.photo_owner[key]]
        assert dup == (others[0] if others else None)


def test_window_hands_valuation_to_phone(tmp_path, qapp, monkeypatch):
    from phonebot.ui.main_window import MainWindow
    from phonebot.web.auth import hash_pin
    from phonebot.web.server import WebApp

    from .sample_data import build_sample_db

    conn, _ = build_sample_db(tmp_path / "w.sqlite3")
    s = SettingsRepository(conn).load()
    s.web_enabled, s.web_port, s.web_pin_hash = True, 0, hash_pin("2468")
    SettingsRepository(conn).save(s)
    win = MainWindow(conn, tmp_path / "w.sqlite3", thumbs_dir=tmp_path)
    try:
        assert win.web is not None
        calls = []
        monkeypatch.setattr(WebApp, "connect", lambda self: calls.append(1) or (_ for _ in ()).throw(AssertionError))
        rows = win.web.app.rows()  # wycena z okna — bez ponownego liczenia (bez połączenia z bazą)
        assert not calls
        assert {o.id: v.verdict for o, v in rows} == {o.id: v.verdict for o, v in win.model.rows()}
        monkeypatch.undo()
        win.web.app.invalidate()  # akcja z telefonu — telefon liczy sam do następnego odświeżenia okna
        assert len(win.web.app.rows()) == len(rows)
    finally:
        win._quitting = True
        win.close()
        conn.close()


def test_perf_panel_and_maintenance_from_settings(tmp_path, qapp):
    from phonebot.ui.main_window import MainWindow

    from .sample_data import build_sample_db

    conn, _ = build_sample_db(tmp_path / "s.sqlite3")
    (tmp_path / "a_72.jpg").write_bytes(b"x" * 2048)
    win = MainWindow(conn, tmp_path / "s.sqlite3", thumbs_dir=tmp_path)
    try:
        dialog = win.open_settings()
        text = dialog.perf_info.text()
        for label in ("Ostatnie odświeżenie", "Start programu", "Pamięć RAM programu", "Baza danych",
                      "Miniatury i zdjęcia", "Ostatnie porządki"):
            assert label in text, label
        assert "wycena i tabela" in text and "jeszcze nie było" in text and "limit 300 MB" in text
        dialog.maintenance_requested.emit()  # przycisk „Uporządkuj bazę teraz”
        deadline = time.monotonic() + 15
        while win._maint_worker is not None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        qapp.processEvents()
        assert maintenance.last_report(conn) is not None and "Ostatnie porządki" in dialog.perf_info.text()
        assert "jeszcze nie było" not in dialog.perf_info.text()
        dialog.reject()
        assert not win.run_maintenance()  # dziś już były — zegar nie uruchamia drugi raz
    finally:
        win._quitting = True
        win.close()
        conn.close()


def test_search_typing_saves_settings_later(tmp_path, qapp):
    from phonebot.core.view_filter import ViewFilter
    from phonebot.ui.main_window import MainWindow

    conn = open_database(tmp_path / "f.sqlite3")
    win = MainWindow(conn, tmp_path / "f.sqlite3", thumbs_dir=tmp_path)
    try:
        win.filters.set_filter(ViewFilter(text="pro"))
        assert SettingsRepository(conn).load().view_filter.text == ""  # jeszcze nie — wpisywanie trwa
        assert win._ui_save_timer.isActive()
        win._save_ui_state()
        assert SettingsRepository(conn).load().view_filter.text == "pro"
        win.filters.set_filter(ViewFilter(text="pro", sources=["vinted"]))  # inny filtr — od razu
        assert SettingsRepository(conn).load().view_filter.sources == ["vinted"]
    finally:
        win._quitting = True
        win.close()
        conn.close()


def test_unknown_stored_values_still_raise(db):
    from phonebot.storage.repositories import OfferRepository

    oid = add(db, "x", 0)
    db.execute("UPDATE offers SET condition = 'broken?' WHERE id = ?", (oid,))
    with pytest.raises(ValueError):
        OfferRepository(db).get(oid)


def test_photo_model_released_when_idle(tmp_path, qapp):
    from phonebot.ml import photo_model
    from phonebot.ui import ai_worker
    from phonebot.ui.ai_worker import AiWorker

    open_database(tmp_path / "a.sqlite3").close()
    worker = AiWorker(tmp_path / "a.sqlite3", Settings(), models_directory=tmp_path)
    photo_model.set_photo_classifier(object())  # wczytany model (atrapa)
    try:
        worker._photo_ready = True
        worker._arm_photo_release()
        assert worker._release_timer.isActive() and worker._release_timer.interval() == ai_worker.PHOTO_IDLE_S * 1000
        worker._pending.append(object())  # są zdjęcia w kolejce — model zostaje
        assert not worker.release_photo_model() and photo_model._current is not None
        worker._pending.clear()
        assert worker.release_photo_model()  # bez zdjęć — pamięć zwolniona, następne zdjęcie wczyta model
        assert photo_model._current is None and not worker._photo_ready
    finally:
        photo_model.set_photo_classifier(None)
