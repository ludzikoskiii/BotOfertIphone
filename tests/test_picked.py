"""Zadanie 2: listy „Wszystkie oferty” i „Wybrane”."""
from __future__ import annotations

import os
from datetime import timedelta

import pytest

from phonebot.core.models import OfferStatus, Verdict
from phonebot.core.selection import SelectionCriteria, auto_match, is_picked
from phonebot.core.sorting import SortLevel
from phonebot.storage.db import open_database
from phonebot.storage.repositories import OfferRepository, utcnow

from .conftest import make_offer
from .test_sorting import _val


def test_auto_criteria():
    c = SelectionCriteria()  # KUPUJ/NEGOCJUJ, zysk ≥ 150
    o = make_offer("iPhone 13 128GB")
    assert auto_match(o, _val(Verdict.BUY, 200), c)
    assert not auto_match(o, _val(Verdict.BUY, 100), c)
    assert not auto_match(o, _val(Verdict.VERIFY, 900), c)
    assert not auto_match(o, _val(Verdict.NEGOTIATE, None), c)
    c.min_score = 70
    assert not auto_match(o, _val(Verdict.BUY, 300, score=60), c)
    assert auto_match(o, _val(Verdict.BUY, 300, score=75), c)
    c.enabled = False
    assert not auto_match(o, _val(Verdict.BUY, 300, score=75), c)


def test_manual_decisions_win():
    c = SelectionCriteria()
    o = make_offer("iPhone 13 128GB")
    weak = _val(Verdict.SKIP, -100)
    assert not is_picked(o, weak, c)
    o.status = OfferStatus.WATCHED  # ręcznie dodana — mimo kryteriów
    assert is_picked(o, weak, c)
    o.status, o.pick_excluded = OfferStatus.NEW, True  # ręcznie usunięta — mimo kryteriów
    assert not is_picked(o, _val(Verdict.BUY, 500), c)


def test_repository_pick_state(tmp_path):
    conn = open_database(tmp_path / "p.sqlite3")
    repo = OfferRepository(conn)
    o = make_offer("iPhone 13 128GB")
    oid = repo.upsert(o.raw, o.parsed).offer_id
    assert repo.mark_picked([oid]) == 1 and repo.mark_picked([oid]) == 0  # data tylko raz
    first = repo.get(oid).picked_at
    repo.set_status(oid, OfferStatus.WATCHED)
    repo.set_pick_excluded(oid, True)  # „Usuń z Wybranych” zdejmuje też obserwowanie
    got = repo.get(oid)
    assert got.pick_excluded and got.status is OfferStatus.NEW and got.picked_at == first
    repo.set_status(oid, OfferStatus.WATCHED)  # „Obserwuj” cofa wykluczenie
    assert not repo.get(oid).pick_excluded
    # zniknęła z portalu → nieaktualna, ale zostaje w „Wybrane”
    repo.deactivate_missing("test", utcnow() + timedelta(days=1))
    assert [x.id for x in repo.list_picked_inactive()] == [oid]
    assert not repo.list_picked_inactive()[0].active
    repo.set_pick_excluded(oid, True)
    assert repo.list_picked_inactive() == []
    conn.close()


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


def _expected_picked(win):
    c = win.settings.selection
    return {o.id for o, v in win.model.rows() if is_picked(o, v, c)}


def test_tabs_counts_and_switch(window):
    tabs = window.list_tabs
    assert tabs.count() == 2 and window.current_list() == "all"
    total = window.proxy.rowCount()
    picked = _expected_picked(window)
    assert picked, "przykładowa baza ma oferty KUPUJ z zyskiem ≥ 150 zł"
    assert tabs.tabText(0) == f"Wszystkie oferty ({total})"
    assert tabs.tabText(1) == f"Wybrane ({len(picked)})"
    tabs.setCurrentIndex(1)
    assert window.current_list() == "picked" and set(_ids(window)) == picked
    # daty trafienia do „Wybrane” zapisane w bazie
    repo = OfferRepository(window.conn)
    assert all(repo.get(i).picked_at is not None for i in picked)


def test_manual_add_and_remove(window):
    window.list_tabs.setCurrentIndex(1)
    picked = _expected_picked(window)
    weak = next(o for o, v in window.model.rows() if o.id not in picked)
    window.set_picked(weak.id, True)  # ręcznie dodana (= obserwowana)
    assert weak.id in _ids(window) and weak.status is OfferStatus.WATCHED
    assert window.list_tabs.tabText(1) == f"Wybrane ({len(picked) + 1})"
    strong = next(iter(picked))
    window.set_picked(strong, False)  # ręcznie usunięta mimo kryteriów
    assert strong not in _ids(window)
    window.reload()  # po ponownym wczytaniu decyzje zostają
    assert weak.id in _ids(window) and strong not in _ids(window)
    # „Obserwuj” przywraca
    window.set_offer_status(strong, OfferStatus.WATCHED)
    window.reload()
    assert strong in _ids(window)


def test_vanished_offer_marked_outdated(window):
    window.list_tabs.setCurrentIndex(1)
    oid = next(iter(_expected_picked(window)))
    window.conn.execute("UPDATE offers SET is_active = 0 WHERE id = ?", (oid,))
    window.reload()
    assert oid in _ids(window)  # w „Wybrane” zostaje…
    row = window.proxy.mapFromSource(window.model.index(window.model.row_of(oid), 1)).row()
    from phonebot.ui.table_model import Col

    assert window.proxy.index(row, Col.MODEL).data().startswith("⌛")
    window.list_tabs.setCurrentIndex(0)
    assert oid not in _ids(window)  # …a we „Wszystkie oferty” jej nie ma
    window.details.set_offer(*window.model.row_at(window.model.row_of(oid)))
    assert "Nieaktualna" in window.details.browser.toPlainText()


def test_each_tab_remembers_sort_and_filters_are_shared(window):
    from phonebot.core.view_filter import ViewFilter
    from tests.test_ui_layout import new_window

    window.model.set_sort_spec([SortLevel("price", "asc")])
    window.list_tabs.setCurrentIndex(1)
    assert window.model.sort_spec != (SortLevel("price", "asc"),)  # „Wybrane” ma własne (domyślne)
    window.model.set_sort_spec([SortLevel("model", "asc")])
    window.list_tabs.setCurrentIndex(0)
    assert window.model.sort_spec == (SortLevel("price", "asc"),)
    window.filters.set_filter(ViewFilter(price_max=800))  # wspólny filtr — liczniki obu list
    c = window.settings.selection
    in_filter = [(o, v) for o, v in window.model.rows() if o.price <= 800]
    assert window.list_tabs.tabText(0) == f"Wszystkie oferty ({sum(o.active for o, _ in in_filter)})"
    assert window.list_tabs.tabText(1) == f"Wybrane ({sum(is_picked(o, v, c) for o, v in in_filter)})"
    window.list_tabs.setCurrentIndex(1)
    window._save_ui_state()
    other = new_window(window)
    try:
        assert other.current_list() == "picked"
        assert other.model.sort_spec == (SortLevel("model", "asc"),)
    finally:
        other._quitting = True
        other.close()


def test_details_pick_button_and_settings_tab(window):
    oid = next(iter(_expected_picked(window)))
    window.details.set_offer(*window.model.row_at(window.model.row_of(oid)))
    assert window.details.pick_btn.text() == "− Wybrane"
    assert "spełnia kryteria automatyczne" in window.details.browser.toPlainText()
    window.details.pick_btn.click()
    assert window.details.pick_btn.text() == "+ Wybrane"
    dialog = window.open_settings()
    from PySide6.QtWidgets import QSpinBox

    dialog.selection_verdicts["NEGOCJUJ"].setChecked(False)
    dialog.findChild(QSpinBox, "selection.min_score").setValue(70)
    result = dialog.result_settings()
    assert result.selection.verdicts == ["KUPUJ"] and result.selection.min_score == 70
    dialog.reject()
