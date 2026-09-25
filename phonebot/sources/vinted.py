"""Adapter Vinted (tryb „best effort").

Vinted nie ma publicznego API. Adapter najpierw otwiera stronę główną, żeby
dostać cookie sesji, a potem korzysta z endpointu JSON katalogu, z którego
ładuje dane sama strona. Vinted ma silną ochronę antybotową — przy blokadzie
adapter zgłasza błąd źródła, a pozostałe portale działają dalej.

Na Vinted wszystkie transakcje są z wysyłką; kupujący płaci opłatę za ochronę
(uwzględniana w kosztach zakupu, patrz ustawienia „Opłaty kupującego").
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from ..core.models import RawOffer
from ..core.settings import Settings
from ..net.http import HttpClient, HttpError
from .base import SearchQuery, SourceAdapter, SourceError, register
from .extract import parse_price

log = logging.getLogger(__name__)

BASE_URL = "https://www.vinted.pl"
API_URL = BASE_URL + "/api/v2/catalog/items"
PER_PAGE = 96

_CONDITIONS = {
    "nowy z metką": "new", "nowy bez metki": "new", "new with tags": "new", "new without tags": "new",
    "bardzo dobry": "used", "dobry": "used", "zadowalający": "used", "very good": "used", "good": "used",
    "satisfactory": "used",
}


def parse_item(item: dict[str, Any]) -> RawOffer | None:
    price, currency = parse_price(item.get("price"))
    currency = (currency or item.get("currency") or "PLN").upper()
    if price is None or currency != "PLN":
        return None
    photos = []
    photo = item.get("photo") or {}
    if isinstance(photo, dict):
        url = photo.get("url") or photo.get("full_size_url")
        if url:
            photos.append(url)
    for extra in item.get("photos") or []:
        if isinstance(extra, dict) and extra.get("url") and extra["url"] not in photos:
            photos.append(extra["url"])
    status = str(item.get("status") or "").strip().lower()
    params = {"condition": _CONDITIONS[status]} if status in _CONDITIONS else {}
    total, _ = parse_price(item.get("total_item_price"))
    if total and total > price:
        params["buyer_fee"] = f"{total - price:.2f}"
    created = None
    ts = (photo.get("high_resolution") or {}).get("timestamp") if isinstance(photo, dict) else None
    if isinstance(ts, (int, float)):
        created = datetime.fromtimestamp(ts).astimezone()
    return RawOffer(
        source=VintedAdapter.key,
        source_id=str(item["id"]),
        url=item.get("url") or f"{BASE_URL}/items/{item['id']}",
        title=str(item.get("title") or "").strip(),
        price=price,
        description=str(item.get("description") or ""),
        currency=currency,
        photos=photos,
        created_at=created,
        shipping_available=True,
        params=params,
    )


@register
class VintedAdapter(SourceAdapter):
    key = "vinted"
    display_name = "Vinted"

    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.settings = settings
        self._session_ready = False

    async def _ensure_session(self) -> None:
        if not self._session_ready:
            await self.http.get_text(BASE_URL + "/", use_cache=False)
            self._session_ready = True

    async def search(self, query: SearchQuery) -> list[RawOffer]:
        try:
            await self._ensure_session()
        except HttpError as e:
            raise SourceError(f"nie udało się otworzyć sesji Vinted: {e}") from e
        results: dict[str, RawOffer] = {}
        errors: list[str] = []
        for phrase in query.phrases:
            try:
                await self._search_phrase(phrase, query, results)
            except HttpError as e:
                errors.append(f"„{phrase}”: {e}")
                if e.status in (None, 401, 403):
                    break
        if errors and not results:
            raise SourceError("; ".join(errors))
        return list(results.values())

    async def _search_phrase(self, phrase: str, query: SearchQuery, out: dict[str, RawOffer]) -> None:
        for page in range(1, query.max_pages + 1):
            params: dict[str, Any] = {
                "search_text": phrase, "page": page, "per_page": PER_PAGE, "order": "newest_first",
            }
            if query.price_min:
                params["price_from"] = int(query.price_min)
            if query.price_max:
                params["price_to"] = int(query.price_max)
            data = await self.http.get_json(API_URL, params=params, headers={"Accept": "application/json"})
            items = data.get("items") or []
            for item in items:
                try:
                    raw = parse_item(item)
                except (KeyError, TypeError, ValueError) as e:
                    log.warning("Vinted: pominięto przedmiot %s: %s", item.get("id"), e)
                    continue
                if raw:
                    out.setdefault(raw.source_id, raw)
            pagination = data.get("pagination") or {}
            log.info("Vinted „%s” strona %d: %d przedmiotów", phrase, page, len(items))
            if not items or page >= int(pagination.get("total_pages") or page):
                break
