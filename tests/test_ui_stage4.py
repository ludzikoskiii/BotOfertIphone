import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
QtWidgets = pytest.importorskip("PySide6.QtWidgets")

from PySide6.QtCore import Qt  # noqa: E402

from phonebot.core.models import Defect  # noqa: E402
from phonebot.core.places import Place  # noqa: E402
from phonebot.core.view_filter import ViewFilter  # noqa: E402
from phonebot.storage.repositories import PartsRepository, SettingsRepository  # noqa: E402
from phonebot.ui.table_model import Col  # noqa: E402

from .sample_data import build_sample_db  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


@pytest.fixture
def window(app, tmp_path):
    conn, report = build_sample_db(tmp_path / "t.sqlite3")
    from phonebot.ui.main_window import MainWindow

    win = MainWindow(conn, tmp_path / "t.sqlite3", thumbs_dir=tmp_path)
    win.report = report
    yield win
    win.close()
    conn.close()


def sources_shown(win):
    return {win.proxy.index(r, Col.SOURCE).data() for r in range(win.proxy.rowCount())}


def test_three_portals_scanned(window):
    by_source = {s.key: s for s in window.report.sources}
    assert all(s.ok for s in by_source.values()), [s.error for s in by_source.values()]
    assert by_source["olx"].saved == 12
    assert by_source["allegro_lokalnie"].saved == 2  # etui odrzucone
    assert by_source["vinted"].saved == 2  # oferta w obcej walucie odrzucona
    assert by_source["sprzedajemy"].saved == 3  # etui odrzucone
    assert sources_shown(window) == {"OLX", "Allegro Lokalnie", "Vinted", "Sprzedajemy.pl"}


def test_filters_apply_and_persist(window):
    total = window.proxy.rowCount()
    window.filters.set_filter(ViewFilter(sources=["vinted"]))
    assert sources_shown(window) == {"Vinted"}
    assert window.proxy.rowCount() < total
    assert "z" in window.count_label.text()
    assert SettingsRepository(window.conn).load().view_filter.sources == ["vinted"]
    window.filters.set_filter(ViewFilter(models=["iPhone 13"], colors=["green"]))
    for r in range(window.proxy.rowCount()):
        assert window.proxy.index(r, Col.MODEL).data().lstrip("★ ").startswith("iPhone 13")
    window.filters.set_filter(ViewFilter())
    assert window.proxy.rowCount() == total


def test_radius_filter_uses_home_location(window):
    window.filters.set_filter(ViewFilter(radius_km=60, radius_keeps_shipping=False))
    for r in range(window.proxy.rowCount()):
        loc = window.proxy.index(r, Col.LOCATION).data()
        km = int(loc.rsplit("(", 1)[1].split()[0])
        assert km <= 60


def test_change_location_recomputes_distances(window):
    def distance_to(city):
        for offer, _ in window.model.rows():
            if offer.raw.city == city:
                return offer.distance_km
        raise AssertionError(city)

    near_home = distance_to("Nowy Targ")
    dialog = window.change_location()
    dialog.set_place(Place("Gdańsk", 54.3520, 18.6466))
    dialog.accept()
    assert distance_to("Nowy Targ") > near_home + 400
    assert SettingsRepository(window.conn).load().location_name == "Gdańsk"
    assert "Gdańsk" in window.filters.location_label.text()


def test_location_dialog_search_results(app):
    from phonebot.ui.location_dialog import LocationDialog

    dlg = LocationDialog("Kacwin", 49.35, 20.30)
    dlg.show_results([Place("Jurgów", 49.33, 20.13, "gm. Bukowina Tatrzańska")])
    dlg._pick_item(dlg.results.item(0))
    assert dlg.place() == Place("Jurgów", 49.33, 20.13)
    dlg.quick.setCurrentIndex(dlg.quick.findText("Zakopane", Qt.MatchFlag.MatchStartsWith))
    assert dlg.place().name == "Zakopane"


def test_settings_dialog_roundtrip(window):
    dialog = window.open_settings()
    dialog.findChild(QtWidgets.QDoubleSpinBox, "profit_repair.min_amount").setValue(250)
    dialog.findChild(QtWidgets.QSpinBox, "score_green").setValue(70)
    dialog.source_checks["vinted"].setChecked(False)
    dialog.apply_location(Place("Nowy Targ", 49.4775, 20.0327))
    dialog.manual.add_row(["iPhone 13", "128", "1800"])
    dialog.accept()
    s = SettingsRepository(window.conn).load()
    assert s.profit_repair.min_amount == 250 and s.score_green == 70
    assert s.enabled_sources["vinted"] is False
    assert s.location_name == "Nowy Targ"
    assert s.manual_market_values == {"iPhone 13|128": 1800.0}
    assert window.settings.profit_repair.min_amount == 250
    # wartość ręczna od razu wpływa na wycenę
    vals = [v for o, v in window.model.rows() if o.parsed.model == "iPhone 13" and o.parsed.storage_gb == 128]
    assert vals and all(v.market.method == "wartość ręczna z ustawień" for v in vals)


def test_settings_cancel_changes_nothing(window):
    before = window.settings.to_json()
    dialog = window.open_settings()
    dialog.findChild(QtWidgets.QSpinBox, "score_green").setValue(99)
    dialog.reject()
    assert window.settings.to_json() == before


def test_parts_editor_edit_and_save(window):
    editor = window.open_parts_editor()
    t = editor.table
    row = next(r for r in range(t.rowCount())
               if t.item(r, 0).text() == "iPhone 13" and t.item(r, 1).data(Qt.ItemDataRole.UserRole) == "screen")
    t.item(row, 2).setText("199")
    editor.model_filter.setCurrentText("iPhone 13")
    assert not t.isRowHidden(row)
    r = editor._add()
    t.item(r, 0).setText("iPhone 13")
    t.item(r, 0).setData(Qt.ItemDataRole.UserRole, "iPhone 13")
    t.item(r, 1).setData(Qt.ItemDataRole.UserRole, Defect.FACE_ID.value)
    t.item(r, 2).setText("350")
    editor._save()
    parts = {(p.model, p.part): p.price for p in PartsRepository(window.conn).all()}
    assert parts[("iPhone 13", Defect.SCREEN)] == 199
    assert parts[("iPhone 13", Defect.FACE_ID)] == 350


def test_parts_editor_rejects_bad_price(window, monkeypatch):
    editor = window.open_parts_editor()
    editor.table.item(0, 2).setText("abc")
    warnings = []
    monkeypatch.setattr(QtWidgets.QMessageBox, "warning", lambda *a: warnings.append(a[2]))
    editor._save()
    assert warnings and "abc" in warnings[0]


def test_auto_refresh_timer_follows_settings(window):
    s = window.settings
    assert window.refresh_timer.isActive() and window.refresh_timer.interval() == s.refresh_minutes * 60_000
    assert "następne" in window.auto_label.text()
    import copy

    new = copy.deepcopy(s)
    new.refresh_minutes = 0
    window.apply_settings(new)
    assert not window.refresh_timer.isActive() and "wyłączone" in window.auto_label.text()
    new = copy.deepcopy(new)
    new.refresh_minutes = 5
    window.apply_settings(new)
    assert window.refresh_timer.interval() == 300_000


def test_notify_green_builds_message(window):
    from phonebot.services.post_scan import GreenOffer

    window.notify_green([GreenOffer(1, "iPhone 13 128 GB — 600 zł", 812.0, "u", "nowa"),
                         GreenOffer(2, "iPhone 12 64 GB — 500 zł", None, "u", "nowa")])
    title, body = window.last_notification
    assert "2 nowe zielone oferty" in title and "zysk 812 zł" in body


def test_settings_notify_tab(window):
    dialog = window.open_settings()
    dialog.tg_token.setText(" 123:ABC ")
    dialog.tg_chat.setText("42")
    dialog.ai_key.setText("sk-ant-test")
    dialog.ai_model.setCurrentText("claude-sonnet-5")
    dialog.findChild(QtWidgets.QCheckBox, "telegram_enabled").setChecked(True)
    dialog.findChild(QtWidgets.QCheckBox, "llm_enabled").setChecked(True)
    dialog.accept()
    s = SettingsRepository(window.conn).load()
    assert (s.telegram_bot_token, s.telegram_chat_id, s.telegram_enabled) == ("123:ABC", "42", True)
    assert (s.anthropic_api_key, s.llm_model, s.llm_enabled) == ("sk-ant-test", "claude-sonnet-5", True)
