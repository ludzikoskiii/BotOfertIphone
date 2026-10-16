"""Sekrety na Linux: pęk kluczy systemu (Secret Service) albo plik tylko dla właściciela — nigdy w bazie."""
from __future__ import annotations

import os
import stat
import sys
import types
from contextlib import contextmanager

import pytest

from phonebot.core import secret_store
from phonebot.core.settings import Settings
from phonebot.storage.db import open_database
from phonebot.storage.repositories import SettingsRepository


class FakeItem:
    def __init__(self, store, attrs, secret):
        self.store, self.attrs, self.secret = store, attrs, secret

    def is_locked(self):
        return False

    def get_secret(self):
        return self.secret

    def delete(self):
        self.store.items.remove(self)


class FakeCollection:
    """Domyślna kolekcja Secret Service (jak GNOME Keyring) w pamięci."""

    def __init__(self):
        self.items: list[FakeItem] = []
        self.locked = False

    def is_locked(self):
        return self.locked

    def unlock(self):
        return self.locked

    def create_item(self, label, attrs, secret, replace=False):
        if replace:
            self.items = [i for i in self.items if i.attrs != attrs]
        self.items.append(FakeItem(self, dict(attrs), secret))

    def search_items(self, attrs):
        return [i for i in self.items if all(i.attrs.get(k) == v for k, v in attrs.items())]


@pytest.fixture
def keyring(monkeypatch, tmp_path):
    col = FakeCollection()
    fake = types.ModuleType("secretstorage")

    @contextmanager
    def dbus_init():
        yield object()

    fake.dbus_init = dbus_init
    fake.get_default_collection = lambda conn: col
    monkeypatch.setitem(sys.modules, "secretstorage", fake)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("PHONEBOT_SECRET_STORE", raising=False)
    monkeypatch.setenv("PHONEBOT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(secret_store, "_keyring_ok", None)
    monkeypatch.setattr(secret_store, "_cache", {})
    return col


@pytest.fixture
def no_keyring(monkeypatch, tmp_path):
    fake = types.ModuleType("secretstorage")

    @contextmanager
    def dbus_init():
        raise OSError("brak magistrali D-Bus sesji")
        yield  # pragma: no cover

    fake.dbus_init = dbus_init
    monkeypatch.setitem(sys.modules, "secretstorage", fake)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("PHONEBOT_SECRET_STORE", raising=False)
    monkeypatch.setenv("PHONEBOT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(secret_store, "_keyring_ok", None)
    monkeypatch.setattr(secret_store, "_cache", {})


def _db_text(conn) -> str:
    return "\n".join(str(tuple(r)) for r in conn.execute("SELECT key, value FROM settings"))


def test_keyring_keeps_token_out_of_database(keyring, tmp_path):
    assert secret_store.backend() == "keyring" and secret_store.is_encrypted()
    conn = open_database(tmp_path / "a.sqlite3")
    SettingsRepository(conn).save(Settings(telegram_bot_token="123:SEKRET", telegram_chat_id="42"))
    assert "123:SEKRET" not in _db_text(conn)
    assert SettingsRepository(conn).get_value("secret:telegram_bot_token").startswith("keyring:phonebot-")
    secret_store._cache.clear()  # odczyt prosto z usługi (np. po ponownym uruchomieniu)
    s = SettingsRepository(conn).load()
    assert (s.telegram_bot_token, s.telegram_chat_id) == ("123:SEKRET", "42")
    assert [i.attrs["application"] for i in keyring.items] == ["PhoneBot", "PhoneBot"]
    s.telegram_bot_token = ""
    SettingsRepository(conn).save(s)  # usunięty token znika też z pęku kluczy
    assert len(keyring.items) == 1 and not SettingsRepository(conn).load().telegram_bot_token
    assert not (tmp_path / "home" / "secrets.json").exists()  # bez kopii w pliku
    conn.close()


def test_file_fallback_is_private_and_separate_from_database(no_keyring, tmp_path):
    assert secret_store.backend() == "file" and not secret_store.is_encrypted()
    assert "600" in secret_store.describe()
    conn = open_database(tmp_path / "b.sqlite3")
    SettingsRepository(conn).save(Settings(telegram_bot_token="123:PLIK"))
    path = tmp_path / "home" / "secrets.json"
    if os.name == "posix":  # prawa dostępu w stylu Unix (testy uruchamiane też na Windows)
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "123:PLIK" not in _db_text(conn) and "123:PLIK" in path.read_text()
    assert SettingsRepository(conn).load().telegram_bot_token == "123:PLIK"
    conn.close()


def test_two_databases_do_not_share_secrets(no_keyring, tmp_path):
    a, b = open_database(tmp_path / "a.sqlite3"), open_database(tmp_path / "b.sqlite3")
    SettingsRepository(a).save(Settings(telegram_bot_token="AAA"))
    SettingsRepository(b).save(Settings(telegram_bot_token="BBB"))
    assert SettingsRepository(a).load().telegram_bot_token == "AAA"
    assert SettingsRepository(b).load().telegram_bot_token == "BBB"
    a.close()
    b.close()


def test_old_plain_and_file_entries_move_to_keyring(keyring, tmp_path):
    conn = open_database(tmp_path / "m.sqlite3")
    repo = SettingsRepository(conn)
    repo.save(Settings())
    repo.set_value("secret:telegram_bot_token", secret_store.protect("STARY"))  # zapis starszej wersji (bez nazwy)
    assert repo.get_value("secret:telegram_bot_token").startswith("plain:")
    assert repo.load().telegram_bot_token == "STARY"
    assert repo.get_value("secret:telegram_bot_token").startswith("keyring:")
    assert "STARY" not in _db_text(conn) and len(keyring.items) == 1
    conn.close()


def test_keyring_unavailable_later_does_not_wipe_reference(keyring, tmp_path, monkeypatch):
    conn = open_database(tmp_path / "k.sqlite3")
    SettingsRepository(conn).save(Settings(telegram_bot_token="TOKEN"))
    ref = SettingsRepository(conn).get_value("secret:telegram_bot_token")
    keyring.locked = True  # pęk kluczy zablokowany (np. inna sesja)
    monkeypatch.setattr(secret_store, "_cache", {})
    monkeypatch.setattr(secret_store, "_keyring_ok", False)
    s = SettingsRepository(conn).load()
    assert s.telegram_bot_token == ""
    SettingsRepository(conn).save(s)  # zapis innych ustawień nie kasuje odnośnika
    assert SettingsRepository(conn).get_value("secret:telegram_bot_token") == ref
    conn.close()


def test_broken_keyring_library_falls_back_to_file(monkeypatch, tmp_path):
    class Panic(BaseException):  # jak PanicException z uszkodzonej biblioteki cryptography (spoza Exception)
        pass

    class Finder:
        def find_spec(self, name, path=None, target=None):
            if name == "secretstorage":
                raise Panic("Python API call failed")
            return None

    monkeypatch.delitem(sys.modules, "secretstorage", raising=False)
    monkeypatch.setattr(sys, "meta_path", [Finder(), *sys.meta_path])
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("PHONEBOT_SECRET_STORE", raising=False)
    monkeypatch.setenv("PHONEBOT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(secret_store, "_keyring_ok", None)
    assert secret_store.backend() == "file"
    assert secret_store.unprotect(secret_store.protect("X", "n/x")) == "X"
