"""Profile powiadomień w praktyce: podgląd „co profil wysłałby z ostatnich 24 godzin” i testowe powiadomienie.

Podgląd liczy oferty, które pojawiły się w ostatnich ``hours`` godzinach (także już sprzedane — wtedy były
aktywne), i sprawdza je filtrami profilu — bez ciszy nocnej i limitu na godzinę (te zbierają nadmiar
w podsumowanie, nie zmieniają liczby pasujących ofert).
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from ..core.models import Mode, Offer, Valuation
from ..core.notify_profiles import NotifyProfile, reject_reason
from ..core.selection import auto_match
from ..core.settings import Settings
from ..storage.repositories import OfferRepository


@dataclass
class Preview:
    hours: int
    total: int  # ofert w oknie czasu
    matched: list[tuple[Offer, Valuation]] = field(default_factory=list)  # najlepsze najpierw
    reasons: Counter = field(default_factory=Counter)  # najczęstsze powody odrzucenia

    @property
    def count(self) -> int:
        return len(self.matched)

    def summary(self) -> str:
        n = self.count
        word = "powiadomienie" if n == 1 else ("powiadomienia" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14)
                                              else "powiadomień")
        text = f"Z ostatnich {self.hours} godzin ten profil wysłałby {n} {word} (z {self.total} nowych ofert)."
        if self.reasons:
            top = ", ".join(f"{r} ({c})" for r, c in self.reasons.most_common(3))
            text += f" Najczęściej odpada: {top}."
        return text


def _valuator(evaluator, app_mode: Mode):
    cache: dict[tuple[int, Mode], Valuation] = {}

    def valuate(offer: Offer, mode: Mode) -> Valuation:
        key = (offer.id or id(offer), mode)
        if key not in cache:
            cache[key] = evaluator.evaluate(offer, mode)
        return cache[key]
    return valuate


def preview(conn: sqlite3.Connection, settings: Settings, profile: NotifyProfile, *, hours: int = 24,
            now: datetime | None = None, offers: list[Offer] | None = None, evaluator=None) -> Preview:
    """Co ten profil wysłałby z ostatnich ``hours`` godzin (bez ciszy i limitu)."""
    from .evaluator import Evaluator

    now = now or datetime.now(UTC)
    offers = OfferRepository(conn).seen_since(now - timedelta(hours=hours)) if offers is None else offers
    evaluator = evaluator or Evaluator(conn, settings)
    app_mode = settings.mode_enum
    valuate = _valuator(evaluator, app_mode)
    mode = profile.evaluation_mode(app_mode)
    out = Preview(hours, len(offers))
    for offer in offers:
        if offer.pick_excluded:
            out.reasons["usunięta z „Wybrane”"] += 1
            continue
        val = valuate(offer, mode)
        picked = auto_match(offer, valuate(offer, app_mode), settings.selection) if profile.require_picked else None
        reason = reject_reason(offer, val, profile, picked=picked)
        if reason is None:
            out.matched.append((offer, val))
        else:
            out.reasons[reason] += 1
    out.matched.sort(key=lambda ov: ov[1].expected_profit or 0, reverse=True)
    return out


def preview_in_background(db_path, settings: Settings, profile: NotifyProfile, hours: int = 24) -> Preview:
    from ..storage.db import connect

    conn = connect(db_path)
    try:
        return preview(conn, settings, profile, hours=hours)
    finally:
        conn.close()


def send_test(conn: sqlite3.Connection, settings: Settings, profile: NotifyProfile, client, *,
              now: datetime | None = None, details_base_url: str | None = None) -> str:
    """Testowe powiadomienie z profilu: najlepsza pasująca oferta z ostatnich 24 h (a gdy brak — z 7 dni) w takiej
    postaci, jak przyjdzie naprawdę; bez wpisu do kolejki (nie blokuje późniejszego prawdziwego powiadomienia)."""
    from .telegram_queue import format_offer

    pv = preview(conn, settings, profile, hours=24, now=now)
    if not pv.matched:
        pv = preview(conn, settings, profile, hours=24 * 7, now=now)
    if not pv.matched:
        text = (f"🧪 <b>Test profilu „{profile.name}”</b>\nPołączenie działa, ale z ostatnich 7 dni żadna oferta "
                "nie pasuje do tego profilu — filtr może być za ostry.")
        client.send(text)
        return "wysłano test (brak pasujących ofert z 7 dni)"
    offer, val = pv.matched[0]
    details = f"{details_base_url.rstrip('/')}/oferta/{offer.id}" if details_base_url else None
    body = format_offer(offer, val, settings, details_url=details, profiles=[profile.name])
    photo = offer.raw.photos[0] if offer.raw.photos and settings.telegram_photos else None
    client.send(f"🧪 <b>TEST — tak wygląda powiadomienie z profilu „{profile.name}”</b>\n{body}", preview_url=photo)
    return f"wysłano test: {offer.parsed.model or offer.raw.title} ({pv.count} pasujących ofert z {pv.hours} h)"


def send_test_in_background(db_path, settings: Settings, profile: NotifyProfile, token: str | None = None,
                            chat_id: str | None = None) -> str:
    """Test z okna programu (wątek roboczy): token i chat ID z pól okna albo z ustawień."""
    from ..storage.db import connect
    from .notifications import TelegramClient

    token, chat_id = token or settings.telegram_bot_token, chat_id or settings.telegram_chat_id
    if not (token and chat_id):
        raise ValueError("najpierw wpisz token bota i chat ID (Ustawienia → Powiadomienia)")
    link = settings.web_url if settings.web_enabled and settings.web_url else None
    conn = connect(db_path)
    try:
        return send_test(conn, settings, profile, TelegramClient(token, chat_id), details_base_url=link)
    finally:
        conn.close()
