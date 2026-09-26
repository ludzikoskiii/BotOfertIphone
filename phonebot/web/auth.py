"""Logowanie do wersji na telefon: PIN (hash PBKDF2), sesje w bazie, ochrona CSRF, blokada po błędnych PIN-ach."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from collections import deque

PBKDF2_ROUNDS = 240_000
SESSION_DAYS = 30
MAX_FAILURES = 5  # błędnych PIN-ów w oknie ``FAILURE_WINDOW_S`` → blokada
FAILURE_WINDOW_S = 600
LOCK_S = 300
MIN_PIN_LEN = 4
_SESSIONS_KEY = "web_sessions"


def hash_pin(pin: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${base64.b64encode(salt).decode()}${base64.b64encode(digest).decode()}"


def verify_pin(pin: str, stored: str) -> bool:
    try:
        algo, rounds, salt, digest = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", pin.encode("utf-8"), base64.b64decode(salt), int(rounds))
        return hmac.compare_digest(got, base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Sessions:
    """Sesje zapamiętane w bazie (tylko skróty tokenów) — telefon nie musi logować się po restarcie programu."""

    def __init__(self, conn_factory):
        self._conn_factory = conn_factory
        self._lock = threading.Lock()

    def _load(self, conn: sqlite3.Connection) -> dict[str, float]:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (_SESSIONS_KEY,)).fetchone()
        try:
            data = json.loads(row[0]) if row else {}
        except (json.JSONDecodeError, TypeError):
            data = {}
        now = time.time()
        return {k: float(v) for k, v in data.items() if isinstance(v, (int, float)) and v > now}

    def _save(self, conn: sqlite3.Connection, data: dict[str, float]) -> None:
        conn.execute("INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = "
                     "excluded.value", (_SESSIONS_KEY, json.dumps(data)))

    def create(self) -> str:
        token = secrets.token_urlsafe(32)
        with self._lock:
            conn = self._conn_factory()
            try:
                data = self._load(conn)
                data[_digest(token)] = time.time() + SESSION_DAYS * 86400
                self._save(conn, data)
            finally:
                conn.close()
        return token

    def valid(self, token: str | None) -> bool:
        if not token:
            return False
        with self._lock:
            conn = self._conn_factory()
            try:
                return _digest(token) in self._load(conn)
            finally:
                conn.close()

    def revoke(self, token: str | None) -> None:
        with self._lock:
            conn = self._conn_factory()
            try:
                data = self._load(conn)
                data.pop(_digest(token or ""), None)
                self._save(conn, data)
            finally:
                conn.close()

    def revoke_all(self) -> None:
        with self._lock:
            conn = self._conn_factory()
            try:
                self._save(conn, {})
            finally:
                conn.close()


def csrf_token(session: str) -> str:
    """Token formularzy powiązany z sesją — obca strona nie wykona akcji w Twoim imieniu."""
    return hmac.new(session.encode(), b"phonebot-csrf", hashlib.sha256).hexdigest()[:32]


class LoginThrottle:
    """Po ``MAX_FAILURES`` błędnych PIN-ach w 10 minut logowanie jest zablokowane na 5 minut."""

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._failures: deque[float] = deque()
        self._locked_until = 0.0
        self._lock = threading.Lock()

    def locked_for(self) -> int:
        with self._lock:
            return max(0, int(self._locked_until - self._clock() + 0.999))

    def failure(self) -> None:
        with self._lock:
            now = self._clock()
            self._failures.append(now)
            while self._failures and self._failures[0] < now - FAILURE_WINDOW_S:
                self._failures.popleft()
            if len(self._failures) >= MAX_FAILURES:
                self._locked_until = now + LOCK_S
                self._failures.clear()

    def success(self) -> None:
        with self._lock:
            self._failures.clear()
