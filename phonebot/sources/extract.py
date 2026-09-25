"""Odporne wyciąganie ofert z danych osadzonych w stronach (JSON / JSON-LD).

Portale bez publicznego API osadzają dane ofert w stronie: ``__NEXT_DATA__``,
``application/json``, ``application/ld+json``. Zamiast polegać na jednej
sztywnej ścieżce (która psuje się przy każdej zmianie serwisu), przeszukujemy
cały JSON i rozpoznajemy obiekty „wyglądające jak oferta": mają tytuł, cenę
i identyfikator lub adres. Dzięki temu drobne zmiany struktury nie psują adaptera.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from selectolax.parser import HTMLParser

_TITLE_KEYS = ("title", "name")
_ID_KEYS = ("id", "offerId", "offer_id", "uuid", "itemId", "sku")
_URL_KEYS = ("url", "link", "href", "path", "slug", "offerUrl")
_PRICE_KEYS = ("price", "priceAmount", "amount", "sellingPrice", "totalPrice", "total_item_price", "offers")
_DESC_KEYS = ("description", "desc", "shortDescription")
_CITY_KEYS = ("city", "cityName", "locationName", "town")
_PHOTO_KEYS = ("photos", "images", "photo", "image", "thumbnails", "pictures", "mainImage", "thumbnail")
_COND_KEYS = ("condition", "state", "status", "itemCondition")
_DATE_KEYS = ("createdAt", "created_at", "publishedAt", "startTime", "created", "created_time", "datePublished")
_PRICE_RE = re.compile(r"(\d[\d\s .]*(?:,\d{1,2})?)")


@dataclass
class ExtractedOffer:
    id: str
    title: str
    price: float
    currency: str = "PLN"
    url: str | None = None
    description: str = ""
    city: str | None = None
    photos: list[str] = field(default_factory=list)
    condition: str | None = None
    created_at: datetime | None = None
    raw: dict[str, Any] = field(default_factory=dict)


def parse_price(value: Any) -> tuple[float | None, str | None]:
    """Cena z liczby, tekstu („1 499,99 zł") lub obiektu ({amount, currency})."""
    if isinstance(value, bool) or value is None:
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, str):
        m = _PRICE_RE.search(value)
        if not m:
            return None, None
        num = m.group(1).replace(" ", "").replace(" ", "")
        if "," in num:
            num = num.replace(".", "").replace(",", ".")
        elif num.count(".") == 1 and len(num.split(".")[1]) == 3:
            num = num.replace(".", "")  # 1.500 = tysiąc pięćset
        try:
            currency = "PLN" if ("zł" in value or "pln" in value.lower()) else None
            return float(num), currency
        except ValueError:
            return None, None
    if isinstance(value, dict):
        currency = value.get("currency") or value.get("currencyCode") or value.get("currency_code") \
            or value.get("priceCurrency")
        for key in ("amount", "value", "price", "raw", "gross", "lowPrice"):
            if key in value:
                price, cur = parse_price(value[key])
                if price is not None:
                    return price, currency or cur
    if isinstance(value, list) and value:
        return parse_price(value[0])
    return None, None


def _first(d: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def _photo_urls(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.startswith("http") else []
    if isinstance(value, dict):
        for key in ("url", "src", "original", "full_size_url", "link", "large", "medium", "thumbnail", "contentUrl"):
            if isinstance(value.get(key), str) and value[key].startswith("http"):
                return [value[key]]
        return []
    if isinstance(value, list):
        out: list[str] = []
        for v in value:
            out += _photo_urls(v)[:1]
        return out
    return []


def _city(d: dict[str, Any]) -> str | None:
    loc = d.get("location") or d.get("address")
    if isinstance(loc, str):
        return loc.split(",")[0].strip() or None
    if isinstance(loc, dict):
        c = _first(loc, _CITY_KEYS + ("name", "addressLocality"))
        if isinstance(c, dict):
            c = c.get("name")
        if isinstance(c, str):
            return c
    c = _first(d, _CITY_KEYS)
    return c if isinstance(c, str) else None


def _date(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and value > 1e9:
        return datetime.fromtimestamp(value / 1000 if value > 1e12 else value).astimezone()
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _condition(value: Any) -> str | None:
    if isinstance(value, str):
        return value.rsplit("/", 1)[-1]  # schema.org/UsedCondition → UsedCondition
    if isinstance(value, dict):
        v = _first(value, ("label", "name", "title", "value", "key"))
        return v if isinstance(v, str) else None
    return None


def offer_from_dict(d: dict[str, Any], base_url: str) -> ExtractedOffer | None:
    title = _first(d, _TITLE_KEYS)
    if not isinstance(title, str) or len(title) < 3:
        return None
    price_value = _first(d, _PRICE_KEYS)
    price, currency = parse_price(price_value)
    if price is None or price <= 0:
        return None
    url = _first(d, _URL_KEYS)
    url = urljoin(base_url, url) if isinstance(url, str) else None
    ident = _first(d, _ID_KEYS)
    if ident is None and url:
        ident = url.rstrip("/").rsplit("/", 1)[-1]
    if ident is None or isinstance(ident, (dict, list)):
        return None
    desc = _first(d, _DESC_KEYS)
    condition = _condition(_first(d, _COND_KEYS))
    if condition is None and isinstance(d.get("offers"), dict):  # JSON-LD: Product.offers.itemCondition
        condition = _condition(_first(d["offers"], _COND_KEYS))
    return ExtractedOffer(
        id=str(ident),
        title=title.strip(),
        price=price,
        currency=(currency or "PLN").upper(),
        url=url,
        description=desc if isinstance(desc, str) else "",
        city=_city(d),
        photos=_photo_urls(_first(d, _PHOTO_KEYS)),
        condition=condition,
        created_at=_date(_first(d, _DATE_KEYS)),
        raw=d,
    )


def walk(node: Any) -> Iterator[dict[str, Any]]:
    stack = [node]
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            yield cur
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)


def offers_from_json(data: Any, base_url: str) -> list[ExtractedOffer]:
    found: dict[str, ExtractedOffer] = {}
    for d in walk(data):
        offer = offer_from_dict(d, base_url)
        if offer and offer.id not in found:
            found[offer.id] = offer
    return list(found.values())


def embedded_json(html: str) -> list[Any]:
    """Wszystkie bloki JSON osadzone w stronie (NEXT_DATA, application/json, ld+json)."""
    tree = HTMLParser(html)
    blocks: list[Any] = []
    for node in tree.css("script"):
        typ = (node.attributes.get("type") or "").lower()
        if typ not in ("application/json", "application/ld+json") and node.attributes.get("id") != "__NEXT_DATA__":
            continue
        text = node.text(strip=True)
        if not text:
            continue
        try:
            blocks.append(json.loads(text))
        except json.JSONDecodeError:
            continue
    return blocks


def offers_from_html(html: str, base_url: str) -> list[ExtractedOffer]:
    found: dict[str, ExtractedOffer] = {}
    for block in embedded_json(html):
        for offer in offers_from_json(block, base_url):
            found.setdefault(offer.id, offer)
    return list(found.values())
