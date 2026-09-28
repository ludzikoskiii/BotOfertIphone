"""Sprzedaż zdjęcia zamiast telefonu: propozycje czarnej listy (z potwierdzeniem) i raport z bazy."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from ..core.fraud import phone_numbers
from ..core.models import RawOffer
from ..storage.repositories import (
    BlacklistMatcher,
    BlacklistRepository,
    SettingsRepository,
    raw_from_json,
)

PROPOSED_KEY = "photo_scam_proposed"  # o których sprzedających już pytano (zgoda albo odmowa)
STAGE = "photo_scam"


def seller_identity(raw: RawOffer) -> str | None:
    """Kogo można zablokować: ID albo login z portalu, a gdy ich brak — numer telefonu z ogłoszenia."""
    who = raw.params.get("seller_id") or raw.params.get("seller")
    if who:
        return f"{raw.source}:{str(who).lower()}"
    phones = phone_numbers(f"{raw.title}\n{raw.description or ''}")
    return f"tel:{phones[0]}" if phones else None


def _proposed(conn: sqlite3.Connection) -> set[str]:
    try:
        return set(json.loads(SettingsRepository(conn).get_value(PROPOSED_KEY) or "[]"))
    except (TypeError, ValueError):
        return set()


@dataclass
class Candidate:
    raw: RawOffer
    identity: str
    reason: str


def blacklist_candidates(conn: sqlite3.Connection, limit: int = 5) -> list[Candidate]:
    """Sprzedający z ogłoszeniami „zdjęcie zamiast telefonu”, jeszcze niezablokowani i nieproponowani."""
    matcher = BlacklistMatcher(BlacklistRepository(conn).list())
    asked = _proposed(conn)
    out: dict[str, Candidate] = {}
    for row in conn.execute("SELECT raw_json, reason FROM rejected_offers WHERE stage = ? ORDER BY rejected_at DESC",
                            (STAGE,)):
        raw = raw_from_json(row["raw_json"])
        who = seller_identity(raw)
        if not who or who in asked or who in out or (matcher and matcher.match(raw)):
            continue
        out[who] = Candidate(raw, who, row["reason"])
        if len(out) >= limit:
            break
    return list(out.values())


def mark_proposed(conn: sqlite3.Connection, identities: list[str]) -> None:
    asked = _proposed(conn) | set(identities)
    SettingsRepository(conn).set_value(PROPOSED_KEY, json.dumps(sorted(asked)))


def block(conn: sqlite3.Connection, candidate: Candidate) -> None:
    BlacklistRepository(conn).add(candidate.raw, f"sprzedaż zdjęcia zamiast telefonu: {candidate.raw.title[:60]}")


def report(conn: sqlite3.Connection, examples: int = 8) -> dict:
    """Ile ofert oznaczono (odrzucone pewne + w wynikach do weryfikacji) i przykłady — do raportu po aktualizacji."""
    from ..services.evaluator import Evaluator
    from ..storage.repositories import OfferRepository

    rejected = [(raw_from_json(r["raw_json"]), r["reason"]) for r in conn.execute(
        "SELECT raw_json, reason FROM rejected_offers WHERE stage = ? ORDER BY rejected_at DESC", (STAGE,))]
    from ..storage.repositories import SettingsRepository as _S

    settings = _S(conn).load()
    ev = Evaluator(conn, settings)
    weak = []
    for offer in OfferRepository(conn).list():
        val = ev.evaluate(offer)
        if val.photo_scam == "weak":
            weak.append((offer.raw, val.reasons[0]))
    return {"rejected": len(rejected), "verify": len(weak),
            "rejected_examples": [(r.source, r.title, r.price, why) for r, why in rejected[:examples]],
            "verify_examples": [(r.source, r.title, r.price, why) for r, why in weak[:examples]]}


def format_report(data: dict) -> str:
    lines = ["Sprzedaż zdjęcia iPhone'a zamiast telefonu — oferty w Twojej bazie", "",
             f"Odrzucone (pewne wykrycie, widok „Odrzucone”): {data['rejected']}",
             f"Do weryfikacji (jeden słaby sygnał, zostają na liście): {data['verify']}", ""]
    for label, key in (("Przykłady odrzuconych", "rejected_examples"), ("Przykłady do weryfikacji", "verify_examples")):
        if data[key]:
            lines.append(label + ":")
            lines += [f"  • [{src}] {title} — {price:.0f} zł\n      {why}" for src, title, price, why in data[key]]
            lines.append("")
    lines.append("Niesłuszne odrzucenie? Okno główne → „Odrzucone” → „To jest telefon”.")
    return "\n".join(lines)


def report_cli(conn: sqlite3.Connection) -> str:
    """Nowe reguły na całej bazie (jak przy starcie programu) + raport."""
    from .offer_guard import OfferGuard

    settings = SettingsRepository(conn).load()
    OfferGuard(conn, settings).refilter_stored()
    return format_report(report(conn))
