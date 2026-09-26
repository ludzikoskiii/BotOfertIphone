"""Bezpieczne przechowywanie sekretów (token bota Telegram, chat ID, PIN) poza kodem i poza plikiem ustawień.

Windows: szyfrowanie DPAPI (``CryptProtectData``) — odszyfrować może tylko Twoje konto Windows na tym
komputerze; skopiowana baza na innym komputerze nie zdradzi tokenu. Inne systemy (testy, wersja
deweloperska): zapis zakodowany, ale NIE szyfrowany — ``is_encrypted()`` mówi, który przypadek zachodzi.
"""
from __future__ import annotations

import base64
import sys

_ENTROPY = b"PhoneBot secret v1"


def is_encrypted() -> bool:
    return sys.platform == "win32"


def protect(value: str) -> str:
    if not value:
        return ""
    data = value.encode("utf-8")
    if sys.platform == "win32":
        return "dpapi:" + base64.b64encode(_dpapi(data, encrypt=True)).decode("ascii")
    return "plain:" + base64.b64encode(data).decode("ascii")


def unprotect(stored: str | None) -> str:
    """Odczyt; uszkodzony albo z innego konta Windows → pusty napis (trzeba wpisać ponownie)."""
    if not stored:
        return ""
    kind, _, payload = stored.partition(":")
    try:
        raw = base64.b64decode(payload)
        if kind == "dpapi":
            return _dpapi(raw, encrypt=False).decode("utf-8") if sys.platform == "win32" else ""
        if kind == "plain":
            return raw.decode("utf-8")
    except (ValueError, OSError):
        return ""
    return ""


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
