"""Porządki w bazie i w pamięci podręcznej zdjęć — w nocy, w tle (wątek roboczy z własnym połączeniem).

* przebiegi pobierania (``fetch_runs``) starsze niż 30 dni — status źródeł potrzebuje tylko ostatnich, a przy
  odświeżaniu co ~2 min tabela rosła o ~3000 wierszy dziennie;
* wysłane / pominięte powiadomienia Telegram starsze niż 90 dni (statystyka profili sięga tygodnia);
* wyniki AI i skróty zdjęć ogłoszeń, których nie ma już w bazie (stare oferty usuwane po oknie wyceny);
* ``PRAGMA optimize`` (statystyki zapytań) i punkt kontrolny WAL;
* ``VACUUM``, gdy wolne miejsce w pliku bazy to ≥ 15 % i ≥ 5 MB — plik bazy się zmniejsza;
* miniatury i zdjęcia: nieużywane od 30 dni oraz najstarsze ponad limit rozmiaru (Ustawienia → Wydajność).

Nic z tego nie zmienia wyceny: usuwane są wyłącznie dane, których program już nie czyta.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

log = logging.getLogger(__name__)

KEY = "maintenance_last"
RUNS_KEEP_DAYS = 30
OUTBOX_KEEP_DAYS = 90
VACUUM_MIN_FREE_PCT = 15
VACUUM_MIN_FREE_MB = 5
CACHE_MAX_AGE_DAYS = 30


@dataclass
class MaintenanceReport:
    at: str = ""
    deleted: dict[str, int] = field(default_factory=dict)
    vacuumed: bool = False
    db_mb_before: float = 0.0
    db_mb_after: float = 0.0
    cache_removed: int = 0
    cache_mb_after: float = 0.0
    seconds: float = 0.0

    def summary(self) -> str:
        rows = sum(self.deleted.values())
        text = f"usunięto {rows} zbędnych wierszy"
        if self.vacuumed:
            text += f", baza {self.db_mb_before:.1f} → {self.db_mb_after:.1f} MB".replace(".", ",")
        if self.cache_removed:
            text += f", {self.cache_removed} starych zdjęć z dysku"
        return text


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def db_file_mb(conn: sqlite3.Connection) -> float:
    """Rozmiar bazy (strony × rozmiar strony, bez pliku WAL)."""
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    size = conn.execute("PRAGMA page_size").fetchone()[0]
    return pages * size / 2**20


def free_pct(conn: sqlite3.Connection) -> tuple[float, float]:
    """(wolne miejsce w %, wolne MB) — strony zwolnione po usunięciu danych, do odzyskania przez VACUUM."""
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    size = conn.execute("PRAGMA page_size").fetchone()[0]
    return (100 * free / pages if pages else 0.0), free * size / 2**20


def prune_cache_dir(directory: Path | None, *, max_age_days: int = CACHE_MAX_AGE_DAYS,
                    max_mb: float | None = 300, now: float | None = None, pattern: str = "*.jpg") -> tuple[int, float]:
    """Miniatury i zdjęcia na dysku: nieużywane od ``max_age_days`` dni, potem najstarsze (ostatnie użycie) ponad
    ``max_mb`` (``None`` = bez limitu). Zwraca (usunięte pliki, MB po porządkach). Używana miniatura ma odświeżaną
    datę (``touch``)."""
    if directory is None or not directory.is_dir():
        return 0, 0.0
    now = now or time.time()
    cutoff = now - max_age_days * 86400
    files: list[tuple[float, int, Path]] = []
    removed = 0
    for f in directory.glob(pattern):
        try:
            st = f.stat()
        except OSError:
            continue
        if st.st_mtime < cutoff:
            f.unlink(missing_ok=True)
            removed += 1
        else:
            files.append((st.st_mtime, st.st_size, f))
    total = sum(size for _, size, _ in files)
    limit = float("inf") if max_mb is None else max(0.0, max_mb) * 2**20
    if total > limit:
        for _, size, f in sorted(files):  # najdawniej używane najpierw
            if total <= limit:
                break
            try:
                f.unlink(missing_ok=True)
            except OSError:
                continue
            total -= size
            removed += 1
    return removed, total / 2**20


def cache_size_mb(directory: Path | None) -> float:
    if directory is None or not directory.is_dir():
        return 0.0
    total = 0
    for f in directory.glob("*.jpg"):
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total / 2**20


def prune_tables(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, int]:
    """Usuwa dane, których program już nie czyta (szczegóły w opisie modułu)."""
    now = now or datetime.now(UTC)
    out: dict[str, int] = {}
    out["fetch_runs"] = conn.execute(
        "DELETE FROM fetch_runs WHERE started_at < ? AND id NOT IN (SELECT MAX(id) FROM fetch_runs GROUP BY source)",
        (_iso(now - timedelta(days=RUNS_KEEP_DAYS)),)).rowcount
    out["telegram_outbox"] = conn.execute(
        "DELETE FROM telegram_outbox WHERE status != 'pending' AND created_at < ?",
        (_iso(now - timedelta(days=OUTBOX_KEEP_DAYS)),)).rowcount
    gone = ("NOT EXISTS (SELECT 1 FROM offers o WHERE o.source = {t}.source AND o.source_id = {t}.source_id) "
            "AND NOT EXISTS (SELECT 1 FROM rejected_offers r WHERE r.source = {t}.source "
            "AND r.source_id = {t}.source_id)")
    out["ai_results"] = conn.execute(f"DELETE FROM ai_results WHERE {gone.format(t='ai_results')}").rowcount
    out["photo_hashes"] = conn.execute(f"DELETE FROM photo_hashes WHERE {gone.format(t='photo_hashes')}").rowcount
    return {k: v for k, v in out.items() if v}


def nightly_maintenance(conn: sqlite3.Connection, settings=None, *, now: datetime | None = None,
                        force_vacuum: bool = False, cache_dir: Path | None = None) -> dict:
    """Porządki (patrz opis modułu). Zapisuje czas i wynik w ustawieniach (``maintenance_last``)."""
    import json

    from ..storage.repositories import SettingsRepository

    t0 = time.perf_counter()
    now = now or datetime.now(UTC)
    rep = MaintenanceReport(at=_iso(now), db_mb_before=round(db_file_mb(conn), 2))
    rep.deleted = prune_tables(conn, now)
    try:
        conn.execute("PRAGMA optimize")
    except sqlite3.DatabaseError:
        log.debug("PRAGMA optimize", exc_info=True)
    pct, mb = free_pct(conn)
    if force_vacuum or (pct >= VACUUM_MIN_FREE_PCT and mb >= VACUUM_MIN_FREE_MB):
        try:
            conn.execute("VACUUM")
            rep.vacuumed = True
        except sqlite3.OperationalError as e:  # baza zajęta (np. długie zapytanie) — spróbujemy następnej nocy
            log.info("VACUUM pominięty: %s", e)
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.DatabaseError:
        log.debug("wal_checkpoint", exc_info=True)
    rep.db_mb_after = round(db_file_mb(conn), 2)
    if cache_dir is not None:
        limit = getattr(settings, "cache_max_mb", 300) if settings is not None else 300
        rep.cache_removed, after = prune_cache_dir(cache_dir, max_mb=limit)
        rep.cache_mb_after = round(after, 1)
    rep.seconds = round(time.perf_counter() - t0, 2)
    data = asdict(rep)
    SettingsRepository(conn).set_value(KEY, json.dumps(data, ensure_ascii=False))
    log.info("Porządki w bazie: %s (%.1f s)", rep.summary(), rep.seconds)
    return data


def last_report(conn: sqlite3.Connection) -> dict | None:
    import json

    from ..storage.repositories import SettingsRepository

    raw = SettingsRepository(conn).get_value(KEY)
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


def maintenance_due(conn: sqlite3.Connection, now_local: datetime, nightly_hour: int) -> bool:
    """Raz na dobę, w nocy (od godziny pełnego pobrania do 6:00), a gdy komputer w nocy jest wyłączony —
    po 3 dobach o dowolnej porze."""
    last = last_report(conn)
    when = datetime.fromisoformat(last["at"]) if last and last.get("at") else None
    if when is None:
        return True
    age = now_local - when.astimezone(now_local.tzinfo)
    if age >= timedelta(days=3):
        return True
    in_window = nightly_hour <= now_local.hour < max(nightly_hour + 1, 6)
    return in_window and age >= timedelta(hours=20)


def run_in_background(db_path, settings, cache_dir: Path | None = None, force_vacuum: bool = False) -> dict:
    from ..storage.db import connect

    conn = connect(db_path)
    try:
        return nightly_maintenance(conn, settings, cache_dir=cache_dir, force_vacuum=force_vacuum)
    finally:
        conn.close()
