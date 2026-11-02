"""„Wybrane” bez starych ofert: nieaktualne (⌛) na końcu listy, znikają same po ``keep_inactive_days`` dniach,
przycisk „Usuń nieaktualne”; obserwowane (★) zostają do ręcznego usunięcia."""
from __future__ import annotations

import os
from datetime import timedelta

import pytest

from phonebot.core.models import OfferStatus, Verdict
from phonebot.core.selection import SelectionCriteria
from phonebot.core.settings import Settings
from phonebot.core.sorting import level, sort_rows
from phonebot.storage import db as dbmod
from phonebot.storage.db import open_database
from phonebot.storage.repositories import OfferRepository, utcnow

from .conftest import make_offer
from .test_sorting import _val


def _picked(repo, title="iPhone 13 128GB", price=1000.0, **kw):
    o = make_offer(title, price, **kw)
    oid = repo.upsert(o.raw, o.parsed).offer_id
    repo.mark_picked([oid])
    return oid


def test_sold_offer_leaves_picked_after_keep_days(tmp_path):
    conn = open_database(tmp_path / "s.sqlite3")
    repo = OfferRepository(conn)
    sold, watched, missing = _picked(repo), _picked(repo), _picked(repo)
    repo.set_status(watched, OfferStatus.WATCHED)
    for oid in (sold, watched):
        assert repo.apply_page_check(oid, exists=False, price=None, reason="sold") == "gone"
    repo.deactivate_missing("test", utcnow() + timedelta(days=1))  # trzecia: niewidziana w wynikach
    assert SelectionCriteria().keep_inactive_days == 1
    now = utcnow()
    assert {o.id for o in repo.list_picked_inactive(1, now)} == {sold, watched, missing}  # dziś jeszcze widać
    later = now + timedelta(days=2)
    assert [o.id for o in repo.list_picked_inactive(1, later)] == [watched]  # ★ zostaje do ręcznego usunięcia
    assert {o.id for o in repo.list_picked_inactive(0, later)} == {sold, watched, missing}  # 0 = jak dotąd
    assert {o.id for o in repo.list_picked_inactive(3, later)} == {sold, watched, missing}
    conn.close()


def test_offer_seen_again_is_active_without_inactive_date(tmp_path):
    conn = open_database(tmp_path / "a.sqlite3")
    repo = OfferRepository(conn)
    o = make_offer("iPhone 13 128GB", 1000, source_id="x1")
    oid = repo.upsert(o.raw, o.parsed).offer_id
    repo.deactivate_missing("test", utcnow() + timedelta(days=1))
    row = conn.execute("SELECT is_active, inactive_at FROM offers WHERE id = ?", (oid,)).fetchone()
    assert row["is_active"] == 0 and row["inactive_at"]
    repo.upsert(o.raw, o.parsed)  # wróciła w wynikach portalu
    row = conn.execute("SELECT is_active, inactive_at FROM offers WHERE id = ?", (oid,)).fetchone()
    assert row["is_active"] == 1 and row["inactive_at"] is None
    conn.close()


def test_update_cleans_old_inactive_offers_out_of_picked(tmp_path, monkeypatch):
    """Po aktualizacji: oferty nieaktualne od dawna (np. sprzed tygodnia) od razu znikają z „Wybrane”."""
    path = tmp_path / "old.sqlite3"
    monkeypatch.setattr(dbmod, "MIGRATIONS", dbmod.MIGRATIONS[:-1])  # baza starszej wersji (bez inactive_at)
    conn = open_database(path)
    repo = OfferRepository.__new__(OfferRepository)
    repo.conn = conn
    old_ids = []
    for i, days in enumerate((10, 0)):
        o = make_offer("iPhone 12 64GB", 500 + i, source_id=f"old{i}")
        seen = (utcnow() - timedelta(days=days)).isoformat()
        conn.execute("INSERT INTO offers (source, source_id, url, title, description, price, currency, photos, params, "
                     "model, condition, defects, flags, first_seen, last_seen, is_active, picked_at) "
                     "VALUES (?, ?, ?, ?, '', ?, 'PLN', '[]', '{}', 'iPhone 12', 'good', '[]', '[]', ?, ?, 0, ?)",
                     (o.raw.source, o.raw.source_id, o.raw.url, o.raw.title, o.raw.price, seen, seen, seen))
        old_ids.append(conn.execute("SELECT id FROM offers WHERE source_id = ?", (f"old{i}",)).fetchone()[0])
    conn.close()
    monkeypatch.undo()
    conn = open_database(path)  # migracja: data „nieaktualna od” = ostatnie sprawdzenie / ostatnio widziana
    assert [o.id for o in OfferRepository(conn).list_picked_inactive(1)] == [old_ids[1]]
    conn.close()


def test_inactive_rows_always_last_whatever_the_sort():
    cheap_sold = make_offer("iPhone 12 Pro Max 128GB", 130)
    cheap_sold.active = False
    rows = [(cheap_sold, _val(Verdict.BUY, 900)), (make_offer("iPhone 11 128GB", 400), _val(Verdict.BUY, 200)),
            (make_offer("iPhone 13 128GB", 900), _val(Verdict.NEGOTIATE, 300))]
    for spec in ([level("price", "asc")], [level("profit", "desc")], [level("verdict", "desc")]):
        sort_rows(rows, spec)
        assert rows[-1][0] is cheap_sold
    assert [o.price for o, _ in rows[:2]] != []


def test_drop_inactive_picked(tmp_path):
    conn = open_database(tmp_path / "d.sqlite3")
    repo = OfferRepository(conn)
    active, sold, watched = _picked(repo), _picked(repo), _picked(repo)
    repo.set_status(watched, OfferStatus.WATCHED)
    for oid in (sold, watched):
        repo.apply_page_check(oid, exists=False, price=None, reason="sold")
    assert repo.drop_inactive_picked() == 2
    assert repo.list_picked_inactive() == []
    assert not repo.get(active).pick_excluded  # aktualne zostają
    assert repo.get(watched).status is OfferStatus.NEW and repo.get(sold).pick_excluded
    assert repo.drop_inactive_picked() == 0
    conn.close()


def test_settings_keep_days_saved():
    s = Settings()
    s.selection.keep_inactive_days = 0
    assert Settings.from_json(s.to_json()).selection.keep_inactive_days == 0
    assert Settings.from_json("{}").selection.keep_inactive_days == 1


# ------------------------------------------------------------------ okno ---

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def window(tmp_path):
    widgets = pytest.importorskip("PySide6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    from phonebot.ui.main_window import MainWindow

    from .sample_data import build_sample_db

    conn, _ = build_sample_db(tmp_path / "t.sqlite3")
    win = MainWindow(conn, tmp_path / "t.sqlite3", thumbs_dir=tmp_path)
    yield win
    app.processEvents()
    win._quitting = True
    win.close()
    conn.close()


def _ids(win):
    return [win._row_at(win.proxy.index(r, 0))[0].id for r in range(win.proxy.rowCount())]


def test_window_stale_offers_last_and_cleanup_button(window):
    from phonebot.core.selection import is_picked

    window.list_tabs.setCurrentIndex(1)
    picked = [o.id for o, v in window.model.rows() if is_picked(o, v, window.settings.selection)]
    assert len(picked) >= 2
    assert window.drop_stale_btn.isHidden()  # brak nieaktualnych — przycisku nie ma
    repo = OfferRepository(window.conn)
    sold, old = picked[0], picked[1]
    repo.apply_page_check(sold, exists=False, price=None, reason="sold")  # sprzedana przed chwilą
    repo.apply_page_check(old, exists=False, price=None, reason="sold")
    window.conn.execute("UPDATE offers SET inactive_at = ? WHERE id = ?",
                        ((utcnow() - timedelta(days=3)).isoformat(), old))  # sprzedana 3 dni temu
    window.reload()
    ids = _ids(window)
    assert ids[-1] == sold and old not in ids  # świeżo sprzedana na końcu, stara zniknęła sama
    assert not window.drop_stale_btn.isHidden() and window.drop_stale_btn.text() == "🧹 Usuń nieaktualne (1)"
    window.list_tabs.setCurrentIndex(0)
    assert window.drop_stale_btn.isHidden()  # tylko przy „Wybrane”
    window.list_tabs.setCurrentIndex(1)
    assert window.drop_inactive_picked() == 1
    assert sold not in _ids(window) and window.drop_stale_btn.isHidden()
    assert "Usunięto z „Wybrane” 1" in window._status.text()
