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
    """,
    # v2 — wyniki analizy AI (opcjonalnej)
    """
    ALTER TABLE offers ADD COLUMN ai_defects TEXT;
    ALTER TABLE offers ADD COLUMN ai_flags TEXT;
    ALTER TABLE offers ADD COLUMN ai_note TEXT;
    ALTER TABLE offers ADD COLUMN ai_checked_at TEXT;
    CREATE INDEX idx_offers_first_seen ON offers (first_seen);
    """,
    # v3 — OLX usunięty ze źródeł (blokuje automatyczne pobieranie): stare oferty znikają z listy,
    # ale ich ceny zostają w danych rynkowych (okno czasowe wyceny)
    """
    UPDATE offers SET is_active = 0 WHERE source = 'olx';
    """,
    # v4 — odrzucone ogłoszenia (z powodem) i ręcznie przywrócone („To jest telefon”)
    """
    CREATE TABLE rejected_offers (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        source       TEXT NOT NULL,
        source_id    TEXT NOT NULL,
        url          TEXT NOT NULL,
        title        TEXT NOT NULL,
        price        REAL NOT NULL,
        stage        TEXT NOT NULL,
        reason       TEXT NOT NULL,
        keyword      TEXT,
        raw_json     TEXT NOT NULL,
        rejected_at  TEXT NOT NULL,
        UNIQUE (source, source_id)
    );
    CREATE INDEX idx_rejected_time ON rejected_offers (rejected_at);

    CREATE TABLE filter_whitelist (
        source       TEXT NOT NULL,
        source_id    TEXT NOT NULL,
        title        TEXT NOT NULL,
        stage        TEXT,
        keyword      TEXT,
        restored_at  TEXT NOT NULL,
        PRIMARY KEY (source, source_id)
    );
    """,
    # v5 — sprzedawcy: kraj (Vinted pokazuje oferty z zagranicy) i sprzedawcy seryjni tanich „iPhone'ów”
    """
    ALTER TABLE offers ADD COLUMN seller_id TEXT;
    CREATE INDEX idx_offers_seller ON offers (source, seller_id);

    CREATE TABLE sellers (
        source         TEXT NOT NULL,
        seller_id      TEXT NOT NULL,
        login          TEXT,
        country_code   TEXT,
        business       INTEGER,
        checked_at     TEXT,
        serial         INTEGER NOT NULL DEFAULT 0,
        serial_reason  TEXT,
        serial_at      TEXT,
        PRIMARY KEY (source, seller_id)
    );
    """,
    # v6 — lokalne AI: Twoje oznaczenia do nauki klasyfikatora i wyniki warstw (tekst, zdjęcie) po ID ogłoszenia
    """
    CREATE TABLE ml_labels (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        source      TEXT NOT NULL,
        source_id   TEXT NOT NULL,
        title       TEXT NOT NULL,
        label       TEXT NOT NULL,
        origin      TEXT NOT NULL,
        created_at  TEXT NOT NULL,
        UNIQUE (source, source_id)
    );
    INSERT OR IGNORE INTO ml_labels (source, source_id, title, label, origin, created_at)
        SELECT source, source_id, title, 'phone', 'user', restored_at FROM filter_whitelist;
    INSERT OR IGNORE INTO ml_labels (source, source_id, title, label, origin, created_at)
        SELECT source, source_id, title, 'accessory', 'hidden', last_seen FROM offers WHERE status = 'hidden';

    CREATE TABLE ai_results (
        source       TEXT NOT NULL,
        source_id    TEXT NOT NULL,
        text_label   TEXT,
        text_conf    REAL,
        text_probs   TEXT,
        text_model   TEXT,
        photo_url    TEXT,
        photo_label  TEXT,
        photo_conf   REAL,
        photo_probs  TEXT,
        photo_model  TEXT,
        photo_at     TEXT,
        photo_error  TEXT,
        PRIMARY KEY (source, source_id)
    );
    """,
    # v7 — etap 3: opis ze strony oferty i wynik lokalnego modelu językowego (Ollama); wyniki usuniętej
    # płatnej analizy Claude są czyszczone (klucz API usuwa z ustawień SettingsRepository.load)
    """
    UPDATE offers SET ai_defects = NULL, ai_flags = NULL, ai_note = NULL, ai_checked_at = NULL;
    ALTER TABLE offers ADD COLUMN page_description TEXT;
    ALTER TABLE offers ADD COLUMN page_checked_at TEXT;
    ALTER TABLE offers ADD COLUMN page_error TEXT;
    ALTER TABLE ai_results ADD COLUMN desc_hash TEXT;
    ALTER TABLE ai_results ADD COLUMN desc_json TEXT;
    ALTER TABLE ai_results ADD COLUMN desc_model TEXT;
    ALTER TABLE ai_results ADD COLUMN desc_at TEXT;
    ALTER TABLE ai_results ADD COLUMN desc_error TEXT;
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
