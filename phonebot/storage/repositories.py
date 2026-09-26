"""Dostęp do danych: oferty, historia cen, części, ustawienia, przebiegi pobrań."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ..core.models import (
    AiLayers,
    Condition,
    Defect,
    MarketObservation,
    Offer,
    OfferStatus,
    ParsedInfo,
    RawOffer,
    RedFlag,
    merge_ai_findings,
)
from ..core.parts import PartPrice, default_parts
from ..core.settings import Settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _bool(value: int | None) -> bool | None:
    return None if value is None else bool(value)


def dedup_key(parsed: ParsedInfo, raw: RawOffer) -> str | None:
    """Klucz grupujący tę samą sztukę wystawioną na kilku portalach."""
    if not parsed.model:
        return None
    city = (raw.city or "").strip().lower()
    return f"{parsed.model}|{parsed.storage_gb or ''}|{round(raw.price, -1):.0f}|{city}"


@dataclass
class UpsertResult:
    offer_id: int
    is_new: bool
    price_changed: bool
    old_price: float | None


class OfferRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def upsert(self, raw: RawOffer, parsed: ParsedInfo, seen_at: datetime | None = None) -> UpsertResult:
        seen = _iso(seen_at or utcnow())
        existing = self.conn.execute(
            "SELECT id, price, description FROM offers WHERE source = ? AND source_id = ?",
            (raw.source, raw.source_id),
        ).fetchone()
        fields = {
            "url": raw.url, "title": raw.title, "description": raw.description, "price": raw.price,
            "currency": raw.currency, "city": raw.city, "region": raw.region, "lat": raw.lat, "lon": raw.lon,
            "photos": json.dumps(raw.photos), "params": json.dumps(raw.params, ensure_ascii=False),
            "shipping": None if raw.shipping_available is None else int(raw.shipping_available),
            "negotiable_raw": None if raw.negotiable is None else int(raw.negotiable),
            "created_at": _iso(raw.created_at), "model": parsed.model, "storage_gb": parsed.storage_gb,
            "condition": parsed.condition.value, "defects": json.dumps([d.value for d in parsed.defects]),
            "flags": json.dumps([f.value for f in parsed.flags]), "battery_health": parsed.battery_health,
            "negotiable": None if parsed.negotiable is None else int(parsed.negotiable),
            "dedup_key": dedup_key(parsed, raw), "seller_id": raw.params.get("seller_id") or None,
        }
        if existing is None:
            cols = ["source", "source_id", *fields, "first_seen", "last_seen"]
            values = [raw.source, raw.source_id, *fields.values(), seen, seen]
            cur = self.conn.execute(
                f"INSERT INTO offers ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", values
            )
            offer_id = int(cur.lastrowid)
            self._add_price(offer_id, raw.price, seen)
            return UpsertResult(offer_id, True, False, None)

        offer_id, old_price = int(existing["id"]), float(existing["price"])
        assignments = ", ".join(f"{k} = ?" for k in fields)
        self.conn.execute(
            f"UPDATE offers SET {assignments}, last_seen = ?, is_active = 1 WHERE id = ?",
            [*fields.values(), seen, offer_id],
        )
        if existing["description"] != raw.description:  # opis zmieniony → analiza AI od nowa
            self.conn.execute(
                "UPDATE offers SET ai_defects = NULL, ai_flags = NULL, ai_note = NULL, ai_checked_at = NULL "
                "WHERE id = ?", (offer_id,))
        changed = abs(old_price - raw.price) >= 0.01
        if changed:
            self._add_price(offer_id, raw.price, seen)
        return UpsertResult(offer_id, False, changed, old_price)

    def _add_price(self, offer_id: int, price: float, seen: str | None) -> None:
        self.conn.execute(
            "INSERT INTO price_history (offer_id, price, seen_at) VALUES (?, ?, ?)", (offer_id, price, seen)
        )

    def get(self, offer_id: int) -> Offer | None:
        row = self.conn.execute(f"{_OFFER_SELECT} WHERE o.id = ?", (offer_id,)).fetchone()
        return _row_to_offer(row) if row else None

    def by_seller(self, source: str, seller_id: str, *, active_only: bool = True) -> list[Offer]:
        sql = f"{_OFFER_SELECT} WHERE o.source = ? AND o.seller_id = ?" + (" AND o.is_active = 1" if active_only else "")
        return [_row_to_offer(r) for r in self.conn.execute(sql, (source, seller_id))]

    def list(self, *, include_hidden: bool = False, active_only: bool = True) -> list[Offer]:
        where = []
        if not include_hidden:
            where.append("o.status != 'hidden'")
        if active_only:
            where.append("o.is_active = 1")
        sql = _OFFER_SELECT + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY o.id"
        return [_row_to_offer(r) for r in self.conn.execute(sql)]

    def set_status(self, offer_id: int, status: OfferStatus) -> None:
        self.conn.execute("UPDATE offers SET status = ? WHERE id = ?", (status.value, offer_id))

    def mark_notified(self, offer_id: int, when: datetime | None = None) -> None:
        self.conn.execute("UPDATE offers SET notified_at = ? WHERE id = ?", (_iso(when or utcnow()), offer_id))

    def was_notified(self, offer_id: int) -> bool:
        row = self.conn.execute("SELECT notified_at FROM offers WHERE id = ?", (offer_id,)).fetchone()
        return bool(row and row["notified_at"])

    def deactivate_missing(self, source: str, older_than: datetime) -> int:
        """Oznacza jako nieaktywne oferty źródła, których nie widziano od ``older_than``."""
        cur = self.conn.execute(
            "UPDATE offers SET is_active = 0 WHERE source = ? AND is_active = 1 AND last_seen < ?",
            (source, _iso(older_than)),
        )
        return cur.rowcount

    def purge_inactive(self, older_than_days: int) -> int:
        """Usuwa dawno nieaktywne oferty (i ich historię cen), których nie potrzebuje już wycena rynkowa.

        Obserwowane oferty zostają. Bez tego baza rośnie bez końca przy każdym skanie.
        """
        cutoff = _iso(utcnow() - timedelta(days=older_than_days))
        cur = self.conn.execute(
            "DELETE FROM offers WHERE is_active = 0 AND last_seen < ? AND status != 'watched'", (cutoff,)
        )
        return cur.rowcount

    def price_history(self, offer_id: int) -> list[tuple[datetime, float]]:
        rows = self.conn.execute(
            "SELECT seen_at, price FROM price_history WHERE offer_id = ? ORDER BY seen_at", (offer_id,)
        )
        return [(_dt(r["seen_at"]), float(r["price"])) for r in rows]  # type: ignore[misc]

    def pending_ai(self, offer_ids: list[int], limit: int, min_description: int = 30) -> list[sqlite3.Row]:
        """Oferty z listy, które nie były jeszcze analizowane przez AI (z opisem, rozpoznanym modelem)."""
        if not offer_ids or limit <= 0:
            return []
        marks = ",".join("?" * len(offer_ids))
        return list(self.conn.execute(
            f"SELECT id, title, description FROM offers WHERE id IN ({marks}) AND ai_checked_at IS NULL "
            f"AND model IS NOT NULL AND length(description) >= ? ORDER BY id DESC LIMIT ?",
            [*offer_ids, min_description, limit],
        ))

    def save_ai(self, offer_id: int, defects: list[Defect], flags: list[RedFlag], note: str) -> None:
        self.conn.execute(
            "UPDATE offers SET ai_defects = ?, ai_flags = ?, ai_note = ?, ai_checked_at = ? WHERE id = ?",
            (json.dumps([d.value for d in defects]), json.dumps([f.value for f in flags]), note,
             _iso(utcnow()), offer_id),
        )

    def market_observations(self, model: str, window_days: int, now: datetime | None = None) -> list[MarketObservation]:
        """Ceny ofert danego modelu z okna czasowego; ta sama sztuka z kilku portali liczona raz."""
        since = _iso((now or utcnow()) - timedelta(days=window_days))
        rows = self.conn.execute(
            """
            SELECT MIN(id) AS id, price, storage_gb, condition FROM offers
            WHERE model = ? AND last_seen >= ?
              AND flags NOT LIKE '%price_unrealistic%' AND flags NOT LIKE '%serial_seller%'
            GROUP BY COALESCE(dedup_key, 'id:' || id)
            """,
            (model, since),
        )
        return [
            MarketObservation(float(r["price"]), r["storage_gb"], Condition(r["condition"]), int(r["id"]))
            for r in rows
        ]


# oferty razem z wynikami lokalnego AI (po ID ogłoszenia)
_OFFER_SELECT = """
    SELECT o.*, a.text_label AS ai_text_label, a.text_conf AS ai_text_conf, a.text_probs AS ai_text_probs,
           a.photo_label AS ai_photo_label, a.photo_conf AS ai_photo_conf, a.photo_probs AS ai_photo_probs,
           a.photo_at AS ai_photo_at, a.photo_error AS ai_photo_error
    FROM offers o LEFT JOIN ai_results a ON a.source = o.source AND a.source_id = o.source_id"""


def _row_to_layers(row: sqlite3.Row) -> AiLayers | None:
    if "ai_text_label" not in row.keys():
        return None
    if row["ai_text_label"] is None and row["ai_photo_label"] is None and row["ai_photo_error"] is None:
        return None
    return AiLayers(
        text_label=row["ai_text_label"], text_conf=row["ai_text_conf"],
        text_probs=json.loads(row["ai_text_probs"] or "{}"),
        photo_label=row["ai_photo_label"], photo_conf=row["ai_photo_conf"],
        photo_probs=json.loads(row["ai_photo_probs"] or "{}"), photo_at=_dt(row["ai_photo_at"]),
        photo_error=row["ai_photo_error"],
    )


def _row_to_offer(row: sqlite3.Row) -> Offer:
    raw = RawOffer(
        source=row["source"], source_id=row["source_id"], url=row["url"], title=row["title"],
        price=float(row["price"]), description=row["description"], currency=row["currency"],
        city=row["city"], region=row["region"], lat=row["lat"], lon=row["lon"],
        photos=json.loads(row["photos"]), created_at=_dt(row["created_at"]),
        shipping_available=_bool(row["shipping"]), negotiable=_bool(row["negotiable_raw"]),
        params=json.loads(row["params"]),
    )
    parsed = ParsedInfo(
        model=row["model"], storage_gb=row["storage_gb"], condition=Condition(row["condition"]),
        defects=[Defect(d) for d in json.loads(row["defects"])],
        flags=[RedFlag(f) for f in json.loads(row["flags"]) if f in RedFlag._value2member_map_],
        battery_health=row["battery_health"], negotiable=_bool(row["negotiable"]),
    )
    offer = Offer(
        raw=raw, parsed=parsed, id=int(row["id"]), status=OfferStatus(row["status"]),
        first_seen=_dt(row["first_seen"]), last_seen=_dt(row["last_seen"]), dedup_key=row["dedup_key"],
        layers=_row_to_layers(row),
    )
    if row["ai_checked_at"]:
        ai_defects = [Defect(d) for d in json.loads(row["ai_defects"] or "[]") if d in Defect._value2member_map_]
        ai_flags = [RedFlag(f) for f in json.loads(row["ai_flags"] or "[]") if f in RedFlag._value2member_map_]
        offer.ai_defects, offer.ai_flags = merge_ai_findings(parsed, ai_defects, ai_flags)
        offer.ai_note = row["ai_note"] or ""
    return offer


class PartsRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def seed_defaults_if_empty(self) -> int:
        if self.conn.execute("SELECT COUNT(*) FROM parts_prices").fetchone()[0]:
            return 0
        rows = default_parts()
        self.conn.executemany(
            "INSERT INTO parts_prices (model, part, price, note) VALUES (?, ?, ?, ?)",
            [(r.model, r.part.value, r.price, r.note) for r in rows],
        )
        return len(rows)

    def all(self) -> list[PartPrice]:
        rows = self.conn.execute("SELECT * FROM parts_prices ORDER BY model, part")
        return [
            PartPrice(r["model"], Defect(r["part"]), float(r["price"]), r["note"], int(r["id"]))
            for r in rows if r["part"] in Defect._value2member_map_
        ]

    def upsert(self, row: PartPrice) -> None:
        self.conn.execute(
            """INSERT INTO parts_prices (model, part, price, note) VALUES (?, ?, ?, ?)
               ON CONFLICT (model, part) DO UPDATE SET price = excluded.price, note = excluded.note""",
            (row.model, row.part.value, row.price, row.note),
        )

    def delete(self, model: str, part: Defect) -> None:
        self.conn.execute("DELETE FROM parts_prices WHERE model = ? AND part = ?", (model, part.value))


class SettingsRepository:
    KEY = "app"

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def load(self) -> Settings:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (self.KEY,)).fetchone()
        return Settings.from_json(row["value"] if row else None)

    def save(self, settings: Settings) -> None:
        self.set_value(self.KEY, settings.to_json())

    def get_value(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_value(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


class FetchRunRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def start(self, source: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO fetch_runs (source, started_at) VALUES (?, ?)", (source, _iso(utcnow()))
        )
        return int(cur.lastrowid)

    def finish(self, run_id: int, *, found: int, new: int, error: str | None = None,
               status: str | None = None) -> None:
        self.conn.execute(
            "UPDATE fetch_runs SET finished_at = ?, status = ?, offers_found = ?, new_offers = ?, error = ? "
            "WHERE id = ?",
            (_iso(utcnow()), status or ("error" if error else "ok"), found, new, error, run_id),
        )

    def latest_by_source(self) -> dict[str, sqlite3.Row]:
        """Ostatni zakończony przebieg każdego źródła (do statusu w GUI)."""
        rows = self.conn.execute(
            "SELECT * FROM fetch_runs WHERE id IN (SELECT MAX(id) FROM fetch_runs "
            "WHERE finished_at IS NOT NULL GROUP BY source)")
        return {r["source"]: r for r in rows}

    def last_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM fetch_runs ORDER BY id DESC LIMIT ?", (limit,)))


def raw_to_json(raw: RawOffer) -> str:
    data = {
        "source": raw.source, "source_id": raw.source_id, "url": raw.url, "title": raw.title, "price": raw.price,
        "description": raw.description, "currency": raw.currency, "city": raw.city, "region": raw.region,
        "lat": raw.lat, "lon": raw.lon, "photos": raw.photos, "created_at": _iso(raw.created_at),
        "shipping_available": raw.shipping_available, "negotiable": raw.negotiable, "params": raw.params,
    }
    return json.dumps(data, ensure_ascii=False)


def raw_from_json(text: str) -> RawOffer:
    d = json.loads(text)
    d["created_at"] = _dt(d.get("created_at"))
    return RawOffer(**d)


@dataclass
class RejectedOffer:
    id: int
    source: str
    source_id: str
    url: str
    title: str
    price: float
    stage: str
    reason: str
    keyword: str | None
    rejected_at: datetime | None
    raw: RawOffer


class RejectedRepository:
    """Ogłoszenia odrzucone przez filtr (do przeglądu) i biała lista przywróconych."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, raw: RawOffer, stage: str, reason: str, keyword: str | None = None) -> None:
        self.conn.execute(
            """INSERT INTO rejected_offers (source, source_id, url, title, price, stage, reason, keyword, raw_json,
                                            rejected_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (source, source_id) DO UPDATE SET
                 url = excluded.url, title = excluded.title, price = excluded.price, stage = excluded.stage,
                 reason = excluded.reason, keyword = excluded.keyword, raw_json = excluded.raw_json,
                 rejected_at = excluded.rejected_at""",
            (raw.source, raw.source_id, raw.url, raw.title, raw.price, stage, reason, keyword, raw_to_json(raw),
             _iso(utcnow())),
        )

    def list(self, limit: int = 2000) -> list[RejectedOffer]:
        rows = self.conn.execute("SELECT * FROM rejected_offers ORDER BY rejected_at DESC, id DESC LIMIT ?", (limit,))
        return [RejectedOffer(r["id"], r["source"], r["source_id"], r["url"], r["title"], float(r["price"]),
                              r["stage"], r["reason"], r["keyword"], _dt(r["rejected_at"]), raw_from_json(r["raw_json"]))
                for r in rows]

    def count(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM rejected_offers").fetchone()[0])

    def remove(self, source: str, source_id: str) -> None:
        self.conn.execute("DELETE FROM rejected_offers WHERE source = ? AND source_id = ?", (source, source_id))

    def purge_older_than(self, days: int) -> int:
        cur = self.conn.execute("DELETE FROM rejected_offers WHERE rejected_at < ?",
                                (_iso(utcnow() - timedelta(days=days)),))
        return cur.rowcount

    # --- biała lista („To jest telefon”) ---

    def whitelist(self) -> set[tuple[str, str]]:
        return {(r["source"], r["source_id"]) for r in self.conn.execute("SELECT source, source_id FROM filter_whitelist")}

    def false_positives_by_keyword(self) -> list[tuple[str, int]]:
        """Które słowa najczęściej niesłusznie odrzucały telefony (podpowiedź do poprawy list)."""
        rows = self.conn.execute(
            "SELECT COALESCE(keyword, stage) AS k, COUNT(*) AS n FROM filter_whitelist GROUP BY k ORDER BY n DESC")
        return [(r["k"], int(r["n"])) for r in rows]

    def reject_stored(self, offer: Offer, stage: str, reason: str, keyword: str | None = None) -> None:
        """Przenosi zapisaną ofertę do odrzuconych (np. po zaostrzeniu filtra albo wykryciu sprzedawcy seryjnego)."""
        self.add(offer.raw, stage, reason, keyword)
        if offer.id is not None:
            self.conn.execute("DELETE FROM offers WHERE id = ?", (offer.id,))

    def restore(self, rejected_id: int) -> int | None:
        """„To jest telefon”: dodaje do białej listy i przenosi ofertę do wyników. Zwraca id oferty."""
        from ..core.normalizer import parse_offer

        row = self.conn.execute("SELECT * FROM rejected_offers WHERE id = ?", (rejected_id,)).fetchone()
        if row is None:
            return None
        raw = raw_from_json(row["raw_json"])
        self.conn.execute(
            "INSERT OR REPLACE INTO filter_whitelist (source, source_id, title, stage, keyword, restored_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (raw.source, raw.source_id, raw.title, row["stage"], row["keyword"], _iso(utcnow())),
        )
        offer_id = OfferRepository(self.conn).upsert(raw, parse_offer(raw)).offer_id
        self.remove(raw.source, raw.source_id)
        LabelRepository(self.conn).add(raw, "phone", "user")  # Twoja poprawka uczy klasyfikator
        return offer_id


@dataclass
class SellerInfo:
    source: str
    seller_id: str
    login: str | None = None
    country_code: str | None = None  # np. "PL", "CZ"; None = jeszcze nie sprawdzono
    business: bool | None = None
    checked_at: datetime | None = None
    serial: bool = False
    serial_reason: str | None = None


class SellerRepository:
    """Sprzedawcy: kraj z profilu (pamięć podręczna) i oznaczenie „sprzedawca seryjny”."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def get_many(self, source: str, seller_ids: list[str] | set[str]) -> dict[str, SellerInfo]:
        ids = [i for i in dict.fromkeys(seller_ids) if i]
        out: dict[str, SellerInfo] = {}
        for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
            rows = self.conn.execute(
                f"SELECT * FROM sellers WHERE source = ? AND seller_id IN ({', '.join('?' * len(chunk))})",
                [source, *chunk])
            for r in rows:
                out[r["seller_id"]] = _row_to_seller(r)
        return out

    def needs_country(self, source: str, seller_ids: list[str], max_age_days: int = 30) -> list[str]:
        """Sprzedawcy bez sprawdzonego kraju (albo sprawdzeni dawno) — w kolejności podanej listy."""
        known = self.get_many(source, seller_ids)
        cutoff = utcnow() - timedelta(days=max_age_days)
        # sprawdzony profil pamiętamy 30 dni — także bez kraju (nie pytamy portalu co odświeżenie)
        return [i for i in dict.fromkeys(seller_ids) if i and (
            i not in known or known[i].checked_at is None or known[i].checked_at < cutoff)]

    def save_country(self, source: str, seller_id: str, country_code: str | None, *, login: str | None = None,
                     business: bool | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO sellers (source, seller_id, login, country_code, business, checked_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, seller_id) DO UPDATE SET
                login = COALESCE(excluded.login, login), country_code = excluded.country_code,
                business = COALESCE(excluded.business, business), checked_at = excluded.checked_at
            """,
            (source, seller_id, login, (country_code or "").upper() or None,
             None if business is None else int(business), _iso(utcnow())))

    def mark_serial(self, source: str, seller_id: str, reason: str, login: str | None = None) -> None:
        self.conn.execute(
            """
            INSERT INTO sellers (source, seller_id, login, serial, serial_reason, serial_at) VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT (source, seller_id) DO UPDATE SET
                serial = 1, serial_reason = excluded.serial_reason, serial_at = excluded.serial_at,
                login = COALESCE(excluded.login, login)
            """, (source, seller_id, login, reason, _iso(utcnow())))

    def unmark_serial(self, source: str, seller_id: str) -> None:
        self.conn.execute("UPDATE sellers SET serial = 0, serial_reason = NULL WHERE source = ? AND seller_id = ?",
                          (source, seller_id))

    def serial_sellers(self, source: str | None = None) -> dict[tuple[str, str], SellerInfo]:
        sql = "SELECT * FROM sellers WHERE serial = 1" + (" AND source = ?" if source else "")
        rows = self.conn.execute(sql, (source,) if source else ())
        return {(r["source"], r["seller_id"]): _row_to_seller(r) for r in rows}


def _row_to_seller(r: sqlite3.Row) -> SellerInfo:
    return SellerInfo(r["source"], r["seller_id"], r["login"], r["country_code"], _bool(r["business"]),
                      _dt(r["checked_at"]), bool(r["serial"]), r["serial_reason"])


class LabelRepository:
    """Twoje oznaczenia ofert — dane do nauki klasyfikatora tytułów."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def add(self, raw: RawOffer, label: str, origin: str = "user") -> None:
        """Zapisuje oznaczenie. Ukrycie oferty (słaba wskazówka) nie nadpisuje Twojego wyraźnego oznaczenia."""
        self.conn.execute(
            """
            INSERT INTO ml_labels (source, source_id, title, label, origin, created_at) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, source_id) DO UPDATE SET
                title = excluded.title, label = excluded.label, origin = excluded.origin,
                created_at = excluded.created_at
            WHERE ml_labels.origin != 'user' OR excluded.origin = 'user'
            """, (raw.source, raw.source_id, raw.title, label, origin, _iso(utcnow())))

    def remove(self, source: str, source_id: str, origin: str | None = None) -> None:
        sql = "DELETE FROM ml_labels WHERE source = ? AND source_id = ?" + (" AND origin = ?" if origin else "")
        self.conn.execute(sql, (source, source_id, origin) if origin else (source, source_id))

    def all(self) -> list[tuple[str, str, str]]:
        """(tytuł, etykieta, pochodzenie)."""
        return [(r["title"], r["label"], r["origin"])
                for r in self.conn.execute("SELECT title, label, origin FROM ml_labels ORDER BY id")]

    def count(self, origin: str | None = None) -> int:
        sql = "SELECT COUNT(*) FROM ml_labels" + (" WHERE origin = ?" if origin else "")
        return int(self.conn.execute(sql, (origin,) if origin else ()).fetchone()[0])


class AiRepository:
    """Wyniki warstw lokalnego AI po ID ogłoszenia (żeby nie analizować tej samej oferty dwa razy)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save_text(self, source: str, source_id: str, probs: dict[str, float], model: str) -> None:
        label = max(probs, key=probs.get) if probs else None
        self.conn.execute(
            """
            INSERT INTO ai_results (source, source_id, text_label, text_conf, text_probs, text_model)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, source_id) DO UPDATE SET
                text_label = excluded.text_label, text_conf = excluded.text_conf,
                text_probs = excluded.text_probs, text_model = excluded.text_model
            """, (source, source_id, label, probs.get(label) if label else None,
                  json.dumps({k: round(v, 4) for k, v in probs.items()}), model))

    def save_photo(self, source: str, source_id: str, url: str, probs: dict[str, float] | None, model: str,
                   error: str | None = None) -> None:
        label = max(probs, key=probs.get) if probs else None
        self.conn.execute(
            """
            INSERT INTO ai_results (source, source_id, photo_url, photo_label, photo_conf, photo_probs, photo_model,
                                    photo_at, photo_error)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source, source_id) DO UPDATE SET
                photo_url = excluded.photo_url, photo_label = excluded.photo_label, photo_conf = excluded.photo_conf,
                photo_probs = excluded.photo_probs, photo_model = excluded.photo_model, photo_at = excluded.photo_at,
                photo_error = excluded.photo_error
            """, (source, source_id, url, label, probs.get(label) if label else None,
                  json.dumps({k: round(v, 4) for k, v in (probs or {}).items()}), model, _iso(utcnow()), error))

    def text_model_of(self, source: str, source_id: str) -> str | None:
        row = self.conn.execute("SELECT text_model FROM ai_results WHERE source = ? AND source_id = ?",
                                (source, source_id)).fetchone()
        return row["text_model"] if row else None

    def missing_text(self, model: str) -> list[tuple[str, str, str]]:
        """Aktywne oferty bez wyniku bieżącego modelu tytułów: (source, source_id, tytuł)."""
        rows = self.conn.execute(
            """
            SELECT o.source, o.source_id, o.title FROM offers o
            LEFT JOIN ai_results a ON a.source = o.source AND a.source_id = o.source_id
            WHERE o.is_active = 1 AND (a.text_model IS NULL OR a.text_model != ?)
            """, (model,))
        return [(r["source"], r["source_id"], r["title"]) for r in rows]
