"""Ryzyko oszustwa w wycenie: kontekst z bazy (opisy, skróty zdjęć, sprzedający) i limity werdyktu."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from functools import lru_cache

from ..core.fraud import SIGNALS, FraudAssessment, FraudContext, SellerStats, Signal, desc_hash
from ..core.models import Valuation, Verdict
from ..core.negotiation import color_for
from ..core.settings import Settings


def owner_key_from(source: str, params: dict, city: str | None) -> str:
    who = params.get("seller") or params.get("seller_id")
    return f"{source}:{str(who).lower()}" if who else f"{source}:@{(city or '?').lower()}"


_CACHE: dict[str, tuple[tuple, FraudContext]] = {}


@lru_cache(maxsize=8192)
def _owner_from_row(source: str, params_text: str | None, city: str | None) -> str:
    """Sprzedający z wiersza bazy — zapamiętany (kontekst buduje się po każdym odświeżeniu z tych samych ofert)."""
    try:
        params = json.loads(params_text or "{}")
    except json.JSONDecodeError:
        params = {}
    return owner_key_from(source, params, city)


def _data_version(conn: sqlite3.Connection, settings: Settings) -> tuple:
    """Tani „odcisk” danych, od których zależy kontekst — gdy się nie zmienił, kontekst jest brany z pamięci."""
    offers = conn.execute("SELECT COUNT(*), MAX(last_seen), MAX(id), SUM(is_active) FROM offers").fetchone()
    photos = conn.execute("SELECT COUNT(*), MAX(computed_at) FROM photo_hashes").fetchone()
    sellers = conn.execute("SELECT COUNT(*), MAX(checked_at) FROM sellers").fetchone()
    return (*offers, *photos, *sellers, settings.fraud.expensive_price)


def build_context(conn: sqlite3.Connection, settings: Settings) -> FraudContext:
    """Kontekst z bazy; między odświeżeniami (te same dane) — z pamięci, bez ponownego liczenia."""
    db = conn.execute("PRAGMA database_list").fetchone()[2] or f":memory:{id(conn)}"
    version = _data_version(conn, settings)
    cached = _CACHE.get(db)
    if cached is not None and cached[0] == version:
        return cached[1]
    ctx = _build(conn, settings)
    _CACHE[db] = (version, ctx)
    return ctx


def _build(conn: sqlite3.Connection, settings: Settings) -> FraudContext:
    ctx = FraudContext(memoize=True)
    owners: dict[tuple[str, str], str] = {}
    for r in conn.execute("SELECT source, source_id, description, city, params FROM offers WHERE is_active = 1"):
        owner = _owner_from_row(r["source"], r["params"], r["city"])
        owners[(r["source"], r["source_id"])] = owner
        h = desc_hash(r["description"])
        ctx.offer_desc[(r["source"], r["source_id"])] = h
        if h:
            ctx.descriptions.setdefault(h, set()).add(owner)
    for r in conn.execute("SELECT source, source_id, dhash, stock FROM photo_hashes WHERE dhash IS NOT NULL"):
        key = (r["source"], r["source_id"])
        if key in owners:
            ctx.photos[key] = (int(r["dhash"], 16), bool(r["stock"]))
            ctx.photo_owner[key] = owners[key]
    for r in conn.execute("SELECT source, seller_id, reviews, positive_pct, negative, created_at FROM sellers "
                          "WHERE reviews IS NOT NULL OR created_at IS NOT NULL"):
        ctx.sellers[(r["source"], r["seller_id"])] = SellerStats(
            datetime.fromisoformat(r["created_at"]) if r["created_at"] else None, r["reviews"], r["positive_pct"],
            r["negative"])
    for r in conn.execute("SELECT source, seller_id, COUNT(*) AS n FROM offers WHERE is_active = 1 AND seller_id "
                          "IS NOT NULL AND price >= ? GROUP BY source, seller_id", (settings.fraud.expensive_price,)):
        ctx.expensive_by_seller[(r["source"], r["seller_id"])] = int(r["n"])
    return ctx


def apply_risk(val: Valuation, risk: FraudAssessment, settings: Settings) -> None:
    """Średnie ryzyko → najwyżej DO WERYFIKACJI; wysokie → ODPUŚĆ z etykietą „MOŻLIWE OSZUSTWO”."""
    val.risk = risk
    if risk.level == "low":
        return
    why = "; ".join(risk.reasons()[:3])
    if risk.level == "high":
        val.verdict = Verdict.SKIP
        val.reasons.insert(0, f"MOŻLIWE OSZUSTWO (ryzyko {risk.score} pkt): {why}.")
    else:
        if val.verdict.rank > Verdict.VERIFY.rank:
            val.verdict = Verdict.VERIFY
        val.reasons.insert(0, f"Ryzyko oszustwa średnie ({risk.score} pkt): {why} — najwyżej DO WERYFIKACJI.")
    val.score = max(0, val.score - risk.score // 2)
    val.color = color_for(val.score, settings)


PHOTO_SCAM_LABEL = "MOŻLIWE OSZUSTWO: sprzedaż zdjęcia zamiast telefonu"


def apply_photo_scam(val: Valuation, result, settings: Settings) -> None:
    """Sprzedaż zdjęcia zamiast telefonu (``core.photo_scam``): pewne → ODPUŚĆ z etykietą „MOŻLIWE OSZUSTWO”
    i wysokie ryzyko (poza „Wybrane”, Telegramem i listą — patrz ``Evaluator.evaluate_visible``);
    słabe → najwyżej DO WERYFIKACJI z wyjaśnieniem."""
    level = result.level
    if level == "none":
        return
    val.photo_scam = level
    if level == "certain":
        val.verdict = Verdict.SKIP
        val.reasons.insert(0, f"{PHOTO_SCAM_LABEL} — {'; '.join(result.strong[:2])}.")
        risk = val.risk if isinstance(val.risk, FraudAssessment) else FraudAssessment()
        points = settings.fraud.weight("photo_sale")
        risk.signals.insert(0, Signal("photo_sale", SIGNALS["photo_sale"][0], points, "; ".join(result.strong[:2])))
        risk.score = max(risk.score + points, settings.fraud.high_threshold)
        risk.level = "high"
        val.risk = risk
        val.score = 0
    else:
        if val.verdict.rank > Verdict.VERIFY.rank:
            val.verdict = Verdict.VERIFY
        val.reasons.insert(0, f"Możliwa sprzedaż samego zdjęcia zamiast telefonu ({'; '.join(result.weak[:2])}) "
                              "— zapytaj sprzedającego, czy sprzedaje telefon; najwyżej DO WERYFIKACJI.")
    val.color = color_for(val.score, settings)
