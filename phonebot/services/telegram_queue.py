"""Powiadomienia Telegram: kolejka w bazie, bez duplikatów, z ponowieniami, ciszą nocną i limitem na godzinę.

Kiedy wiadomość trafia do kolejki (``enqueue_scan``, po każdym odświeżeniu):

* **nowa oferta**, która automatycznie trafiła do „Wybrane” i spełnia osobne (ostrzejsze) kryteria
  Telegrama — każda oferta najwyżej raz (unikalny wpis w ``telegram_outbox``);
* opcjonalnie **obniżka ceny** oferty z „Wybrane” — raz na każdą nową, niższą cenę;
* nigdy dla ofert sprzed pierwszego włączenia powiadomień (``telegram_since``) i nigdy przy pierwszym
  pobraniu do pustej bazy.

Wysyłka (``flush``) działa w tle — po skanie i co kilka minut: w ciszy nocnej wiadomości czekają do rana
(albo są pomijane — do wyboru), ponad limit na godzinę trafiają do jednego podsumowania, a po błędzie
sieci są ponawiane z rosnącą przerwą. Blokada chroni przed podwójną wysyłką z dwóch wątków.
"""
from __future__ import annotations

import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from html import escape

from ..core.catalog import format_storage
from ..core.messages import compose, opening_price, zl
from ..core.models import Offer, OfferStatus, Valuation, Verdict
from ..core.selection import auto_match, high_risk, is_picked
from ..core.settings import Settings
from ..sources import SOURCE_NAMES
from ..storage.repositories import OfferRepository, SettingsRepository
from .notifications import NotificationError, TelegramClient

log = logging.getLogger(__name__)

SINCE_KEY = "telegram_since"
QUIET_MODES = {"batch": "wyślij zebrane rano, po ciszy nocnej", "skip": "pomiń (nie wysyłaj wcale)"}
RETRY_MINUTES = (1, 5, 15, 30, 60, 120)
MAX_ATTEMPTS = 8
_FLUSH_LOCK = threading.Lock()
_ICON = {Verdict.BUY: "🟢", Verdict.NEGOTIATE: "🟡", Verdict.VERIFY: "⚪", Verdict.SKIP: "🔴"}


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


# ------------------------------------------------------------- treść ---

def headline(offer: Offer) -> str:
    p = offer.parsed
    storage = f" {format_storage(p.storage_gb)}" if p.storage_gb else ""
    return f"{p.model or 'iPhone'}{storage} — {zl(offer.price)}"


def format_offer(offer: Offer, val: Valuation, settings: Settings, *, kind: str = "new",
                 old_price: float | None = None, details_url: str | None = None) -> str:
    """Wiadomość HTML: model, pamięć, cena, zysk, werdykt, portal, miejsce, link; przy NEGOCJUJ — proponowana
    cena i gotowa wiadomość do sprzedającego w bloku, który Telegram pozwala skopiować jednym dotknięciem."""
    head = f"{_ICON.get(val.verdict, '')} <b>{escape(val.verdict.value)}</b>"
    if kind == "drop" and old_price:
        head += f" · 📉 obniżka ceny: {zl(old_price)} → <b>{zl(offer.price)}</b>"
    else:
        head += " · nowa oferta"
    p = offer.parsed
    lines = [head, f"<b>{escape(p.model or 'iPhone')}</b>"
             + (f" · {escape(format_storage(p.storage_gb))}" if p.storage_gb else "")
             + f" · cena <b>{zl(offer.price)}</b>",
             f"Szacowany zysk: <b>{zl(val.expected_profit) if val.expected_profit is not None else '—'}</b>"
             f" · ocena {val.score}/100"]
    place = offer.raw.city or "lokalizacja nieznana"
    if offer.distance_km is not None:
        place += f" ({offer.distance_km:.0f} km)"
    lines.append(f"{escape(SOURCE_NAMES.get(offer.raw.source, offer.raw.source))} · {escape(place)}")
    if val.flags:
        lines.append("⚑ " + escape(", ".join(f.label for f in dict.fromkeys(val.flags))))
    risk = val.risk
    if risk is not None and getattr(risk, "level", "low") != "low":
        lines.append(f"⚠️ <b>Ryzyko oszustwa: {escape(risk.label)}</b> — " + escape("; ".join(risk.reasons()[:3])))
    if val.verdict is Verdict.NEGOTIATE:
        neg = val.negotiation
        top = f" (maks. {zl(neg.max_price)})" if neg.max_price else ""
        lines.append(f"💬 Proponowana cena: <b>{zl(opening_price(offer, val))}</b>{top}")
        _, text = compose(offer, val, settings.message_templates, style=settings.negotiation_style,
                          pickup_km=settings.pickup_radius_km)
        lines.append("Wiadomość do sprzedającego (dotknij, aby skopiować):")
        lines.append(f"<pre>{escape(text)}</pre>")
    links = [f'<a href="{escape(offer.raw.url, quote=True)}">Otwórz ogłoszenie</a>']
    if details_url:
        links.append(f'<a href="{escape(details_url, quote=True)}">Szczegóły w PhoneBot</a>')
    lines.append(" · ".join(links))
    return "\n".join(lines)


# ------------------------------------------------------------- cisza ---

def in_quiet_hours(local: datetime, s: Settings) -> bool:
    start, end = s.telegram_quiet_start % 24, s.telegram_quiet_end % 24
    if not s.telegram_quiet_enabled or start == end:
        return False
    h = local.hour
    return start <= h < end if start < end else (h >= start or h < end)


def quiet_ends(local: datetime, s: Settings) -> datetime:
    end = local.replace(hour=s.telegram_quiet_end % 24, minute=0, second=0, microsecond=0)
    return end if end > local else end + timedelta(days=1)


# ------------------------------------------------------------- kolejka ---

@dataclass
class FlushResult:
    sent: int = 0  # wysłanych wiadomości (podsumowanie = 1)
    summarized: int = 0  # ofert zebranych w podsumowaniu
    skipped: int = 0  # pominiętych w ciszy nocnej
    waiting: int = 0  # czekają (cisza nocna, limit na godzinę, ponowienie)
    error: str | None = None


class TelegramQueue:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, *, details_base_url: str | None = None):
        self.conn = conn
        self.settings = settings
        self.details_base_url = details_base_url

    # --- od kiedy powiadamiać ---

    def since(self) -> datetime | None:
        value = SettingsRepository(self.conn).get_value(SINCE_KEY)
        return _dt(value) if value else None

    def ensure_since(self, now: datetime | None = None) -> datetime:
        """Pierwsze włączenie powiadomień: zapamiętaj chwilę — starsze oferty nigdy nie są zgłaszane."""
        current = self.since()
        if current is None:
            current = now or datetime.now(UTC)
            SettingsRepository(self.conn).set_value(SINCE_KEY, _iso(current))
        return current

    # --- dodawanie ---

    def eligible(self, offer: Offer, val: Valuation) -> bool:
        """Nowa oferta: automatycznie w „Wybrane” i spełnia osobne kryteria Telegrama."""
        s = self.settings
        if not offer.active or offer.status is OfferStatus.HIDDEN or offer.pick_excluded:
            return False
        return auto_match(offer, val, s.selection) and auto_match(offer, val, s.telegram_criteria)

    def enqueue(self, offer: Offer, val: Valuation, kind: str, *, old_price: float | None = None,
                now: datetime | None = None) -> bool:
        now = now or datetime.now(UTC)
        details = f"{self.details_base_url.rstrip('/')}/oferta/{offer.id}" if self.details_base_url else None
        text = format_offer(offer, val, self.settings, kind=kind, old_price=old_price, details_url=details)
        photo = offer.raw.photos[0] if offer.raw.photos and self.settings.telegram_photos else None
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO telegram_outbox (offer_id, kind, price, headline, text, photo, next_try, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (offer.id, kind, offer.price, headline(offer), text, photo, _iso(now), _iso(now)))
        return cur.rowcount > 0

    def enqueue_scan(self, new_ids: list[int], drop_ids: list[int], evaluate, *,
                     now: datetime | None = None) -> int:
        """Po odświeżeniu: nowe oferty i obniżki cen → kolejka. ``evaluate(offer)`` → wycena. Zwraca liczbę."""
        if not self.settings.telegram_enabled:
            return 0
        now = now or datetime.now(UTC)
        since = self.ensure_since(now)
        repo = OfferRepository(self.conn)
        added = 0
        for offer_id in new_ids:
            offer = repo.get(offer_id)
            if offer is None or offer.first_seen is None or offer.first_seen < since:
                continue
            val = evaluate(offer)
            if self.eligible(offer, val):
                repo.mark_picked([offer.id], now)
                added += self.enqueue(offer, val, "new", now=now)
        if self.settings.telegram_price_drops:
            for offer_id in drop_ids:
                offer = repo.get(offer_id)
                if offer is None or not offer.active or offer.status is OfferStatus.HIDDEN:
                    continue
                history = repo.price_history(offer_id)
                old = next((price for _, price in reversed(history[:-1]) if price > offer.price), None)
                val = evaluate(offer)
                if old is not None and is_picked(offer, val, self.settings.selection) and not high_risk(val):
                    added += self.enqueue(offer, val, "drop", old_price=old, now=now)
        return added

    # --- wysyłka ---

    def _sent_last_hour(self, now: datetime) -> int:
        cutoff = _iso(now - timedelta(hours=1))
        single = self.conn.execute("SELECT COUNT(*) FROM telegram_outbox WHERE status = 'sent' AND sent_at >= ?",
                                   (cutoff,)).fetchone()[0]
        summaries = self.conn.execute("SELECT COUNT(DISTINCT sent_at) FROM telegram_outbox "
                                      "WHERE status = 'summarized' AND sent_at >= ?", (cutoff,)).fetchone()[0]
        return int(single) + int(summaries)

    def pending(self, now: datetime) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM telegram_outbox WHERE status = 'pending' AND next_try <= ? "
                                 "ORDER BY created_at, id", (_iso(now),)).fetchall()

    def flush(self, client: TelegramClient, now: datetime | None = None) -> FlushResult:
        with _FLUSH_LOCK:
            return self._flush(client, now or datetime.now(UTC))

    def _flush(self, client: TelegramClient, now: datetime) -> FlushResult:
        res = FlushResult()
        rows = self.pending(now)
        if not rows:
            return res
        s = self.settings
        local = now.astimezone()
        if in_quiet_hours(local, s):
            ids = [r["id"] for r in rows]
            marks = ",".join("?" * len(ids))
            if s.telegram_quiet_mode == "skip":
                self.conn.execute(f"UPDATE telegram_outbox SET status = 'skipped' WHERE id IN ({marks})", ids)
                res.skipped = len(ids)
            else:
                self.conn.execute(f"UPDATE telegram_outbox SET next_try = ? WHERE id IN ({marks})",
                                  [_iso(quiet_ends(local, s)), *ids])
                res.waiting = len(ids)
            return res
        available = max(0, max(1, s.telegram_max_per_hour) - self._sent_last_hour(now))
        if available == 0:
            res.waiting = len(rows)
            return res
        single, rest = (rows, []) if len(rows) <= available else (rows[:available - 1], rows[available - 1:])
        try:
            for row in single:
                client.send(row["text"], preview_url=row["photo"])
                self._mark(row["id"], "sent", now)
                res.sent += 1
            if rest:
                client.send(self._summary(rest))
                for row in rest:
                    self._mark(row["id"], "summarized", now)
                res.sent += 1
                res.summarized = len(rest)
        except NotificationError as e:
            res.error = str(e)
            done = {r["id"] for r in self.conn.execute(
                "SELECT id FROM telegram_outbox WHERE status != 'pending' AND id IN "
                f"({','.join('?' * len(rows))})", [r["id"] for r in rows])}
            for row in rows:
                if row["id"] not in done:
                    self._retry(row, now, str(e))
            res.waiting = len(rows) - len(done)
            log.warning("Telegram: %s — ponowienie później", e)
        return res

    def _summary(self, rows: list[sqlite3.Row]) -> str:
        lines = [f"📦 Kolejne {len(rows)} ofert z „Wybrane” (limit {self.settings.telegram_max_per_hour} "
                 "wiadomości na godzinę):"]
        for r in rows[:30]:
            mark = "📉 " if r["kind"] == "drop" else "• "
            lines.append(mark + escape(r["headline"]))
        if len(rows) > 30:
            lines.append(f"…i jeszcze {len(rows) - 30}. Pełna lista w programie (zakładka „Wybrane”).")
        return "\n".join(lines)

    def _mark(self, outbox_id: int, status: str, now: datetime) -> None:
        self.conn.execute("UPDATE telegram_outbox SET status = ?, sent_at = ?, error = NULL WHERE id = ?",
                          (status, _iso(now), outbox_id))

    def _retry(self, row: sqlite3.Row, now: datetime, error: str) -> None:
        attempts = int(row["attempts"]) + 1
        if attempts >= MAX_ATTEMPTS:
            self.conn.execute("UPDATE telegram_outbox SET status = 'failed', attempts = ?, error = ? WHERE id = ?",
                              (attempts, error, row["id"]))
            return
        wait = RETRY_MINUTES[min(attempts - 1, len(RETRY_MINUTES) - 1)]
        self.conn.execute("UPDATE telegram_outbox SET attempts = ?, next_try = ?, error = ? WHERE id = ?",
                          (attempts, _iso(now + timedelta(minutes=wait)), error, row["id"]))

    def stats(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS n FROM telegram_outbox GROUP BY status")
        return {r["status"]: int(r["n"]) for r in rows}


def flush_in_background(db_path, settings: Settings) -> FlushResult:
    """Dla wątku roboczego: własne połączenie z bazą, wysyłka zaległych wiadomości."""
    from ..storage.db import connect

    if not (settings.telegram_enabled and settings.telegram_bot_token and settings.telegram_chat_id):
        return FlushResult()
    conn = connect(db_path)
    try:
        return TelegramQueue(conn, settings).flush(
            TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id))
    finally:
        conn.close()
