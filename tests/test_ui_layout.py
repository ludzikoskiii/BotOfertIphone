"""Nowy układ okna: panele, kolumny, motyw, pasek statusu, wydajność przy dużej liczbie ofert."""
import os
import random
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402

from phonebot.core.models import RawOffer  # noqa: E402
from phonebot.core.normalizer import parse_offer  # noqa: E402
from phonebot.core.view_filter import ViewFilter  # noqa: E402
from phonebot.storage.db import open_database  # noqa: E402
from phonebot.storage.repositories import OfferRepository, PartsRepository, SettingsRepository  # noqa: E402
from phonebot.ui.table_model import Col  # noqa: E402

from .sample_data import build_sample_db  # noqa: E402

DEFAULT_VISIBLE = [Col.MODEL, Col.STORAGE, Col.PRICE, Col.PROFIT, Col.MAX_BUY, Col.VERDICT, Col.SOURCE]


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path):
    conn, _ = build_sample_db(tmp_path / "t.sqlite3")
    from phonebot.ui.main_window import MainWindow

    win = MainWindow(conn, tmp_path / "t.sqlite3", thumbs_dir=tmp_path)
    yield win
    win._quitting = True
    win.close()
    conn.close()


def new_window(win):
    from phonebot.ui.main_window import MainWindow

    return MainWindow(win.conn, win.db_path, thumbs_dir=win.db_path.parent)


def test_three_panel_layout(window):
    split = window.splitter
    assert [split.widget(i) for i in range(3)] == [window.filters, window.stack, window.details]
    assert window.statusBar().isVisible() or not window.isVisible()


def test_default_columns_and_toggle_persist(window):
    assert window.visible_columns() == DEFAULT_VISIBLE
    window.set_column_visible(Col.BATTERY, True)
    window.set_column_visible(Col.SOURCE, False)
    window.set_column_visible(Col.MODEL, False)  # model zawsze widoczny
    assert Col.BATTERY in window.visible_columns() and Col.SOURCE not in window.visible_columns()
    assert Col.MODEL in window.visible_columns()
    other = new_window(window)
    assert other.visible_columns() == window.visible_columns()
    other.reset_columns()
    assert other.visible_columns() == DEFAULT_VISIBLE
    other._quitting = True
    other.close()


def test_columns_menu_lists_all_columns(window):
    window._fill_columns_menu()
    actions = [a for a in window.columns_menu.actions() if a.isCheckable()]
    assert len(actions) == len(Col)
    photo = next(a for a in actions if a.text() == "Zdjęcie")
    photo.setChecked(True)
    assert not window.table.isColumnHidden(Col.PHOTO)
    assert window.table.verticalHeader().defaultSectionSize() >= 60  # miniatura mieści się w wierszu


def test_details_panel_follows_selection(window):
    assert window.details.offer is None
    window.table.selectRow(0)
    offer, _ = window._row_at(window.proxy.index(0, 0))
    assert window.details.offer.id == offer.id
    assert "Maksymalna cena zakupu" in window.details.browser.toHtml()
    window.details._toggle_watch()
    assert window.model.row_at(window.model.row_of(offer.id))[0].status.value == "watched"
    # zaznaczenie przeżywa przeładowanie i zmianę filtrów
    window.reload()
    assert window.current_offer_id() == offer.id
    window.details_action.setChecked(False)
    assert window.details.isHidden()
    assert SettingsRepository(window.conn).load().details_visible is False or window._ui_save_timer.isActive()


def test_status_bar_shows_count_refresh_and_sources(window):
    text = window.count_label.text()
    assert "Ofert:" in text and "zielonych" in text
    assert window.refresh_label.text().startswith("Odświeżono")
    for key in ("allegro_lokalnie", "vinted", "sprzedajemy"):
        assert window.source_status.text_of(key)
    window.filters.set_filter(ViewFilter(sources=["vinted"]))
    assert " z " in window.count_label.text()


def test_theme_switch_without_restart(window):
    from phonebot.ui.theme import DARK, LIGHT, current

    s = SettingsRepository(window.conn).load()
    s.ui_theme, s.ui_font_pt = "dark", 11
    window.apply_settings(s)
    assert current() is DARK
    assert QtWidgets.QApplication.instance().font().pointSize() == 11
    r = next(r for r in range(window.proxy.rowCount())
             if (window._row_at(window.proxy.index(r, 0))[1].expected_profit or 0) > 0)
    fg = window.proxy.index(r, Col.PROFIT).data(Qt.ItemDataRole.ForegroundRole).color().name()
    assert fg == DARK.positive
    s = SettingsRepository(window.conn).load()
    assert s.ui_theme == "dark"
    s.ui_theme, s.ui_font_pt = "light", 10
    window.apply_settings(s)
    assert current() is LIGHT


def test_theme_selectable_in_settings(window):
    dialog = window.open_settings()
    dialog.theme_combo.setCurrentIndex(dialog.theme_combo.findData("dark"))
    dialog.font_spin.setValue(12)
    result = dialog.result_settings()
    assert (result.ui_theme, result.ui_font_pt) == ("dark", 12)
    dialog.reject()


def _big_db(path, n):
    conn = open_database(path)
    PartsRepository(conn).seed_defaults_if_empty()
    repo = OfferRepository(conn)
    rng = random.Random(7)
    models = ["iPhone 11", "iPhone 12", "iPhone 12 Pro", "iPhone 13", "iPhone 13 Pro", "iPhone 14", "iPhone 15"]
    extras = ["", " zbity ekran", " bateria 81%", " nie ładuje", " stan idealny", " pęknięty tył"]
    conn.execute("BEGIN")
    for i in range(n):
        title = f"{rng.choice(models)} {rng.choice([64, 128, 256])}GB{rng.choice(extras)}"
        raw = RawOffer(rng.choice(["allegro_lokalnie", "vinted", "sprzedajemy"]), f"id{i}", f"https://x/{i}", title,
                       float(rng.randrange(400, 3500, 10)), description="Opis", city="Kraków", photos=["x"],
                       shipping_available=True)
        repo.upsert(raw, parse_offer(raw))
    conn.execute("COMMIT")
    return conn


def test_fast_with_many_rows(app, tmp_path):
    """Kilkaset–tysiąc ofert: wczytanie, sortowanie, filtrowanie i przewijanie bez zacięć."""
    from phonebot.ui.main_window import MainWindow

    conn = _big_db(tmp_path / "big.sqlite3", 1000)
    win = MainWindow(conn, tmp_path / "big.sqlite3", thumbs_dir=tmp_path)
    win.resize(1400, 800)
    win.show()
    app.processEvents()
    assert win.model.rowCount() == 1000

    t = time.perf_counter()
    win.reload()
    reload_s = time.perf_counter() - t

    t = time.perf_counter()
    for col in (Col.PRICE, Col.VERDICT, Col.PROFIT):
        win.table.sortByColumn(col, Qt.SortOrder.AscendingOrder)
    sort_s = time.perf_counter() - t

    t = time.perf_counter()
    win.filters.set_filter(ViewFilter(models=["iPhone 13"]))
    win.filters.set_filter(ViewFilter())
    filter_s = time.perf_counter() - t

    t = time.perf_counter()
    bar = win.table.verticalScrollBar()
    for v in range(0, bar.maximum(), max(1, bar.maximum() // 20)):
        bar.setValue(v)
        win.table.viewport().repaint()
    scroll_s = time.perf_counter() - t

    win._quitting = True
    win.close()
    conn.close()
    # progi z zapasem na wolne maszyny CI
    assert reload_s < 3.0, reload_s
    assert sort_s < 1.5, sort_s
    assert filter_s < 1.0, filter_s
    assert scroll_s < 3.0, scroll_s


def test_sorting_keeps_selected_offer(window):
    window.table.selectRow(3)
    oid = window.current_offer_id()
    for col in (Col.PRICE, Col.MODEL, Col.VERDICT):
        window.table.sortByColumn(col, Qt.SortOrder.DescendingOrder)
        assert window.current_offer_id() == oid
        assert window.details.offer.id == oid
    window.table.sortByColumn(Col.PRICE, Qt.SortOrder.AscendingOrder)
    prices = [window._row_at(window.proxy.index(r, 0))[0].price for r in range(window.proxy.rowCount())]
    assert prices == sorted(prices)
    # nowe dane zachowują wybrane sortowanie
    window.reload()
    assert [window._row_at(window.proxy.index(r, 0))[0].price for r in range(window.proxy.rowCount())] == prices


def test_safety_settings_tab(window):
    from PySide6.QtWidgets import QComboBox, QDoubleSpinBox

    dialog = window.open_settings()
    price = dialog.findChild(QDoubleSpinBox, "sanity.price_min_ratio_working")
    assert price.value() == 30  # ułamek 0.30 pokazany jako 30 %
    price.setValue(25)
    cap = dialog.findChild(QComboBox, "sanity.soft_flag_cap")
    cap.setCurrentIndex(cap.findData("DO WERYFIKACJI"))
    country = dialog.findChild(QComboBox, "vinted_country_mode")
    country.setCurrentIndex(country.findData("ship"))
    cid, path = dialog.category_edits["sprzedajemy"]
    assert cid.text() == "1390" and path.text().endswith("apple-iphone")
    dialog.min_price_edits["vinted"].setValue(200)
    result = dialog.result_settings()
    assert result.sanity.price_min_ratio_working == 0.25
    assert result.sanity.soft_flag_cap == "DO WERYFIKACJI"
    assert result.vinted_country_mode == "ship"
    assert result.source_min_price["vinted"] == 200
    assert result.source_categories["sprzedajemy"]["id"] == "1390"
    dialog.reject()


def test_verify_verdict_shown_as_grey_label(window):
    from phonebot.core.models import MarketEstimate, Mode, Verdict
    from phonebot.core.parts import PartsCatalog, default_parts
    from phonebot.core.valuation import evaluate
    from phonebot.ui.table_model import VERDICT_ROLE
    from phonebot.ui.theme import current

    from .conftest import make_offer

    offer = make_offer("iPhone 13", price=83)  # „iPhone 13 ?” za 83 zł ze zrzutu ekranu
    offer.id = 999_999
    val = evaluate(offer, MarketEstimate(1500, 12, "t", "wysoka", 1500), PartsCatalog(default_parts()),
                   window.settings, Mode.REPAIR)
    window.model.set_rows([(offer, val)])
    index = window.proxy.index(0, Col.VERDICT)
    assert index.data() == "DO WERYFIKACJI" and index.data(VERDICT_ROLE) == Verdict.VERIFY
    assert current().verdict_bg[Verdict.VERIFY] in ("#e9ecef", "#34363c")  # szara etykieta
    window.table.viewport().repaint()  # delegat rysuje nowy werdykt bez błędów


def test_foreign_offers_restored_message(window):
    from phonebot.storage.repositories import RejectedRepository

    for i in (1, 2):
        raw = RawOffer("vinted", f"cz{i}", "https://x", f"iPhone 13 128GB nr {i}", 1400, photos=["x"],
                       params={"seller_id": f"9{i}"})
        RejectedRepository(window.conn).add(raw, "country", "sprzedawca spoza Polski (kraj z profilu: CZ)", "CZ")
    SettingsRepository(window.conn).set_value("filter_signature", "stara")  # np. po aktualizacji programu
    window._apply_filter_rules()
    assert window._status.text() == "+2 oferty z zagranicy wróciły na listę."
    ids = {o.raw.source_id for o in OfferRepository(window.conn).list()}
    assert {"cz1", "cz2"} <= ids
