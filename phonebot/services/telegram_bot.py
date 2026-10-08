"""Sterowanie PhoneBot z Telegrama: komendy do bota i przyciski pod wiadomością.

* ``/pauza [czas]`` — wstrzymuje powiadomienia (``/pauza 2h``, ``/pauza 30m``, ``/pauza 1d``; bez czasu — do
  ``/wznow``). Oferty z czasu pauzy nie są wysyłane później (są w programie).
* ``/wznow`` — wznawia.
* ``/profile`` — lista profili powiadomień z przyciskami włącz / wyłącz.
* ``/status`` — stan programu i źródeł.

Bot odpowiada WYŁĄCZNIE na czat o ID z ustawień — wiadomości od innych osób są ignorowane bez odpowiedzi.
Wiadomości odbiera długim odpytywaniem (``getUpdates``) z komputera — bez serwera wystawionego do internetu.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from html import escape

from ..core.notify_profiles import describe
from ..core.settings import Settings
from ..sources import SOURCE_NAMES
from ..storage.repositories import FetchRunRepository, NotifyProfileRepository, SettingsRepository
from .notifications import NotificationError, TelegramClient
from .telegram_queue import paused_until, set_pause

log = logging.getLogger(__name__)

OFFSET_KEY = "telegram_update_offset"
PAUSED_AT_KEY = "telegram_paused_at"
HELP = ("<b>PhoneBot — komendy</b>\n"
        "/pauza 2h — wstrzymaj powiadomienia (np. 30m, 2h, 1d; bez czasu — do /wznow)\n"
        "/wznow — wznów powiadomienia\n"
        "/profile — profile powiadomień (włączanie i wyłączanie przyciskami)\n"
        "/status — stan programu i źródeł")
_UNITS = {"m": 1, "min": 1, "h": 60, "g": 60, "godz": 60, "d": 1440, "dni": 1440, "dzien": 1440, "dzień": 1440}


def parse_duration(text: str) -> timedelta | None:
    """„2h” / „30m” / „1d” / „1h30m” / „2” (= godziny) → czas; pusty / niezrozumiały → ``None``."""
    text = (text or "").strip().lower().replace(" ", "")
    if not text:
        return None
    if text.isdigit():
        return timedelta(hours=int(text))
    total = 0
    for num, unit in re.findall(r"(\d+)([a-ząćęłńóśźż]+)", text):
        if unit not in _UNITS:
            return None
        total += int(num) * _UNITS[unit]
    return timedelta(minutes=total) if total else None


def _local(dt: datetime) -> str:
    local = dt.astimezone()
    today = datetime.now().astimezone().date()
    return local.strftime("%H:%M") if local.date() == today else local.strftime("%d.%m %H:%M")


def pause(conn: sqlite3.Connection, span: timedelta | None, now: datetime | None = None) -> str:
    """Wstrzymuje powiadomienia na ``span`` (``None`` — do wznowienia). Zwraca opis dla użytkownika."""
    now = now or datetime.now(UTC)
    until = now + span if span else "forever"
    set_pause(conn, until)
    SettingsRepository(conn).set_value(PAUSED_AT_KEY, now.isoformat())
    when = "do odwołania" if until == "forever" else f"do {_local(until)}"
    return f"Powiadomienia wstrzymane {when}. Oferty z tego czasu nie przyjdą później — zobaczysz je w programie."


def resume(conn: sqlite3.Connection) -> str:
    """Wznawia powiadomienia; opis z liczbą ofert pominiętych w czasie pauzy."""
    was = paused_until(conn)
    set_pause(conn, None)
    since = SettingsRepository(conn).get_value(PAUSED_AT_KEY)
    missed = 0
    if since:
        missed = conn.execute("SELECT COUNT(*) FROM telegram_outbox WHERE status = 'paused' AND created_at >= ?",
                              (since,)).fetchone()[0]
    extra = f" W czasie pauzy pominięto {missed} ofert (są w programie)." if missed else ""
    return ("Powiadomienia wznowione." if was else "Powiadomienia działają (nie było pauzy).") + extra


def pause_text(conn: sqlite3.Connection) -> str | None:
    """„wstrzymane do 14:30” / „wstrzymane do odwołania” albo ``None``."""
    until = paused_until(conn)
    if not until:
        return None
    return "wstrzymane " + ("do odwołania" if until == "forever" else f"do {_local(until)}")


class TelegramBot:
    """Obsługa komend. ``app_state()`` — słownik ze stanem programu (z okna; wywoływany w wątku bota)."""

    def __init__(self, conn: sqlite3.Connection, settings: Settings, client: TelegramClient, *, app_state=None):
        self.conn, self.settings, self.client = conn, settings, client
        self.app_state = app_state or (lambda: {})

    # --- odbieranie ---

    def poll_once(self, timeout: int = 25) -> int:
        """Jedno odpytanie Telegrama; zwraca liczbę obsłużonych wiadomości."""
        repo = SettingsRepository(self.conn)
        offset = repo.get_value(OFFSET_KEY)
        updates = self.client.get_updates(int(offset) if offset else None, timeout=timeout)
        for upd in updates:
            try:
                self.handle(upd)
            except NotificationError as e:
                log.warning("Telegram (komenda): %s", e)
            except Exception:  # noqa: BLE001 — jedna zła wiadomość nie zatrzymuje bota
                log.exception("Telegram: błąd obsługi komendy")
            repo.set_value(OFFSET_KEY, str(int(upd["update_id"]) + 1))  # potwierdzone — nie wróci
        return len(updates)

    def _authorized(self, chat: dict | None) -> bool:
        return bool(chat) and str(chat.get("id")) == str(self.settings.telegram_chat_id).strip() != ""

    def handle(self, upd: dict) -> str | None:
        """Obsługuje wiadomość albo kliknięcie przycisku. Zwraca nazwę komendy (testy) albo ``None``."""
        if "callback_query" in upd:
            q = upd["callback_query"]
            msg = q.get("message") or {}
            if not self._authorized(msg.get("chat")):
                return None  # cudzy czat — bez odpowiedzi
            return self._button(q)
        msg = upd.get("message") or {}
        if not self._authorized(msg.get("chat")):
            return None
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            self.client.send(HELP)
            return "help"
        cmd, _, arg = text.partition(" ")
        cmd = cmd.split("@", 1)[0].lower()
        handler = {"/pauza": self.cmd_pause, "/wznow": self.cmd_resume, "/wznów": self.cmd_resume,
                   "/profile": self.cmd_profiles, "/status": self.cmd_status}.get(cmd)
        if handler is None:
            self.client.send(HELP)
            return "help"
        handler(arg)
        return cmd.lstrip("/")

    # --- komendy ---

    def cmd_pause(self, arg: str = "") -> None:
        span = parse_duration(arg)
        if arg.strip() and span is None:
            self.client.send("Nie rozumiem czasu pauzy. Przykłady: <code>/pauza 30m</code>, <code>/pauza 2h</code>, "
                             "<code>/pauza 1d</code>, albo samo <code>/pauza</code> — do /wznow.")
            return
        self.client.send(f"⏸ {pause(self.conn, span)} /wznow — wznowienie.")

    def cmd_resume(self, _arg: str = "") -> None:
        self.client.send(f"▶️ {resume(self.conn)}")

    def _profiles_message(self) -> tuple[str, list[list[dict]]]:
        repo = NotifyProfileRepository(self.conn)
        repo.ensure_default(self.settings)
        profiles = repo.all()
        week = repo.sent_counts(datetime.now(UTC) - timedelta(days=7))
        if not profiles:
            return "Brak profili powiadomień — dodaj je w programie (Ustawienia → Powiadomienia).", []
        lines = ["<b>Profile powiadomień</b> (dotknij, aby włączyć / wyłączyć):"]
        buttons = []
        for p in profiles:
            mark = "✅" if p.enabled else "⛔"
            lines.append(f"\n{mark} <b>{escape(p.name)}</b> — {week.get(p.id, 0)} w 7 dni\n"
                         f"<i>{escape(describe(p))}</i>")
            buttons.append([{"text": f"{mark} {p.name}", "callback_data": f"p:{p.id}"}])
        pause = paused_until(self.conn)
        if pause:
            lines.append("\n⏸ Powiadomienia są wstrzymane " + ("do /wznow" if pause == "forever" else
                                                               f"do {_local(pause)}") + ".")
        return "\n".join(lines), buttons

    def cmd_profiles(self, _arg: str = "") -> None:
        text, buttons = self._profiles_message()
        self.client.send(text, buttons=buttons or None)

    def _button(self, q: dict) -> str | None:
        data = q.get("data") or ""
        if not data.startswith("p:") or not data[2:].isdigit():
            self.client.answer_button(q.get("id", ""))
            return None
        repo = NotifyProfileRepository(self.conn)
        p = repo.get(int(data[2:]))
        if p is None:
            self.client.answer_button(q.get("id", ""), "Tego profilu już nie ma.")
            return None
        repo.set_enabled(p.id, not p.enabled)
        self.client.answer_button(q.get("id", ""), f"„{p.name}” {'wyłączony' if p.enabled else 'włączony'}")
        msg = q.get("message") or {}
        text, buttons = self._profiles_message()
        self.client.edit(str(msg.get("chat", {}).get("id")), int(msg.get("message_id", 0)), text, buttons)
        return "toggle"

    def cmd_status(self, _arg: str = "") -> None:
        from .. import __version__

        state = self.app_state() or {}
        lines = [f"<b>PhoneBot {__version__}</b>"]
        pause = paused_until(self.conn)
        lines.append("⏸ Powiadomienia wstrzymane " + ("do /wznow" if pause == "forever" else f"do {_local(pause)}")
                     if pause else "🔔 Powiadomienia włączone")
        offers = self.conn.execute("SELECT COUNT(*) FROM offers WHERE is_active = 1 AND archived_at IS NULL "
                                   "AND status != 'hidden'").fetchone()[0]
        lines.append(f"Ofert w tabeli: {offers}" + (f" · zielonych: {state['green']}" if "green" in state else ""))
        if state.get("last_refresh"):
            lines.append(f"Ostatnie odświeżenie: {state['last_refresh']}")
        runs = FetchRunRepository(self.conn).latest_by_source()
        if runs:
            lines.append("\n<b>Źródła</b>")
            for src, r in sorted(runs.items()):
                icon = {"ok": "✅", "empty": "✅", "blocked": "⛔", "error": "⚠️", "running": "⏳"}.get(r["status"], "•")
                when = _local(datetime.fromisoformat(r["finished_at"] or r["started_at"]))
                detail = f"{r['new_offers']} nowych" if r["status"] in ("ok", "empty") else (r["error"] or r["status"])
                lines.append(f"{icon} {escape(SOURCE_NAMES.get(src, src))} — {when}, {escape(str(detail))[:80]}")
        week = self.conn.execute("SELECT COUNT(*) FROM telegram_outbox WHERE status IN ('sent', 'summarized') "
                                 "AND sent_at >= ?", ((datetime.now(UTC) - timedelta(days=7)).isoformat(),)
                                 ).fetchone()[0]
        waiting = self.conn.execute("SELECT COUNT(*) FROM telegram_outbox WHERE status = 'pending'").fetchone()[0]
        on = sum(1 for p in NotifyProfileRepository(self.conn).all() if p.enabled)
        lines.append(f"\nPowiadomień w 7 dni: {week} · w kolejce: {waiting} · włączone profile: {on}")
        self.client.send("\n".join(lines))


class BotThread:
    """Wątek odbierający komendy (długie odpytywanie) — działa, gdy Telegram jest skonfigurowany."""

    def __init__(self, db_path, settings: Settings, *, app_state=None, client_factory=TelegramClient):
        self.db_path, self.settings = db_path, settings
        self.app_state = app_state
        self.client_factory = client_factory
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="phonebot-telegram-bot", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def update_settings(self, settings: Settings) -> None:
        """Nowe ustawienia (np. inny token) — wątek weźmie je przy następnym odpytaniu, bez restartu."""
        self.settings = settings

    def _run(self) -> None:
        from ..storage.db import connect

        conn = connect(self.db_path)
        bot, key, errors = None, None, 0
        try:
            while not self._stop.is_set():
                s = self.settings
                try:
                    if (s.telegram_bot_token, s.telegram_chat_id) != key:
                        bot = None
                        bot = TelegramBot(conn, s, self.client_factory(s.telegram_bot_token, s.telegram_chat_id),
                                          app_state=self.app_state)
                        key = (s.telegram_bot_token, s.telegram_chat_id)
                    bot.settings = s
                    bot.poll_once(timeout=25)
                    errors = 0
                except Exception as e:  # brak internetu, zły token — spróbuj później; wątek nie może zginąć
                    errors += 1
                    if isinstance(e, NotificationError):
                        log.info("Telegram (odbieranie komend): %s", e)
                    else:
                        log.exception("Telegram (odbieranie komend)")
                    self._stop.wait(min(300, 15 * errors))
        finally:
            conn.close()
