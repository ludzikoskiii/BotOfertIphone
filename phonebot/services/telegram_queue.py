"""Powiadomienia Telegram: kolejka w bazie, bez duplikatów, z ponowieniami, ciszą nocną i limitem na godzinę.

Kiedy wiadomość trafia do kolejki (``enqueue_scan``, po każdym odświeżeniu):

* **nowa oferta** pasująca do co najmniej jednego włączonego **profilu powiadomień** (``core.notify_profiles``)
  — każda oferta najwyżej raz (unikalny wpis w ``telegram_outbox``), z listą profili, do których pasuje;
* opcjonalnie **obniżka ceny** oferty z „Wybrane” — raz na każdą nową, niższą cenę;
* nigdy dla ofert sprzed pierwszego włączenia powiadomień (``telegram_since``) i nigdy przy pierwszym
  pobraniu do pustej bazy.

Wysyłka (``flush``) działa w tle — po skanie i co kilka minut: w ciszy nocnej wiadomości czekają do rana
(albo są pomijane — do wyboru), ponad limit na godzinę trafiają do jednego podsumowania, a po błędzie
sieci są ponawiane z rosnącą przerwą. Profil może mieć własną ciszę nocną i własny limit na godzinę (wiadomość
z kilku profili czeka tylko, gdy żaden z nich nie pozwala jej teraz wysłać). „/pauza” z Telegrama wstrzymuje
wysyłkę — oferty z czasu pauzy są pomijane (widać je w programie). Blokada chroni przed podwójną wysyłką.
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
from ..core.models import Mode, Offer, OfferStatus, Valuation, Verdict
from ..core.notify_profiles import NotifyProfile, in_quiet, matching_profiles, profile_quiet, quiet_end
from ..core.selection import auto_match, high_risk, is_picked
from ..core.settings import Settings
from ..net.http import HostRateLimiter
from ..sources import SOURCE_NAMES
from ..sources.pages import PageFetcher
from ..storage.repositories import NotifyProfileRepository, OfferRepository, SettingsRepository
from .notifications import NotificationError, TelegramClient

log = logging.getLogger(__name__)

SINCE_KEY = "telegram_since"
PAUSE_KEY = "telegram_paused_until"  # ISO albo „forever” (do /wznow)
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
                 old_price: float | None = None, details_url: str | None = None,
                 profiles: list[str] | None = None) -> str:
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
             + (f" · {val.profit_per_hour:.0f} zł/h" if val.profit_per_hour is not None else "")
             + f" · ocena {val.score}/100"]
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
    if profiles:
        lines.append("📋 Profil: " + escape(" · ".join(profiles)))
    return "\n".join(lines)


# ------------------------------------------------------------- cisza ---

def in_quiet_hours(local: datetime, s: Settings) -> bool:
    """Globalna cisza nocna (profil może mieć własną — ``core.notify_profiles.profile_quiet``)."""
    return in_quiet(local, s.telegram_quiet_enabled, s.telegram_quiet_start, s.telegram_quiet_end)


def quiet_ends(local: datetime, s: Settings) -> datetime:
    return quiet_end(local, s.telegram_quiet_end)


# ------------------------------------------------------------- pauza ---

def paused_until(conn: sqlite3.Connection, now: datetime | None = None) -> datetime | str | None:
    """Pauza z Telegrama (/pauza): koniec pauzy, „forever” albo ``None`` (brak pauzy / już minęła)."""
    value = SettingsRepository(conn).get_value(PAUSE_KEY)
    if not value:
        return None
    if value == "forever":
        return "forever"
    until = _dt(value)
    return until if until > (now or datetime.now(UTC)) else None


def set_pause(conn: sqlite3.Connection, until: datetime | str | None) -> None:
    SettingsRepository(conn).set_value(PAUSE_KEY, "" if until is None else
                                       until if isinstance(until, str) else _iso(until))


# ------------------------------------------------------------- kolejka ---

@dataclass
class FlushResult:
    sent: int = 0  # wysłanych wiadomości (podsumowanie = 1)
    summarized: int = 0  # ofert zebranych w podsumowaniu
    skipped: int = 0  # pominiętych w ciszy nocnej
    waiting: int = 0  # czekają (cisza nocna, limit na godzinę, ponowienie)
    paused: int = 0  # pominięte — pauza z Telegrama (/pauza)
    gone: int = 0  # niewysłane — oferta sprzedana / zakończona (sprawdzone przed wysyłką)
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

    def profiles(self) -> list[NotifyProfile]:
        repo = NotifyProfileRepository(self.conn)
        repo.ensure_default(self.settings)  # pierwsze uruchomienie: dotychczasowe ustawienia → „Domyślny”
        return repo.all()

    def matches(self, offer: Offer, evaluate, profiles: list[NotifyProfile] | None = None
                ) -> list[tuple[NotifyProfile, Valuation]]:
        """Włączone profile, do których pasuje nowa oferta (wycena w trybie profilu, zapamiętywana)."""
        app_mode = self.settings.mode_enum
        cache: dict[Mode, Valuation] = {}

        def valuate(o: Offer, mode: Mode) -> Valuation:
            if mode not in cache:
                cache[mode] = evaluate(o) if mode is app_mode else evaluate(o, mode)
            return cache[mode]

        def picked(o: Offer, _val: Valuation) -> bool:  # „Wybrane” liczone jak w programie (tryb programu)
            return auto_match(o, valuate(o, app_mode), self.settings.selection)

        return matching_profiles(offer, valuate, self.profiles() if profiles is None else profiles, app_mode,
                                 picked=picked)

    def eligible(self, offer: Offer, val: Valuation) -> bool:
        """Czy nowa oferta pasuje do któregoś włączonego profilu (wycena ``val`` w trybie programu)."""
        return bool(self.matches(offer, lambda o, mode=None: val))

    def enqueue(self, offer: Offer, val: Valuation, kind: str, *, old_price: float | None = None,
                now: datetime | None = None, profiles: list[NotifyProfile] | None = None) -> bool:
        now = now or datetime.now(UTC)
        details = f"{self.details_base_url.rstrip('/')}/oferta/{offer.id}" if self.details_base_url else None
        names = [p.name for p in profiles or []]
        text = format_offer(offer, val, self.settings, kind=kind, old_price=old_price, details_url=details,
                            profiles=names)
        photo = offer.raw.photos[0] if offer.raw.photos and self.settings.telegram_photos else None
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO telegram_outbox (offer_id, kind, price, headline, text, photo, next_try, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (offer.id, kind, offer.price, headline(offer), text, photo, _iso(now), _iso(now)))
        if cur.rowcount and profiles:
            self.conn.executemany("INSERT OR IGNORE INTO telegram_outbox_profiles (outbox_id, profile_id) "
                                  "VALUES (?, ?)", [(cur.lastrowid, p.id) for p in profiles if p.id is not None])
        return cur.rowcount > 0

    def enqueue_scan(self, new_ids: list[int], drop_ids: list[int], evaluate, *,
                     now: datetime | None = None) -> int:
        """Po odświeżeniu: nowe oferty i obniżki cen → kolejka. ``evaluate(offer)`` → wycena. Zwraca liczbę."""
        if not self.settings.telegram_enabled:
            return 0
        now = now or datetime.now(UTC)
        since = self.ensure_since(now)
        repo = OfferRepository(self.conn)
        profiles = self.profiles()
        added = 0
        for offer_id in new_ids:
            offer = repo.get(offer_id)
            if offer is None or offer.first_seen is None or offer.first_seen < since:
                continue
            found = self.matches(offer, evaluate, profiles)
            if found:  # jedna wiadomość na ofertę, z listą pasujących profili (wycena z pierwszego)
                if auto_match(offer, found[0][1], self.settings.selection):
                    repo.mark_picked([offer.id], now)
                added += self.enqueue(offer, found[0][1], "new", now=now, profiles=[p for p, _ in found])
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

    def flush(self, client: TelegramClient, now: datetime | None = None, *, checker=None) -> FlushResult:
        """``checker(offer_id) -> wynik`` — sprawdzenie strony oferty przed wysyłką (domyślnie ``check_one``)."""
        with _FLUSH_LOCK:
            return self._flush(client, now or datetime.now(UTC), checker)

    def _drop_gone(self, rows: list[sqlite3.Row], now: datetime, res: FlushResult, checker) -> list[sqlite3.Row]:
        """Wiadomości czekające dłużej niż ``open_check_minutes`` (cisza nocna, limit, ponowienia): najpierw strona
        oferty — sprzedanej / zakończonej nie wysyłamy. Świeże wiadomości (oferta widziana przed chwilą) bez sprawdzania."""
        limit = timedelta(minutes=max(1, self.settings.refresh.open_check_minutes))
        fetcher = None
        keep = []
        try:
            for row in rows:
                active = self.conn.execute("SELECT is_active FROM offers WHERE id = ?", (row["offer_id"],)).fetchone()
                gone = active is None or not active["is_active"]
                if not gone and now - (_dt(row["created_at"]) or now) >= limit:
                    if checker is None:
                        from .offer_checks import check_one  # tu: offer_checks importuje skaner

                        fetcher = fetcher or PageFetcher(HostRateLimiter(self.settings.request_delay_s))
                        outcome = check_one(self.conn, self.settings, row["offer_id"], fetcher=fetcher, now=now)
                    else:
                        outcome = checker(row["offer_id"])
                    gone = outcome == "gone"
                if gone:
                    self.conn.execute("UPDATE telegram_outbox SET status = 'skipped', error = ? WHERE id = ?",
                                      ("oferta sprzedana / zakończona — niewysłane", row["id"]))
                    res.gone += 1
                else:
                    keep.append(row)
        finally:
            if fetcher is not None:
                fetcher.close()
        return keep

    def _flush(self, client: TelegramClient, now: datetime, checker=None) -> FlushResult:
        res = FlushResult()
        rows = self.pending(now)
        if not rows:
            return res
        s = self.settings
        if paused_until(self.conn, now) is not None:  # /pauza: oferty z czasu pauzy nie są wysyłane później
            ids = [r["id"] for r in rows]
            self.conn.execute(f"UPDATE telegram_outbox SET status = 'paused' WHERE id IN ({','.join('?' * len(ids))})",
                              ids)
            res.paused = len(ids)
            return res
        rows = self._quiet_and_profiles(rows, now, res)
        if not rows:
            return res
        rows = self._drop_gone(rows, now, res, checker)
        if not rows:
            return res
        rows = self._profile_limits(rows, now, res)
        if not rows:
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

    def _row_profiles(self, rows: list[sqlite3.Row]) -> dict[int, list[int]]:
        ids = [r["id"] for r in rows]
        out: dict[int, list[int]] = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in self.conn.execute("SELECT outbox_id, profile_id FROM telegram_outbox_profiles WHERE outbox_id IN "
                                       f"({','.join('?' * len(chunk))})", chunk):
                out.setdefault(int(r[0]), []).append(int(r[1]))
        return out

    def _quiet_and_profiles(self, rows: list[sqlite3.Row], now: datetime, res: FlushResult) -> list[sqlite3.Row]:
        """Profil wyłączony / usunięty → wiadomość pominięta; cisza nocna: czeka, gdy WSZYSTKIE jej profile
        (albo globalna cisza — obniżki cen, wiadomości bez profilu) mają teraz ciszę."""
        s = self.settings
        local = now.astimezone()
        profiles = {p.id: p for p in NotifyProfileRepository(self.conn).all()}
        linked = self._row_profiles(rows)
        self._linked, self._profiles_by_id = linked, profiles
        keep = []
        for row in rows:
            ids = linked.get(row["id"], [])
            active = [profiles[i] for i in ids if i in profiles and profiles[i].enabled]
            if ids and not active:
                self.conn.execute("UPDATE telegram_outbox SET status = 'skipped', error = ? WHERE id = ?",
                                  ("profil wyłączony albo usunięty", row["id"]))
                res.skipped += 1
                continue
            scopes = active or [None]
            quiet = [(in_quiet(local, *profile_quiet(p, s)), p) for p in scopes]
            if all(q for q, _ in quiet):
                if s.telegram_quiet_mode == "skip":
                    self.conn.execute("UPDATE telegram_outbox SET status = 'skipped' WHERE id = ?", (row["id"],))
                    res.skipped += 1
                else:
                    wake = min(quiet_end(local, profile_quiet(p, s)[2]) for _, p in quiet)
                    self.conn.execute("UPDATE telegram_outbox SET next_try = ? WHERE id = ?", (_iso(wake), row["id"]))
                    res.waiting += 1
                continue
            keep.append(row)
        return keep

    def _profile_limits(self, rows: list[sqlite3.Row], now: datetime, res: FlushResult) -> list[sqlite3.Row]:
        """Limit na godzinę ustawiony w profilu: wiadomość czeka, gdy każdy jej profil wyczerpał swój limit."""
        limited = {pid: p for pid, p in self._profiles_by_id.items() if p.max_per_hour}
        if not limited:
            return rows
        cutoff = _iso(now - timedelta(hours=1))
        sent = {int(r[0]): int(r[1]) for r in self.conn.execute(
            "SELECT p.profile_id, COUNT(*) FROM telegram_outbox_profiles p JOIN telegram_outbox o ON o.id = p.outbox_id "
            "WHERE o.status = 'sent' AND o.sent_at >= ? GROUP BY p.profile_id", (cutoff,))}
        keep = []
        for row in rows:
            ids = [i for i in self._linked.get(row["id"], []) if i in self._profiles_by_id
                   and self._profiles_by_id[i].enabled]
            free = [i for i in ids if i not in limited or sent.get(i, 0) < limited[i].max_per_hour]
            if ids and not free:
                res.waiting += 1  # spróbujemy przy następnej wysyłce (co kilka minut)
                continue
            for i in ids:
                sent[i] = sent.get(i, 0) + 1
            keep.append(row)
        return keep

    def _summary(self, rows: list[sqlite3.Row]) -> str:
        lines = [f"📦 Kolejne {len(rows)} ofert (limit {self.settings.telegram_max_per_hour} "
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
