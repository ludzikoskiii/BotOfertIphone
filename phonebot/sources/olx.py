"""Adapter OLX.pl.

OLX nie ma publicznego API wyszukiwania dla kupujących. Adapter korzysta z
endpointu JSON ``/api/v1/offers/``, z którego ładuje dane sama strona OLX.
Zapytania idą przez ``HttpClient`` (limit zapytań, ponawianie, cache).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from selectolax.parser import HTMLParser

from ..core.models import RawOffer
from ..core.settings import Settings
from ..net.http import HttpClient, HttpError
from .base import SearchQuery, SourceAdapter, SourceError, register

log = logging.getLogger(__name__)

API_URL = "https://www.olx.pl/api/v1/offers/"
PAGE_SIZE = 40
PHOTO_SIZE = "400x300"

_PHOTO_SIZE_RE = re.compile(r"\{width\}x\{height\}|s=\d+x\d+")


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    if "<" not in html:
        return html.strip()
    text = HTMLParser(html.replace("<br />", "\n").replace("<br>", "\n")).text(separator=" ")
    return re.sub(r"[ \t]+", " ", text).strip()


def _photo_url(photo: dict[str, Any]) -> str | None:
    link = photo.get("link")
    if not link:
        return None
    link = link.replace("{width}x{height}", PHOTO_SIZE)
    return _PHOTO_SIZE_RE.sub(f"s={PHOTO_SIZE}", link) if "s=" in link else link


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def parse_offer(item: dict[str, Any]) -> RawOffer | None:
    """Zamienia pojedynczy element ``data[]`` z API OLX na ``RawOffer``.

    Zwraca ``None`` dla ogłoszeń bez ceny w złotówkach (zamiana, „za darmo").
    """
    price: float | None = None
    negotiable: bool | None = None
    currency = "PLN"
    params: dict[str, str] = {}
    for p in item.get("params") or []:
        key = str(p.get("key") or "").lower()
        name = str(p.get("name") or "").lower()
        value = p.get("value") or {}
        if key == "price" or p.get("type") == "price":
            if isinstance(value, dict) and value.get("value") is not None:
                price = float(value["value"])
                negotiable = value.get("negotiable")
                currency = value.get("currency") or "PLN"
            continue
        label = value.get("label") if isinstance(value, dict) else str(value)
        vkey = value.get("key") if isinstance(value, dict) else None
        if key == "state" or name == "stan":
            params["condition"] = vkey or label or ""
        elif "model" in key or "model" in name:
            params["model"] = label or ""
        elif "memory" in key or "pamie" in name:
            params["storage"] = label or ""
        elif label:
            params[key or name] = label

    if price is None or currency != "PLN":
        return None

    location = item.get("location") or {}
    geo = item.get("map") or {}
    delivery = (item.get("delivery") or {}).get("rock") or {}
    photos = [u for u in (_photo_url(ph) for ph in item.get("photos") or []) if u]

    return RawOffer(
        source=OlxAdapter.key,
        source_id=str(item["id"]),
        url=item.get("url") or f"https://www.olx.pl/oferta/{item['id']}",
        title=(item.get("title") or "").strip(),
        price=price,
        description=html_to_text(item.get("description")),
        currency=currency,
        city=((location.get("city") or {}).get("name")),
        region=((location.get("region") or {}).get("name")),
        lat=geo.get("lat"),
        lon=geo.get("lon"),
        photos=photos,
        created_at=_parse_dt(item.get("created_time")),
        shipping_available=delivery.get("active") if "active" in delivery else None,
        negotiable=negotiable,
        params=params,
    )


def parse_page(payload: dict[str, Any]) -> tuple[list[RawOffer], str | None]:
    """Oferty z jednej strony wyników oraz adres następnej strony (lub ``None``)."""
    offers = []
    for item in payload.get("data") or []:
        try:
            offer = parse_offer(item)
        except (KeyError, TypeError, ValueError) as e:
            log.warning("OLX: pominięto nieczytelną ofertę %s: %s", item.get("id"), e)
            continue
        if offer:
            offers.append(offer)
    next_href = (((payload.get("links") or {}).get("next")) or {}).get("href")
    return offers, next_href


@register
class OlxAdapter(SourceAdapter):
    key = "olx"
    display_name = "OLX"

    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.settings = settings

    def _params(self, phrase: str, query: SearchQuery) -> dict[str, Any]:
        params: dict[str, Any] = {
            "offset": 0,
            "limit": PAGE_SIZE,
            "query": phrase,
            "sort_by": "created_at:desc",
        }
        if self.settings.olx_category_id:
            params["category_id"] = self.settings.olx_category_id
        if query.price_min:
            params["filter_float_price:from"] = int(query.price_min)
        if query.price_max:
            params["filter_float_price:to"] = int(query.price_max)
        return params

    async def search(self, query: SearchQuery) -> list[RawOffer]:
        results: dict[str, RawOffer] = {}
        errors: list[str] = []
        for phrase in query.phrases:
            try:
                await self._search_phrase(phrase, query, results)
            except HttpError as e:
                errors.append(f"„{phrase}”: {e}")
                if e.status in (None, 403):
                    break  # host nieosiągalny lub blokada — nie męczymy go kolejnymi frazami
        if errors and not results:
            raise SourceError("; ".join(errors))
        if errors:
            log.warning("OLX: część fraz nie powiodła się: %s", "; ".join(errors))
        return list(results.values())

    async def _search_phrase(self, phrase: str, query: SearchQuery, out: dict[str, RawOffer]) -> None:
        url: str | None = API_URL
        params: dict[str, Any] | None = self._params(phrase, query)
        for page in range(query.max_pages):
            payload = await self.http.get_json(url, params=params)  # type: ignore[arg-type]
            offers, next_href = parse_page(payload)
            for o in offers:
                out.setdefault(o.source_id, o)
            log.info("OLX „%s” strona %d: %d ofert", phrase, page + 1, len(offers))
            if not next_href or not offers:
                break
            url, params = next_href, None

