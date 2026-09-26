"""Powiadomienia o nowych zielonych ofertach: Telegram (tu) i pulpit (w GUI)."""
from __future__ import annotations

import logging
from html import escape

import httpx

from ..core.catalog import format_storage
from ..core.models import Offer, Valuation
from ..core.settings import Settings
from ..sources import SOURCE_NAMES

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class NotificationError(Exception):
    pass


def _zl(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f} zł".replace(",", " ")


def offer_headline(offer: Offer, val: Valuation) -> str:
    p = offer.parsed
    return f"{p.model or '?'} {format_storage(p.storage_gb)} — {_zl(offer.price)}"


def format_telegram(offer: Offer, val: Valuation, reason: str = "") -> str:
    """Wiadomość HTML do Telegrama."""
    lines = [f"🟢 <b>{escape(val.verdict.value)}</b> · ocena {val.score}/100" + (f" · {escape(reason)}" if reason else ""),
             f"<b>{escape(offer_headline(offer, val))}</b>",
             escape(offer.raw.title),
             f"Zysk ok. <b>{_zl(val.expected_profit)}</b> · max cena {_zl(val.max_buy_price)}"]
    neg = val.negotiation
    if neg.opening_price:
        lines.append(f"Negocjuj: zacznij od {_zl(neg.opening_price)}, maks. {_zl(neg.max_price)}")
    place = offer.raw.city or "—"
    if offer.distance_km is not None:
        place += f" ({offer.distance_km:.0f} km)"
    lines.append(f"{escape(SOURCE_NAMES.get(offer.raw.source, offer.raw.source))} · {escape(place)}")
    if val.flags:
        lines.append("⚑ " + escape(", ".join(f.label for f in dict.fromkeys(val.flags))))
    lines.append(escape(offer.raw.url))
    return "\n".join(lines)


class TelegramClient:
    def __init__(self, token: str, chat_id: str = "", *, transport: httpx.BaseTransport | None = None):
        if not token.strip():
            raise NotificationError("brak tokenu bota Telegram")
        self.token = token.strip()
        self.chat_id = chat_id.strip()
        self._transport = transport

    def _call(self, method: str, payload: dict) -> dict:
        url = TELEGRAM_API.format(token=self.token, method=method)
        try:
            with httpx.Client(timeout=15, transport=self._transport) as client:
                data = client.post(url, json=payload).json()
        except (httpx.HTTPError, ValueError) as e:
            raise NotificationError(f"Telegram: błąd połączenia ({e.__class__.__name__})") from e
        if not data.get("ok"):
            raise NotificationError(f"Telegram: {data.get('description') or 'nieznany błąd'}")
        return data

    def send(self, text: str) -> None:
        if not self.chat_id:
            raise NotificationError("brak chat ID Telegram")
        self._call("sendMessage", {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                                   "disable_web_page_preview": False})

    def find_chat_id(self) -> str:
        """Chat ID z ostatniej wiadomości wysłanej do bota (najpierw napisz do bota /start)."""
        updates = self._call("getUpdates", {"limit": 20}).get("result") or []
        for upd in reversed(updates):
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                return str(chat["id"])
        raise NotificationError("brak wiadomości — wyślij do swojego bota /start i spróbuj ponownie")


def send_telegram_batch(settings: Settings, items: list[tuple[Offer, Valuation, str]],
                        client: TelegramClient | None = None) -> int:
    """Wysyła powiadomienia (max ``notify_max_per_scan`` osobno + podsumowanie reszty)."""
    if not items:
        return 0
    client = client or TelegramClient(settings.telegram_bot_token, settings.telegram_chat_id)
    limit = max(1, settings.notify_max_per_scan)
    for offer, val, reason in items[:limit]:
        client.send(format_telegram(offer, val, reason))
    rest = items[limit:]
    if rest:
        lines = [f"…i jeszcze {len(rest)} zielonych ofert:"]
        lines += [f"• {escape(offer_headline(o, v))} — zysk {_zl(v.expected_profit)}" for o, v, _ in rest[:15]]
        client.send("\n".join(lines))
    return min(len(items), limit) + (1 if rest else 0)
