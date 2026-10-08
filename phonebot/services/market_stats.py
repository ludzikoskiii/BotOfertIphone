"""Statystyki rynku liczone w tle i zapisywane w bazie (zakładka „Rynek”, trend w szczegółach oferty).

Przeliczenie (co ``recompute_hours`` godzin, w wątku roboczym):
1. uzupełnia werdykt „z chwili pojawienia się” ofertom, które go nie mają (starsze bazy — wycena bieżąca),
2. liczy dzienne ceny i podaż za dni jeszcze niepoliczone oraz ostatnie ``refresh_days`` dni (starsze dni są
   zapisane na stałe — zostają, nawet gdy dawne oferty znikną z bazy),
3. zapisuje trendy, czas aktywności ogłoszeń i najlepsze pory na zakupy.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from ..core.market_stats import (
    ALL_STORAGE,
    ActiveTime,
    BestTimes,
    DayStat,
    MarketStatsConfig,
    OfferPoint,
    Slot,
    Trend,
    active_time,
    best_times,
    daily_stats,
    trend,
)
from ..core.models import Condition
from ..core.settings import Settings
from ..storage.db import open_database
from ..storage.repositories import _dt, _iso, utcnow

log = logging.getLogger(__name__)


def _key(model: str, storage: int, cls: str) -> str:
    return f"{model}|{storage}|{cls}"


class MarketStatsRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ------------------------------------------------------------ odczyt ---

    def daily(self, model: str, storage: int, cls: str, since: date | None = None) -> list[DayStat]:
        sql = "SELECT * FROM market_daily WHERE model = ? AND storage_gb = ? AND cls = ?"
        params: list = [model, storage, cls]
        if since is not None:
            sql += " AND day >= ?"
            params.append(since.isoformat())
        rows = self.conn.execute(sql + " ORDER BY day", params)
        return [DayStat(r["model"], r["storage_gb"], r["cls"], date.fromisoformat(r["day"]), r["n"], r["p10"],
                        r["p25"], r["median"], r["p75"], r["p90"], r["new_count"]) for r in rows]

    def models(self) -> list[str]:
        """Modele w statystykach — najczęstsze najpierw."""
        rows = self.conn.execute("SELECT model, SUM(new_count) + SUM(n) AS c FROM market_daily "
                                 "WHERE storage_gb = 0 GROUP BY model ORDER BY c DESC, model")
        return [r["model"] for r in rows]

    def storages(self, model: str) -> list[int]:
        rows = self.conn.execute("SELECT DISTINCT storage_gb FROM market_daily WHERE model = ? AND storage_gb > 0 "
                                 "ORDER BY storage_gb", (model,))
        return [int(r[0]) for r in rows]

    def get(self, key: str):
        row = self.conn.execute("SELECT data FROM market_stats WHERE key = ?", (key,)).fetchone()
        return json.loads(row["data"]) if row else None

    def computed_at(self) -> datetime | None:
        row = self.conn.execute("SELECT computed_at FROM market_stats WHERE key = 'meta'").fetchone()
        return _dt(row["computed_at"]) if row else None

    def trends(self) -> dict[str, Trend]:
        return {k: Trend.from_dict(v) for k, v in (self.get("trends") or {}).items()}

    def trend_for(self, model: str | None, storage: int | None, cls: str,
                  trends: dict[str, Trend] | None = None) -> tuple[Trend, bool] | None:
        """Trend dla modelu i pamięci; gdy dla tej pamięci za mało danych — dla wszystkich pamięci
        (drugi element: True = trend wszystkich pamięci)."""
        if not model:
            return None
        trends = self.trends() if trends is None else trends
        exact = trends.get(_key(model, storage or ALL_STORAGE, cls))
        if storage and (exact is None or exact.direction == "unknown"):
            overall = trends.get(_key(model, ALL_STORAGE, cls))
            if overall is not None and overall.direction != "unknown":
                return overall, True
        return (exact, not storage) if exact is not None else None

    def active_time(self, model: str | None) -> ActiveTime | None:
        data = (self.get("active") or {}).get(model or "*")
        return ActiveTime(data["median_days"], data["count"]) if data else None

    def best_times(self, model: str | None = None) -> BestTimes | None:
        data = (self.get("best") or {}).get(model or "*")
        if not data:
            return None

        def slots(items):
            return [Slot(s["good"], s["total"], s.get("ratios", [])) for s in items]

        return BestTimes(slots(data["weekdays"]), slots(data["hours"]), data["good_total"], data["enough"])

    # ------------------------------------------------------------ zapis ---

    def put(self, key: str, data, now: datetime) -> None:
        self.conn.execute("INSERT INTO market_stats (key, computed_at, data) VALUES (?, ?, ?) ON CONFLICT (key) "
                          "DO UPDATE SET computed_at = excluded.computed_at, data = excluded.data",
                          (key, _iso(now), json.dumps(data, ensure_ascii=False)))


# --------------------------------------------------------------- dane ---

_FILTER = "AND flags NOT LIKE '%price_unrealistic%' AND flags NOT LIKE '%serial_seller%'"


def load_points(conn: sqlite3.Connection, since: datetime) -> list[OfferPoint]:
    """Oferty widziane od ``since`` (także archiwalne), jedna na sztukę (ta sama oferta z kilku portali — raz)."""
    rows = conn.execute(
        f"""SELECT o.* FROM offers o JOIN (
                SELECT MIN(id) AS id FROM offers WHERE model IS NOT NULL AND last_seen >= ? {_FILTER}
                GROUP BY COALESCE(dedup_key, 'id:' || id)) d ON d.id = o.id""", (_iso(since),)).fetchall()
    ids = [int(r["id"]) for r in rows]
    history: dict[int, list[tuple[datetime, float]]] = {}
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for h in conn.execute(f"SELECT offer_id, seen_at, price FROM price_history WHERE offer_id IN "
                              f"({','.join('?' * len(chunk))}) ORDER BY seen_at", chunk):
            when = _dt(h["seen_at"])
            if when is not None:
                history.setdefault(int(h["offer_id"]), []).append((when, float(h["price"])))
    out = []
    for r in rows:
        cond = r["condition"] if r["condition"] in Condition._value2member_map_ else "good"
        first, last = _dt(r["first_seen"]), _dt(r["last_seen"])
        if first is None or last is None:
            continue
        out.append(OfferPoint(int(r["id"]), r["model"], r["storage_gb"], Condition(cond).market_class, first, last,
                              float(r["price"]), history.get(int(r["id"]), []), _dt(r["created_at"]),
                              bool(r["is_active"]), r["first_verdict"]))
    return out


def backfill_verdicts(conn: sqlite3.Connection, settings: Settings, since: datetime, limit: int) -> int:
    """Oferty bez zapisanego werdyktu „z chwili pojawienia się” — wycena bieżąca (przybliżenie dla starszych baz)."""
    from ..storage.repositories import OfferRepository
    from .evaluator import Evaluator

    ids = [int(r[0]) for r in conn.execute(
        "SELECT id FROM offers WHERE first_verdict IS NULL AND model IS NOT NULL AND first_seen >= ? "
        "ORDER BY first_seen DESC LIMIT ?", (_iso(since), limit))]
    if not ids:
        return 0
    repo, evaluator = OfferRepository(conn), Evaluator(conn, settings)
    done = 0
    for oid in ids:
        offer = repo.get(oid)
        if offer is None:
            continue
        try:
            repo.set_first_verdict(oid, evaluator.evaluate(offer).verdict.value)
            done += 1
        except Exception as e:  # jedna oferta nie może zatrzymać statystyk
            log.debug("Werdykt oferty %s: %s", oid, e)
    return done


def stats_due(conn: sqlite3.Connection, cfg: MarketStatsConfig, now: datetime | None = None) -> bool:
    if not cfg.enabled:
        return False
    last = MarketStatsRepository(conn).computed_at()
    return last is None or (now or utcnow()) - last >= timedelta(hours=max(1, cfg.recompute_hours))


def compute(conn: sqlite3.Connection, settings: Settings, now: datetime | None = None) -> dict:
    """Pełne przeliczenie statystyk (w wątku roboczym). Zwraca podsumowanie."""
    cfg = settings.market_stats
    now = now or utcnow()
    today = now.astimezone().date()
    since_dt = now - timedelta(days=cfg.window_days + 1)
    backfilled = backfill_verdicts(conn, settings, since_dt, cfg.backfill_limit)
    points = load_points(conn, since_dt)

    # dni do policzenia: niepoliczone + ostatnie ``refresh_days`` (starsze zostają zapisane na stałe)
    window = [today - timedelta(days=i) for i in range(cfg.window_days, -1, -1)]
    done = {r[0] for r in conn.execute("SELECT day FROM market_days")}
    fresh_from = today - timedelta(days=cfg.refresh_days)
    todo = [d for d in window if d >= fresh_from or d.isoformat() not in done]
    stats = daily_stats(points, todo) if todo else []
    conn.execute("BEGIN IMMEDIATE")
    try:
        for i in range(0, len(todo), 200):
            chunk = [d.isoformat() for d in todo[i:i + 200]]
            conn.execute(f"DELETE FROM market_daily WHERE day IN ({','.join('?' * len(chunk))})", chunk)
        conn.executemany(
            "INSERT INTO market_daily (model, storage_gb, cls, day, n, p10, p25, median, p75, p90, new_count) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(s.model, s.storage_gb, s.cls, s.day.isoformat(), s.n, s.p10, s.p25, s.median, s.p75, s.p90,
              s.new_count) for s in stats])
        conn.executemany("INSERT INTO market_days (day, computed_at) VALUES (?, ?) ON CONFLICT (day) DO UPDATE SET "
                         "computed_at = excluded.computed_at", [(d.isoformat(), _iso(now)) for d in todo])
        conn.execute("DELETE FROM market_daily WHERE day < ?",
                     ((today - timedelta(days=max(cfg.window_days, 365))).isoformat(),))

        repo = MarketStatsRepository(conn)
        # trendy: każda kombinacja model × pamięć × stan — z cen nowych ogłoszeń
        samples: dict[str, list[tuple[date, float]]] = {}
        for p in points:
            price = p.prices[0][1] if p.prices else p.price
            keys = [_key(p.model, p.storage_gb or ALL_STORAGE, p.cls)]
            if p.storage_gb:
                keys.append(_key(p.model, ALL_STORAGE, p.cls))
            for k in keys:
                samples.setdefault(k, []).append((p.first_seen.astimezone().date(), price))
        trends = {k: trend(v, cfg, today).to_dict() for k, v in samples.items()}
        repo.put("trends", trends, now)

        # czas aktywności ogłoszeń: każdy model + wszystkie
        by_model: dict[str, list[OfferPoint]] = {}
        for p in points:
            by_model.setdefault(p.model, []).append(p)
        active = {"*": active_time(points, cfg).__dict__}
        active.update({m: active_time(ps, cfg).__dict__ for m, ps in by_model.items()})
        repo.put("active", active, now)

        # najlepsze pory: wszystkie modele + modele z wystarczającą liczbą okazji
        medians = {(r["model"], r["storage_gb"], r["cls"], date.fromisoformat(r["day"])): r["median"]
                   for r in conn.execute("SELECT model, storage_gb, cls, day, median FROM market_daily "
                                         "WHERE median IS NOT NULL AND n >= ?", (cfg.min_offers_point,))}
        recent = [p for p in points if p.first_seen >= since_dt]

        def pack(bt: BestTimes) -> dict:
            def slots(items):
                return [{"good": s.good, "total": s.total, "ratios": [round(x, 4) for x in s.ratios[-400:]]}
                        for s in items]
            return {"weekdays": slots(bt.weekdays), "hours": slots(bt.hours), "good_total": bt.good_total,
                    "enough": bt.enough}

        best = {"*": pack(best_times(recent, medians, cfg))}
        for m in by_model:
            bt = best_times([p for p in recent if p.model == m], medians, cfg)
            if bt.enough:
                best[m] = pack(bt)
        repo.put("best", best, now)
        summary = {"offers": len(points), "days": len(todo), "rows": len(stats), "backfilled": backfilled}
        repo.put("meta", summary, now)
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    return summary


def compute_in_background(db_path: str | Path, settings: Settings) -> dict:
    """Wątek roboczy: własne połączenie z bazą."""
    conn = open_database(db_path)
    try:
        return compute(conn, settings)
    finally:
        conn.close()
