"""Profile powiadomień w programie (Ustawienia → Powiadomienia) i na telefonie (/powiadomienia); wątek komend bota."""
from __future__ import annotations

import re

import pytest

from phonebot.core.catalog import generations
from phonebot.core.notify_profiles import NotifyProfile
from phonebot.core.settings import Settings
from phonebot.storage.db import open_database
from phonebot.storage.repositories import NotifyProfileRepository
from phonebot.web.auth import hash_pin
from phonebot.web.server import WebServer

from .sample_data import build_sample_db
from .test_web import _client, _login


@pytest.fixture
def server(tmp_path):
    conn, _ = build_sample_db(tmp_path / "w.sqlite3")
    settings = Settings(web_enabled=True, web_port=0, web_pin_hash=hash_pin("2468"))
    srv = WebServer(tmp_path / "w.sqlite3", settings)
    srv.start(settings)
    yield srv, conn
    srv.stop()
    conn.close()


@pytest.fixture
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


def _csrf(text: str) -> str:
    return re.search(r'name="csrf" value="([0-9a-f]+)"', text).group(1)


# ---------------------------------------------------------------- telefon ---

def test_phone_list_toggle_and_pause(server):
    srv, conn = server
    with _client(srv) as c:
        assert c.get("/powiadomienia").url.path == "/login"  # za PIN-em
        _login(c)
        page = c.get("/powiadomienia")
        assert "Domyślny" in page.text and 'class="on" href="/powiadomienia"' in page.text
        assert "Wysłane w 7 dni: <b>0</b>" in page.text and "Telegram nie jest jeszcze połączony" in page.text
        default = NotifyProfileRepository(conn).all()[0]
        assert c.post("/powiadomienia", data={"a": "toggle", "id": default.id}).status_code == 403  # bez CSRF
        csrf = _csrf(page.text)
        c.post("/powiadomienia", data={"a": "toggle", "id": default.id, "csrf": csrf})
        assert NotifyProfileRepository(conn).get(default.id).enabled is not default.enabled
        paused = c.post("/powiadomienia", data={"a": "pause", "h": "2", "csrf": csrf})
        assert "Powiadomienia wstrzymane do" in paused.text and "▶ Wznów" in paused.text
        resumed = c.post("/powiadomienia", data={"a": "resume", "csrf": csrf})
        assert "Powiadomienia działają" in resumed.text
        assert c.post("/powiadomienia", data={"a": "pause", "h": "x", "csrf": csrf}).status_code == 400


def test_phone_profile_edit_preview_save_test_delete(server, monkeypatch):
    srv, conn = server
    gen13 = generations()["13"]
    with _client(srv) as c:
        _login(c)
        new = c.get("/powiadomienia/nowy")
        assert "Z ostatnich 24 godzin ten profil wysłałby" in new.text and "cała generacja" in new.text
        csrf = _csrf(new.text)
        form = {"csrf": csrf, "name": "Resell – iPhone 13", "enabled": "1", "gen": "13", "verdicts": ["KUPUJ"],
                "price_max": "2 500", "max_risk": "low", "skip_hard_flags": "1", "exclude_words": "icloud, atrapa",
                "country": "pl", "quiet_start": "22", "quiet_end": "7"}
        pv = c.post("/powiadomienia/nowy", data={**form, "a": "preview"})
        assert pv.status_code == 200 and 'id="preview"' in pv.text and "iPhone 13 mini" in pv.text
        assert not NotifyProfileRepository(conn).all()[1:]  # podgląd nie zapisuje
        saved = c.post("/powiadomienia/nowy", data={**form, "a": "save"})
        assert saved.url.path == "/powiadomienia" and "Zapisano profil." in saved.text
        p = NotifyProfileRepository(conn).all()[-1]
        assert (p.name, p.models, p.verdicts, p.price_max, p.country) == (
            "Resell – iPhone 13", gen13, ["KUPUJ"], 2500.0, "pl")
        assert p.exclude_words == ["icloud", "atrapa"] and p.skip_hard_flags and not p.only_with_parts
        assert "iPhone 13 (cała gen.)" in saved.text
        edit = c.get(f"/powiadomienia/{p.id}")
        assert f'value="{gen13[0]}" data-gen="13" checked' in edit.text
        # test bez połączonego Telegrama — czytelny błąd zamiast wyjątku
        nope = c.post(f"/powiadomienia/{p.id}", data={**form, "a": "test"})
        assert "Test nie wysłany" in nope.text

        sent = []

        class Client:
            def __init__(self, token, chat_id=""):
                pass

            def send(self, text, *, preview_url=None, buttons=None):
                sent.append(text)

        import phonebot.services.notifications as notifications

        monkeypatch.setattr(notifications, "TelegramClient", Client)
        srv.app.settings.telegram_bot_token, srv.app.settings.telegram_chat_id = "t", "1"
        ok = c.post(f"/powiadomienia/{p.id}", data={**form, "a": "test"})
        assert "✅ wysłano test" in ok.text and len(sent) == 1 and ("TEST" in sent[0] or "Test profilu" in sent[0])
        bad = c.post(f"/powiadomienia/{p.id}", data={**form, "price_max": "dużo", "a": "save"})
        assert bad.status_code == 400 and "niepoprawna liczba" in bad.text
        gone = c.post(f"/powiadomienia/{p.id}", data={"csrf": csrf, "a": "delete"})
        assert "Usunięto profil." in gone.text and NotifyProfileRepository(conn).get(p.id) is None
        assert c.get(f"/powiadomienia/{p.id}").status_code == 404


# ------------------------------------------------------------- komputer ---

def test_settings_profiles_panel_editor_and_test(tmp_path, qapp):
    from PySide6.QtCore import Qt

    from phonebot.ui.notify_profiles_ui import ProfileDialog
    from phonebot.ui.settings_dialog import SettingsDialog

    conn, _ = build_sample_db(tmp_path / "d.sqlite3")
    dialog = SettingsDialog(Settings(), conn=conn, db_path=tmp_path / "d.sqlite3")
    panel = dialog.profiles_panel
    assert panel.table.rowCount() == 1 and panel.table.item(0, 1).text() == "Domyślny"  # stare ustawienia
    editor = ProfileDialog(NotifyProfile(name="Naprawa – blisko domu"), settings=Settings(),
                           db_path=tmp_path / "d.sqlite3", make_test=lambda p: (lambda: f"wysłano: {p.name}"))
    editor.wait_preview()
    assert "Z ostatnich 24 godzin ten profil wysłałby" in editor.preview_label.text()
    all_count = editor.preview_result.count
    editor.models.generation_item("iPhone 13").setCheckState(0, Qt.CheckState.Checked)  # cała generacja
    editor.radius.setValue(30)
    editor.wait_preview()
    assert editor.read().models == generations()["13"] and editor.read().radius_km == 30
    assert editor.preview_result.count <= all_count
    editor.test_btn.click()
    for _ in range(300):
        qapp.processEvents()
        if editor._test_worker is None:
            break
        import time

        time.sleep(0.01)
    assert editor.test_status.text() == "✅ wysłano: Naprawa – blisko domu"
    assert panel.open_editor(editor.profile, dialog=editor)
    assert panel.table.rowCount() == 2 and "do 30 km" in panel.table.item(1, 3).text()
    panel.table.item(1, 0).setCheckState(Qt.CheckState.Unchecked)  # włącznik w tabeli — od razu w bazie
    assert not NotifyProfileRepository(conn).all()[1].enabled
    dialog.close()
    editor.close()
    conn.close()


def test_window_starts_bot_only_when_telegram_configured(tmp_path, qapp):
    import copy

    from phonebot.ui.main_window import MainWindow

    conn = open_database(tmp_path / "m.sqlite3")
    win = MainWindow(conn, tmp_path / "m.sqlite3", thumbs_dir=tmp_path)
    try:
        assert win.bot is None
        s = copy.deepcopy(win.settings)
        s.telegram_enabled, s.telegram_bot_token, s.telegram_chat_id = True, "t", "1"
        win.apply_settings(s)
        assert win.bot is not None and win.bot.started  # wątek (w testach bez sieci)
        win.set_last_refresh(None)
        assert "green" in win.bot.app_state()
        dialog = win.open_settings()
        assert dialog.profiles_panel is not None and dialog.profiles_panel.table.rowCount() == 1
        dialog.reject()
        s = copy.deepcopy(win.settings)
        s.telegram_enabled = False
        win.apply_settings(s)
        assert win.bot is None
    finally:
        win._quitting = True
        win.close()
        conn.close()


def test_bot_thread_follows_new_token_without_restart(tmp_path):
    from phonebot.services.telegram_bot import BotThread

    open_database(tmp_path / "b.sqlite3").close()
    made = []

    class Client:
        def __init__(self, token, chat_id=""):
            made.append(token)

        def get_updates(self, offset, timeout=25):
            if len(made) == 1:
                thread.update_settings(Settings(telegram_enabled=True, telegram_bot_token="t2", telegram_chat_id="1"))
            else:
                thread.stop()
            return []

    thread = BotThread(tmp_path / "b.sqlite3", Settings(telegram_enabled=True, telegram_bot_token="t1",
                                                         telegram_chat_id="1"), client_factory=Client)
    thread._run()
    assert made == ["t1", "t2"]
