"""Zadanie 1: sortowanie — wiele poziomów, kierunek jednym kliknięciem, kolejność generacji, pamięć."""
from __future__ import annotations

import os

import pytest

from phonebot.core.models import MarketEstimate, Mode, Negotiation, RowColor, Valuation, Verdict
from phonebot.core.sorting import (
    ASC,
    DEFAULT_SORT,
    DESC,
    SortLevel,
    model_rank,
    sort_rows,
    spec_from_json,
    spec_to_json,
)

from .conftest import make_offer


def _val(verdict=Verdict.NEGOTIATE, profit=None, score=50, market=None):
    return Valuation(mode=Mode.REPAIR, market=MarketEstimate(market, 3, "test", "średnia"), repair_items=[],
                     repair_cost=0, cost_items=[], total_costs=0, expected_profit=profit, roi_pct=None,
                     required_profit=None, max_buy_price=None, verdict=verdict, negotiation=Negotiation(False, None, None, ""),
                     score=score, color=RowColor.YELLOW, flags=[], reasons=[])


def _titles(rows):
    return [o.raw.title for o, _ in rows]


def test_model_sorted_by_generation_then_storage():
    titles = ["iPhone 13 128GB", "iPhone 12 mini 64GB", "iPhone 11 Pro Max 256GB", "iPhone 12 Pro 128GB",
              "iPhone 11 64GB", "iPhone 13 mini 128GB", "iPhone 12 128GB", "iPhone 11 Pro 64GB",
              "iPhone 12 Pro Max 128GB", "iPhone 12 64GB", "iPhone SE 2020 64GB"]
    rows = [(make_offer(t), _val()) for t in titles]
    sort_rows(rows, [SortLevel("model", ASC)])
    assert _titles(rows) == ["iPhone 11 64GB", "iPhone 11 Pro 64GB", "iPhone 11 Pro Max 256GB",
                             "iPhone SE 2020 64GB", "iPhone 12 mini 64GB", "iPhone 12 64GB", "iPhone 12 128GB",
                             "iPhone 12 Pro 128GB", "iPhone 12 Pro Max 128GB", "iPhone 13 mini 128GB",
                             "iPhone 13 128GB"]
    sort_rows(rows, [SortLevel("model", DESC)])
    assert _titles(rows)[:2] == ["iPhone 13 128GB", "iPhone 13 mini 128GB"]


def test_future_models_after_catalog():
    assert model_rank("iPhone 17 Pro Max") < model_rank("iPhone 18") < model_rank("iPhone 18 Pro")
    assert model_rank(None) is None


def test_multi_level_verdict_then_profit():
    rows = [(make_offer("a"), _val(Verdict.NEGOTIATE, 300)), (make_offer("b"), _val(Verdict.BUY, 150)),
            (make_offer("c"), _val(Verdict.NEGOTIATE, 500)), (make_offer("d"), _val(Verdict.SKIP, 900)),
            (make_offer("e"), _val(Verdict.BUY, 400))]
    sort_rows(rows, DEFAULT_SORT)
    assert _titles(rows) == ["e", "b", "c", "a", "d"]
    sort_rows(rows, [SortLevel("verdict", DESC), SortLevel("profit", ASC)])
    assert _titles(rows) == ["b", "e", "a", "c", "d"]


def test_missing_values_last_in_both_directions():
    rows = [(make_offer("brak"), _val(profit=None)), (make_offer("100"), _val(profit=100)),
            (make_offer("-50"), _val(profit=-50))]
    sort_rows(rows, [SortLevel("profit", DESC)])
    assert _titles(rows) == ["100", "-50", "brak"]
    sort_rows(rows, [SortLevel("profit", ASC)])
    assert _titles(rows) == ["-50", "100", "brak"]


def test_price_and_score():
    rows = [(make_offer("x", price=p), _val(score=s)) for p, s in ((900, 40), (500, 80), (700, 80))]
    sort_rows(rows, [SortLevel("score", DESC), SortLevel("price", ASC)])
    assert [o.price for o, _ in rows] == [500, 700, 900]
    sort_rows(rows, [SortLevel("price", DESC)])
    assert [o.price for o, _ in rows] == [900, 700, 500]


def test_spec_json_roundtrip_and_fallback():
    spec = (SortLevel("verdict", DESC), SortLevel("profit", ASC))
    assert spec_from_json(spec_to_json(spec)) == spec
    assert spec_from_json(None) == DEFAULT_SORT
    assert spec_from_json([["nie_ma", "asc"], "śmieci"]) == DEFAULT_SORT
    # powtórzone pole i więcej niż 3 poziomy — przycięte
    long = [["price", "asc"], ["price", "desc"], ["model", "asc"], ["score", "desc"], ["added", "desc"]]
    assert [lv.field for lv in spec_from_json(long)] == ["price", "model", "score"]


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


def _column(win, col):
    return [win._row_at(win.proxy.index(r, 0))[0 if col != "val" else 1] for r in range(win.proxy.rowCount())]


def _click(win, col, shift=False, monkeypatch=None):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    mods = Qt.KeyboardModifier.ShiftModifier if shift else Qt.KeyboardModifier.NoModifier
    monkeypatch.setattr(QApplication, "keyboardModifiers", staticmethod(lambda: mods))
    win._header_clicked(int(col))


def test_default_sort_and_bar(window):
    assert window.model.sort_spec == DEFAULT_SORT
    bar = window.sort_bar
    assert bar.field_combos[0].currentData() == "verdict" and bar.field_combos[1].currentData() == "profit"
    assert bar.dir_buttons[0].text() == "↓ najlepszy najpierw"
    assert bar.field_combos[2].currentData() == "" and not bar.dir_buttons[2].isVisibleTo(bar)


def test_header_click_sort_toggle_and_shift_levels(window, monkeypatch):
    from phonebot.ui.table_model import Col

    _click(window, Col.PRICE, monkeypatch=monkeypatch)
    assert window.model.sort_spec == (SortLevel("price", ASC),)
    prices = [o.price for o in _column(window, "offer")]
    assert prices == sorted(prices)
    _click(window, Col.PRICE, monkeypatch=monkeypatch)  # drugi klik — odwrotnie
    assert [o.price for o in _column(window, "offer")] == sorted(prices, reverse=True)
    header = window.table.horizontalHeader()
    assert header.sortIndicatorSection() == Col.PRICE
    # Shift+klik: kolejny poziom
    _click(window, Col.MODEL, monkeypatch=monkeypatch)
    _click(window, Col.STORAGE, shift=True, monkeypatch=monkeypatch)
    assert window.model.sort_spec == (SortLevel("model", ASC), SortLevel("storage", DESC))
    assert window.model.headerData(Col.STORAGE, Qt_h()) == "Pamięć ²↓"
    assert window.sort_bar.field_combos[1].currentData() == "storage"
    # pierwszy klik w kolumnę pierwszego poziomu odwraca go, drugi poziom zostaje
    _click(window, Col.MODEL, monkeypatch=monkeypatch)
    assert window.model.sort_spec == (SortLevel("model", DESC), SortLevel("storage", DESC))


def Qt_h():
    from PySide6.QtCore import Qt

    return Qt.Orientation.Horizontal


def test_sort_bar_changes_table_and_is_remembered(window):
    from tests.test_ui_layout import new_window

    bar = window.sort_bar
    bar.field_combos[0].setCurrentIndex(bar.field_combos[0].findData("score"))
    assert window.model.sort_spec == (SortLevel("score", DESC), SortLevel("profit", DESC))
    bar.dir_buttons[0].click()
    assert window.model.sort_spec[0] == SortLevel("score", ASC)
    scores = [v.score for v in _column(window, "val")]
    assert scores == sorted(scores)
    bar.field_combos[1].setCurrentIndex(0)  # „—” usuwa drugi poziom
    assert window.model.sort_spec == (SortLevel("score", ASC),)
    window._save_ui_state()
    other = new_window(window)
    try:
        assert other.model.sort_spec == (SortLevel("score", ASC),)
        assert other.sort_bar.dir_buttons[0].text() == "↑ najniższa"
    finally:
        other._quitting = True
        other.close()


def test_preset_and_selection_kept(window):
    window.table.selectRow(4)
    oid = window.current_offer_id()
    action = next(a for a in window.sort_bar.presets_btn.menu().actions() if a.text().startswith("Model"))
    action.trigger()
    assert window.model.sort_spec == (SortLevel("model", ASC), SortLevel("price", ASC))
    assert window.current_offer_id() == oid


def test_sorting_does_not_revaluate(window, monkeypatch):
    from phonebot.services import evaluator

    def boom(*a, **k):
        raise AssertionError("sortowanie nie może wyceniać ofert od nowa")

    monkeypatch.setattr(evaluator.Evaluator, "evaluate_all", boom)
    from phonebot.core.sorting import PRESETS

    for spec in PRESETS.values():
        window.model.set_sort_spec(spec)
