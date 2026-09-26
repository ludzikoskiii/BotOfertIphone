import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from phonebot.core.models import Mode, OfferStatus, Verdict  # noqa: E402
from phonebot.core.settings import Settings  # noqa: E402
from phonebot.net.http import HostRateLimiter, HttpClient  # noqa: E402
from phonebot.services.scanner import Scanner  # noqa: E402
from phonebot.storage.db import open_database  # noqa: E402
from phonebot.storage.repositories import PartsRepository, SettingsRepository  # noqa: E402
from phonebot.ui.table_model import OFFER_ROLE, Col  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path):
    pages = {False: json.loads((FIX / "olx_page1.json").read_text(encoding="utf-8")),
             True: json.loads((FIX / "olx_page2.json").read_text(encoding="utf-8"))}
    db = tmp_path / "t.sqlite3"
    conn = open_database(db)
    PartsRepository(conn).seed_defaults_if_empty()
    settings = Settings(max_pages_per_query=2)
    SettingsRepository(conn).save(settings)
    limiter = HostRateLimiter(0)
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=pages["offset=40" in str(r.url)]))
    asyncio.run(Scanner(conn, settings, limiter,
                        http_factory=lambda: HttpClient(limiter, transport=transport)).run())
    from phonebot.ui.main_window import MainWindow

    win = MainWindow(conn, db, thumbs_dir=tmp_path)
    yield win
    win.close()
    conn.close()


def cell(win, row, col):
    return win.proxy.index(row, col).data()


def test_table_filled_and_sorted_by_profit(window):
    assert window.proxy.rowCount() == 12
    profits = []
    for r in range(window.proxy.rowCount()):
        oid = window.proxy.index(r, 0).data(OFFER_ROLE)
        _, val = next(x for x in window.model.rows() if x[0].id == oid)
        profits.append(val.expected_profit if val.expected_profit is not None else float("-inf"))
    assert profits == sorted(profits, reverse=True)
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
            return ScanReport(sources=[SourceReport("olx", "OLX", found=1, saved=1, new=1)])

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
    assert window._status.text().endswith("OLX: 1 ofert (1 nowych)")
    assert window.refresh_action.isEnabled()


def find_row(win, text_in_title, exact=False):
    for r in range(win.proxy.rowCount()):
        offer, val = win._row_at(win.proxy.index(r, 0))
        if (offer.raw.title == text_in_title) if exact else (text_in_title in offer.raw.title):
            return r, offer, val
    raise AssertionError(text_in_title)


def test_row_colors_and_flag_markers(window):
    from PySide6.QtCore import Qt

    from phonebot.core.models import RowColor
    from phonebot.ui.theme import ROW_BACKGROUND

    r, offer, val = find_row(window, "zbity ekran")
    bg = window.proxy.index(r, Col.PRICE).data(Qt.ItemDataRole.BackgroundRole).color().name()
    assert bg == ROW_BACKGROUND[val.color]
    assert val.color is RowColor.GREEN
    r, offer, val = find_row(window, "iPhone 12 Pro 256GB", exact=True)  # blokada iCloud, brak zdjęć
    assert val.has_hard_flag
    assert "⚑" in cell(window, r, Col.MODEL)
    tooltip = window.proxy.index(r, Col.MODEL).data(Qt.ItemDataRole.ToolTipRole)
    assert "Blokada iCloud" in tooltip and "Brak zdjęć" in tooltip


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
