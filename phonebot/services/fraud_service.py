"""Ryzyko oszustwa w wycenie: kontekst z bazy (opisy, skróty zdjęć, sprzedający) i limity werdyktu."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from ..core.fraud import FraudAssessment, FraudContext, SellerStats, desc_hash
from ..core.models import Valuation, Verdict
from ..core.negotiation import color_for
from ..core.settings import Settings


def owner_key_from(source: str, params: dict, city: str | None) -> str:
    who = params.get("seller") or params.get("seller_id")
    return f"{source}:{str(who).lower()}" if who else f"{source}:@{(city or '?').lower()}"


def build_context(conn: sqlite3.Connection, settings: Settings) -> FraudContext:
    ctx = FraudContext()
    owners: dict[tuple[str, str], str] = {}
    for r in conn.execute("SELECT source, source_id, description, city, params FROM offers WHERE is_active = 1"):
        try:
            params = json.loads(r["params"] or "{}")
        except json.JSONDecodeError:
            params = {}
        owner = owner_key_from(r["source"], params, r["city"])
        owners[(r["source"], r["source_id"])] = owner
        h = desc_hash(r["description"])
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
