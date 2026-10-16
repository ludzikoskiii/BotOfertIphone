"""Bezpieczne przechowywanie sekretów (token bota Telegram, chat ID, PIN, klucze API) poza kodem i poza plikiem ustawień.

* **Windows** — szyfrowanie DPAPI (``CryptProtectData``): odszyfrować może tylko Twoje konto Windows na tym komputerze;
  skopiowana baza na innym komputerze nie zdradzi tokenu.
* **Linux** — systemowy pęk kluczy (Secret Service: GNOME Keyring, KWallet, KeePassXC), odblokowywany razem
  z logowaniem do pulpitu. W bazie zostaje tylko odnośnik (``keyring:…``). Gdy pęku kluczy nie ma (np. lekki
  menedżer okien bez usługi kluczy), sekrety trafiają do osobnego pliku ``secrets.json`` w folderze danych, który
  może czytać wyłącznie Twoje konto (uprawnienia 600) — też nie do bazy.
* Inne systemy (macOS) — zapis zakodowany, ale NIE szyfrowany; ``describe()`` mówi to wprost w Ustawieniach.

Wybór można wymusić zmienną ``PHONEBOT_SECRET_STORE`` = ``keyring`` | ``file`` | ``plain`` (testy: ``file``).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import sys
import threading
from pathlib import Path

log = logging.getLogger(__name__)

_ENTROPY = b"PhoneBot secret v1"
_APP = "PhoneBot"
_lock = threading.Lock()
_keyring_ok: bool | None = None  # sprawdzane raz na uruchomienie
_cache: dict[str, str] = {}  # odczytane z pęku kluczy — bez ponownego pytania usługi przy każdym wczytaniu ustawień


def backend() -> str:
    """„dpapi” | „keyring” | „file” | „plain” — gdzie trafią nowe sekrety."""
    forced = os.environ.get("PHONEBOT_SECRET_STORE", "").strip().lower()
    if forced in ("keyring", "file", "plain"):
        return forced
    if sys.platform == "win32":
        return "dpapi"
    if sys.platform.startswith("linux"):
        return "keyring" if _keyring_available() else "file"
    return "plain"


def is_encrypted() -> bool:
    return backend() in ("dpapi", "keyring")


def describe() -> str:
    """Zdanie do Ustawień: gdzie i jak zapisane są token i chat ID."""
    return {
        "dpapi": "Token i chat ID są zapisywane osobno, zaszyfrowane kontem Windows (DPAPI) — nie w kodzie ani "
                 "w zwykłych ustawieniach.",
        "keyring": "Token i chat ID są w systemowym pęku kluczy (Secret Service: GNOME Keyring / KWallet), "
                   "zaszyfrowane i odblokowywane razem z logowaniem — w bazie programu jest tylko odnośnik.",
        "file": "Token i chat ID są w osobnym pliku secrets.json, który może czytać tylko Twoje konto (uprawnienia "
                "600) — nie w bazie. Pęk kluczy systemu (GNOME Keyring / KWallet) nie jest dostępny; po jego "
                "uruchomieniu zapisz ustawienia ponownie, a sekrety przeniosą się do niego.",
        "plain": "Token i chat ID są zapisywane osobno, poza zwykłymi ustawieniami, ale bez szyfrowania.",
    }[backend()]


def protect(value: str, name: str | None = None) -> str:
    """Zapisuje sekret; zwraca to, co trafia do bazy (szyfrogram DPAPI albo odnośnik do pęku kluczy / pliku).
    ``name`` — stała nazwa sekretu (wymagana na Linux, inaczej zapis zakodowany bez szyfrowania)."""
    kind = backend()
    if kind == "dpapi":
        return "dpapi:" + base64.b64encode(_dpapi(value.encode("utf-8"), encrypt=True)).decode("ascii") if value else ""
    if name and kind in ("keyring", "file"):
        if kind == "keyring":
            try:
                _keyring_set(name, value)
                _file_set(name, "")  # nie zostawiaj kopii w pliku po przeniesieniu do pęku kluczy
                return f"keyring:{name}" if value else ""
            except Exception as e:  # noqa: BLE001 — usługa zniknęła w trakcie: plik zamiast utraty sekretu
                log.warning("Pęk kluczy niedostępny (%s) — sekret zapisany w pliku z prawami tylko dla Ciebie", e)
        _file_set(name, value)
        return f"file:{name}" if value else ""
    return "plain:" + base64.b64encode(value.encode("utf-8")).decode("ascii") if value else ""


def unprotect(stored: str | None) -> str:
    """Odczyt; uszkodzony, z innego konta / komputera albo niedostępny pęk kluczy → pusty napis (wpisz ponownie)."""
    if not stored:
        return ""
    kind, _, payload = stored.partition(":")
    try:
        if kind == "dpapi":
            raw = base64.b64decode(payload)
            return _dpapi(raw, encrypt=False).decode("utf-8") if sys.platform == "win32" else ""
        if kind == "plain":
            return base64.b64decode(payload).decode("utf-8")
        if kind == "keyring":
            return _keyring_get(payload)
        if kind == "file":
            return _file_get(payload)
    except Exception as e:  # noqa: BLE001
        log.warning("Nie udało się odczytać sekretu (%s): %s", kind, e)
        return ""
    return ""


def needs_migration(stored: str | None) -> bool:
    """Sekret zapisany słabiej, niż pozwala ten komputer (np. „plain” na Linux z pękiem kluczy) — zapisz ponownie."""
    if not stored:
        return False
    kind = stored.partition(":")[0]
    order = {"plain": 0, "file": 1, "keyring": 2, "dpapi": 2}
    return order.get(kind, 0) < order.get(backend(), 0)


# ---------------------------------------------------------- Linux: pęk kluczy ---

def _secretstorage():
    """Import biblioteki pęku kluczy. Uszkodzona instalacja (np. ``cryptography`` z menedżera pakietów systemu)
    potrafi rzucić wyjątkiem spoza ``Exception`` — program ma wtedy działać dalej z plikiem."""
    try:
        import secretstorage
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as e:  # noqa: BLE001
        raise OSError(f"biblioteka pęku kluczy niedostępna: {e.__class__.__name__}") from None
    return secretstorage


def _collection(conn):
    ss = _secretstorage()
    col = ss.get_default_collection(conn)
    if col.is_locked():
        col.unlock()  # system pyta o hasło (zwykle odblokowany już przy logowaniu)
        if col.is_locked():
            raise OSError("pęk kluczy pozostał zablokowany")
    return col


def _keyring_available() -> bool:
    global _keyring_ok
    with _lock:
        if _keyring_ok is None:
            try:
                ss = _secretstorage()
                with ss.dbus_init() as conn:
                    _collection(conn)
                _keyring_ok = True
            except Exception as e:  # noqa: BLE001 — brak D-Bus, brak usługi, zablokowany: plik
                log.info("Pęk kluczy systemu niedostępny (%s) — sekrety w pliku z prawami tylko dla Ciebie", e)
                _keyring_ok = False
        return _keyring_ok


def _attrs(name: str) -> dict[str, str]:
    return {"application": _APP, "name": name}


def _keyring_set(name: str, value: str) -> None:
    ss = _secretstorage()
    with _lock, ss.dbus_init() as conn:
        col = _collection(conn)
        if value:
            col.create_item(f"PhoneBot: {name}", _attrs(name), value.encode("utf-8"), replace=True)
            _cache[name] = value
        else:
            for item in col.search_items(_attrs(name)):
                item.delete()
            _cache.pop(name, None)


def _keyring_get(name: str) -> str:
    with _lock:
        if name in _cache:
            return _cache[name]
    ss = _secretstorage()
    with _lock, ss.dbus_init() as conn:
        col = _collection(conn)
        item = next(iter(col.search_items(_attrs(name))), None)
        if item is None:
            return ""
        if item.is_locked():
            item.unlock()
        value = item.get_secret().decode("utf-8")
        _cache[name] = value
        return value


# ------------------------------------------------- Linux: plik tylko dla Ciebie ---

def _secrets_file() -> Path:
    from ..paths import data_dir

    return data_dir() / "secrets.json"


def _file_read() -> dict[str, str]:
    path = _secrets_file()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _file_set(name: str, value: str) -> None:
    with _lock:
        data = _file_read()
        if not value and name not in data:
            return
        if value:
            data[name] = value
        else:
            data.pop(name, None)
        path = _secrets_file()
        tmp = path.with_suffix(".tmp")
        # plik tworzony od razu z prawami 600 (bez chwili, w której inni mogliby go odczytać)
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)


def _file_get(name: str) -> str:
    with _lock:
        return str(_file_read().get(name, ""))


# ------------------------------------------------------------- Windows: DPAPI ---

def _dpapi(data: bytes, *, encrypt: bool) -> bytes:  # pragma: no cover — tylko Windows
    import ctypes
    from ctypes import wintypes

    class Blob(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32

    def blob(b: bytes) -> Blob:
        buf = ctypes.create_string_buffer(b, len(b))
        return Blob(len(b), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))

    src, entropy, out = blob(data), blob(_ENTROPY), Blob()
    fn = crypt32.CryptProtectData if encrypt else crypt32.CryptUnprotectData
    fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                   wintypes.DWORD, ctypes.POINTER(Blob)]
    fn.restype = wintypes.BOOL
    if not fn(ctypes.byref(src), None, ctypes.byref(entropy), None, None, 0x01, ctypes.byref(out)):
        raise OSError("DPAPI: nie udało się " + ("zaszyfrować" if encrypt else "odszyfrować"))
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)
