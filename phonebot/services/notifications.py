"""Klient Telegram (Bot API) i nagłówek oferty do powiadomień; kolejka wysyłek: ``telegram_queue``."""
from __future__ import annotations

import logging

import httpx

from ..core.catalog import format_storage
from ..core.models import Offer, Valuation

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class NotificationError(Exception):
    pass


def _zl(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f} zł".replace(",", " ")


def offer_headline(offer: Offer, val: Valuation) -> str:
    p = offer.parsed
    return f"{p.model or '?'} {format_storage(p.storage_gb)} — {_zl(offer.price)}"


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

    def send(self, text: str, *, preview_url: str | None = None) -> None:
        """Wiadomość HTML. ``preview_url`` — miniatura (np. zdjęcie oferty) jako podgląd linku nad tekstem."""
        if not self.chat_id:
            raise NotificationError("brak chat ID Telegram")
        payload: dict = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if preview_url:
            payload["link_preview_options"] = {"url": preview_url, "prefer_small_media": True,
                                               "show_above_text": True}
        else:
            payload["link_preview_options"] = {"is_disabled": True}
        self._call("sendMessage", payload)

    def find_chat_id(self) -> str:
        """Chat ID z ostatniej wiadomości wysłanej do bota (najpierw napisz do bota /start)."""
        updates = self._call("getUpdates", {"limit": 20}).get("result") or []
        for upd in reversed(updates):
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                return str(chat["id"])
        raise NotificationError("brak wiadomości — wyślij do swojego bota /start i spróbuj ponownie")
