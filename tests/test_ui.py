import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from phonebot.core.models import Mode, OfferStatus, Verdict  # noqa: E402
from phonebot.storage.db import open_database  # noqa: E402
from phonebot.ui.table_model import OFFER_ROLE, Col  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path):
    from .sample_data import build_sample_db

    conn, _ = build_sample_db(tmp_path / "t.sqlite3")
    from phonebot.ui.main_window import MainWindow

    win = MainWindow(conn, tmp_path / "t.sqlite3", thumbs_dir=tmp_path)
    yield win
    win.close()
    conn.close()


def cell(win, row, col):
    return win.proxy.index(row, col).data()


def test_table_filled_and_sorted_by_verdict_then_profit(window):
    assert window.proxy.rowCount() == 17  # 12 Allegro Lokalnie + 2 Vinted + 3 Sprzedajemy.pl
    keys = []
    for r in range(window.proxy.rowCount()):
        oid = window.proxy.index(r, 0).data(OFFER_ROLE)
        _, val = next(x for x in window.model.rows() if x[0].id == oid)
        keys.append((val.verdict.rank, val.expected_profit if val.expected_profit is not None else float("-inf")))
    assert keys == sorted(keys, reverse=True)
    assert cell(window, 0, Col.VERDICT).startswith(Verdict.BUY.value)
    assert cell(window, 0, Col.PRICE).endswith("zł")


def test_sort_by_price(window):
    from PySide6.QtCore import Qt

    window.table.sortByColumn(Col.PRICE, Qt.SortOrder.AscendingOrder)
    prices = [float(cell(window, r, Col.PRICE).replace(" zł", "").replace(" ", ""))
              for r in range(window.proxy.rowCount())]
    assert prices == sorted(prices)


def test_mode_switch_reevaluates(window):
    window.mode_combo.setCurrentIndex(window.mode_combo.findData(Mode.RESELL.value))
    verdicts = {cell(window, r, Col.CONDITION): cell(window, r, Col.VERDICT) for r in range(window.proxy.rowCount())}
    assert verdicts["Uszkodzony"].startswith(Verdict.SKIP.value)
    assert window.settings_repo.load().mode == Mode.RESELL.value


def test_empty_state(app, tmp_path):
    conn = open_database(tmp_path / "empty.sqlite3")
    from phonebot.ui.main_window import MainWindow

    win = MainWindow(conn, tmp_path / "empty.sqlite3", thumbs_dir=tmp_path)
    assert win.stack.currentWidget() is win.empty_label
    win.close()


def test_scan_runs_in_background_thread(window, monkeypatch):
    """Regresja: worker musi przeżyć do końca skanu, a wynik wrócić do GUI."""
    import threading
    import time

    from phonebot.services.scanner import ScanReport, SourceReport
    from phonebot.ui import workers

    seen_threads = []

    class FakeScanner:
        def __init__(self, *a, **kw):
            pass

        async def run(self, progress, force=False):
            seen_threads.append(threading.current_thread())
            progress("pracuję")
            return ScanReport(sources=[SourceReport("vinted", "Vinted", found=1, saved=1, new=1)])

    monkeypatch.setattr(workers, "Scanner", FakeScanner)
    app = QtWidgets.QApplication.instance()
    window.start_scan()
    assert not window.refresh_action.isEnabled()
    deadline = time.time() + 10
    while window._thread is not None and time.time() < deadline:
        app.processEvents()
        time.sleep(0.01)
    assert window._thread is None
    assert seen_threads and seen_threads[0] is not threading.main_thread()
    assert window._status.text().endswith("Vinted: 1 ofert (1 nowych)")
    assert window.refresh_action.isEnabled()


def find_row(win, text_in_title, exact=False):
    for r in range(win.proxy.rowCount()):
        offer, val = win._row_at(win.proxy.index(r, 0))
        if (offer.raw.title == text_in_title) if exact else (text_in_title in offer.raw.title):
            return r, offer, val
    raise AssertionError(text_in_title)


def test_verdict_label_profit_colors_and_flag_markers(window):
    from PySide6.QtCore import Qt

    from phonebot.core.models import RowColor
    from phonebot.ui.table_model import VERDICT_ROLE
    from phonebot.ui.theme import current

    pal = current()
    r, offer, val = find_row(window, "zbity ekran")
    # tło wiersza neutralne, werdykt jako etykieta (rola dla delegata) z tekstem
    assert window.proxy.index(r, Col.PRICE).data(Qt.ItemDataRole.BackgroundRole) is None
    assert window.proxy.index(r, Col.VERDICT).data(VERDICT_ROLE) == val.verdict
    assert cell(window, r, Col.VERDICT) == val.verdict.value
    assert val.color is RowColor.GREEN
    # zysk dodatni na zielono, liczby do prawej
    assert val.expected_profit > 0
    fg = window.proxy.index(r, Col.PROFIT).data(Qt.ItemDataRole.ForegroundRole).color().name()
    assert fg == pal.positive
    align = window.proxy.index(r, Col.PRICE).data(Qt.ItemDataRole.TextAlignmentRole)
    assert align & Qt.AlignmentFlag.AlignRight
    losers = [(o, v) for o, v in window.model.rows() if v.expected_profit is not None and v.expected_profit < 0]
    assert losers
    row = window.proxy.mapFromSource(window.model.index(window.model.row_of(losers[0][0].id), Col.PROFIT)).row()
    fg = window.proxy.index(row, Col.PROFIT).data(Qt.ItemDataRole.ForegroundRole).color().name()
    assert fg == pal.negative
    r, offer, val = find_row(window, "iPhone 12 Pro 256GB", exact=True)  # blokada iCloud, brak zdjęć
    assert val.has_hard_flag
    assert "⚑" in cell(window, r, Col.MODEL)
    tooltip = window.proxy.index(r, Col.MODEL).data(Qt.ItemDataRole.ToolTipRole)
    assert "Blokada iCloud" in tooltip and "Brak zdjęć" in tooltip


def test_money_format():
    from phonebot.ui.table_model import money

    assert money(1250) == "1 250 zł"
    assert money(-80.4) == "-80 zł"
    assert money(None) == "—"


def test_watch_and_hide(window):
    r, offer, _ = find_row(window, "zbity ekran")
    window.set_offer_status(offer.id, OfferStatus.WATCHED)
    r, offer, _ = find_row(window, "zbity ekran")
    assert cell(window, r, Col.MODEL).startswith("★")
    before = window.proxy.rowCount()
    window.set_offer_status(offer.id, OfferStatus.HIDDEN)
    assert window.proxy.rowCount() == before - 1
    window.show_hidden_action.setChecked(True)
    assert window.proxy.rowCount() == before


def test_details_dialog(window):
    r, offer, val = find_row(window, "zbity ekran")
    dialog = window.show_details(window.proxy.index(r, 0))
    html = dialog.browser.toHtml()
    assert "Rekomendacja negocjacji" in html and "Maksymalna cena zakupu" in html
    dialog._toggle_watch()
    assert window.model.row_at(window.model.row_of(offer.id))[0].status is OfferStatus.WATCHED
    assert "Przestań" in dialog.watch_btn.text()
    dialog.close()
