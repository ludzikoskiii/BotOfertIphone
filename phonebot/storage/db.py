"""Połączenie z SQLite i migracje schematu."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

log = logging.getLogger(__name__)

MIGRATIONS: list[str] = [
    # v1 — schemat początkowy
    """
    CREATE TABLE offers (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        source          TEXT NOT NULL,
        source_id       TEXT NOT NULL,
        url             TEXT NOT NULL,
        title           TEXT NOT NULL,
        description     TEXT NOT NULL DEFAULT '',
        price           REAL NOT NULL,
        currency        TEXT NOT NULL DEFAULT 'PLN',
        city            TEXT,
        region          TEXT,
        lat             REAL,
        lon             REAL,
        photos          TEXT NOT NULL DEFAULT '[]',
        params          TEXT NOT NULL DEFAULT '{}',
        shipping        INTEGER,
        negotiable_raw  INTEGER,
        created_at      TEXT,
        model           TEXT,
        storage_gb      INTEGER,
        condition       TEXT NOT NULL,
        defects         TEXT NOT NULL DEFAULT '[]',
        flags           TEXT NOT NULL DEFAULT '[]',
        battery_health  INTEGER,
        negotiable      INTEGER,
        dedup_key       TEXT,
        status          TEXT NOT NULL DEFAULT 'new',
        is_active       INTEGER NOT NULL DEFAULT 1,
        first_seen      TEXT NOT NULL,
        last_seen       TEXT NOT NULL,
        notified_at     TEXT,
        UNIQUE (source, source_id)
    );
    CREATE INDEX idx_offers_model ON offers (model, storage_gb, last_seen);
    CREATE INDEX idx_offers_dedup ON offers (dedup_key);
    CREATE INDEX idx_offers_status ON offers (status, is_active);

    CREATE TABLE price_history (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        offer_id  INTEGER NOT NULL REFERENCES offers (id) ON DELETE CASCADE,
        price     REAL NOT NULL,
        seen_at   TEXT NOT NULL
    );
    CREATE INDEX idx_price_history_offer ON price_history (offer_id, seen_at);

    CREATE TABLE parts_prices (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        model  TEXT NOT NULL,
        part   TEXT NOT NULL,
        price  REAL NOT NULL,
        note   TEXT NOT NULL DEFAULT '',
        UNIQUE (model, part)
    );

    CREATE TABLE settings (
        key    TEXT PRIMARY KEY,
        value  TEXT NOT NULL
    );

    CREATE TABLE fetch_runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        source        TEXT NOT NULL,
        started_at    TEXT NOT NULL,
        finished_at   TEXT,
        status        TEXT NOT NULL DEFAULT 'running',
        offers_found  INTEGER NOT NULL DEFAULT 0,
        new_offers    INTEGER NOT NULL DEFAULT 0,
        error         TEXT
    );

    CREATE TABLE http_cache (
        url         TEXT PRIMARY KEY,
        fetched_at  TEXT NOT NULL,
        status      INTEGER NOT NULL,
        body        BLOB NOT NULL
    );
    """,
]


def connect(path: str | Path) -> sqlite3.Connection:
    """Nowe połączenie (każdy wątek powinien mieć własne)."""
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    if str(path) != ":memory:":
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def migrate(conn: sqlite3.Connection) -> int:
    """Stosuje brakujące migracje. Zwraca wersję schematu."""
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for idx, script in enumerate(MIGRATIONS[version:], start=version + 1):
        log.info("Migracja bazy do wersji %d", idx)
        conn.execute("BEGIN")
        try:
            for stmt in (s.strip() for s in script.split(";")):
                if stmt:
                    conn.execute(stmt)
            conn.execute(f"PRAGMA user_version = {idx}")
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
    return len(MIGRATIONS)


def open_database(path: str | Path) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn
