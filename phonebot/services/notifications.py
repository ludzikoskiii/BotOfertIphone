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

    def _call(self, method: str, payload: dict, *, timeout: float = 15) -> dict:
        url = TELEGRAM_API.format(token=self.token, method=method)
        try:
            with httpx.Client(timeout=timeout, transport=self._transport) as client:
                data = client.post(url, json=payload).json()
        except (httpx.HTTPError, ValueError) as e:
            raise NotificationError(f"Telegram: błąd połączenia ({e.__class__.__name__})") from e
        if not data.get("ok"):
            raise NotificationError(f"Telegram: {data.get('description') or 'nieznany błąd'}")
        return data

    def send(self, text: str, *, preview_url: str | None = None, buttons: list[list[dict]] | None = None) -> dict:
        """Wiadomość HTML. ``preview_url`` — miniatura (np. zdjęcie oferty) jako podgląd linku nad tekstem;
        ``buttons`` — przyciski pod wiadomością (``[[{"text": …, "callback_data": …}]]``)."""
        if not self.chat_id:
            raise NotificationError("brak chat ID Telegram")
        payload: dict = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"}
        if preview_url:
            payload["link_preview_options"] = {"url": preview_url, "prefer_small_media": True,
                                               "show_above_text": True}
        else:
            payload["link_preview_options"] = {"is_disabled": True}
        if buttons:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        return self._call("sendMessage", payload).get("result") or {}

    # --- sterowanie z Telegrama (komendy, przyciski) ---

    def get_updates(self, offset: int | None, timeout: int = 25) -> list[dict]:
        """Nowe wiadomości do bota (długie odpytywanie — czeka do ``timeout`` s na nową wiadomość)."""
        payload: dict = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return self._call("getUpdates", payload, timeout=timeout + 15).get("result") or []

    def edit(self, chat_id: str, message_id: int, text: str, buttons: list[list[dict]] | None = None) -> None:
        payload: dict = {"chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
                         "link_preview_options": {"is_disabled": True}}
        if buttons is not None:
            payload["reply_markup"] = {"inline_keyboard": buttons}
        try:
            self._call("editMessageText", payload)
        except NotificationError as e:
            if "not modified" not in str(e):  # ta sama treść — Telegram zgłasza błąd, ale nic nie trzeba robić
                raise

    def answer_button(self, callback_id: str, text: str = "") -> None:
        self._call("answerCallbackQuery", {"callback_query_id": callback_id, "text": text[:190]})

    def find_chat_id(self) -> str:
        """Chat ID z ostatniej wiadomości wysłanej do bota (najpierw napisz do bota /start)."""
        updates = self._call("getUpdates", {"limit": 20}).get("result") or []
        for upd in reversed(updates):
            msg = upd.get("message") or upd.get("channel_post") or {}
            chat = msg.get("chat") or {}
            if "id" in chat:
                return str(chat["id"])
        raise NotificationError("brak wiadomości — wyślij do swojego bota /start i spróbuj ponownie")
