"""Adapter Vinted (tryb „best effort").

Vinted nie ma publicznego API. We wrześniu 2026 Vinted wyłączył stary endpoint
``www.vinted.pl/api/v2/catalog/items`` (odpowiada 404) i przeniósł katalog na
``api.vinted.pl/svc-catalogue/items``. Nowy endpoint wymaga anonimowego tokenu
sesji: ciasteczko ``access_token_web``, które strona ``www.vinted.pl`` wydaje
każdemu odwiedzającemu, wysyłane jako ``Authorization: Bearer``.

Odporność na zmiany: adapter próbuje kolejno znanych endpointów, szuka listy
ofert w dowolnym miejscu odpowiedzi i czyta pola na kilka sposobów (stary i
nowy format). Oferty w innej walucie niż PLN są pomijane (waluta zależy od
kraju, z którego łączy się komputer).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

from ..core.models import RawOffer
from ..core.settings import Settings
from ..net.http import HttpClient, HttpError
from .base import (
    SearchQuery,
    SourceAdapter,
    SourceBlocked,
    SourceFormatChanged,
    register,
    search_all_phrases,
    source_error_from_http,
)
from .extract import parse_price, walk

log = logging.getLogger(__name__)

BASE_URL = "https://www.vinted.pl"
SESSION_URL = BASE_URL + "/catalog"
# Kolejność prób: aktualny endpoint, potem stary (gdyby Vinted go przywrócił).
ENDPOINTS = ("https://api.vinted.pl/svc-catalogue/items", BASE_URL + "/api/v2/catalog/items")
PER_PAGE = 96
TOKEN_COOKIE = "access_token_web"

_CONDITIONS = {
    "nowy z metką": "new", "nowy bez metki": "new", "new with tags": "new", "new without tags": "new",
    "bardzo dobry": "used", "dobry": "used", "zadowalający": "used", "very good": "used", "good": "used",
    "satisfactory": "used",
}


def _condition_text(item: dict[str, Any]) -> str | None:
    candidates = [item.get("status")]
    box = item.get("item_box")
    if isinstance(box, dict):
        candidates += [box.get("second_line"), box.get("status")]
    for c in candidates:
        if isinstance(c, str) and c.strip().lower() in _CONDITIONS:
            return c.strip().lower()
    return None


def _photos(item: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    for photo in [item.get("photo"), *(item.get("photos") or [])]:
        if not isinstance(photo, dict):
            continue
        url = photo.get("url") or photo.get("full_size_url")
        thumbs = [t for t in photo.get("thumbnails") or [] if isinstance(t, dict)]
        mid = next((t.get("url") for t in thumbs if t.get("type") == "thumb310x430"), None)
        url = mid or url
        if url and url not in urls:
            urls.append(url)
    return urls


def parse_item(item: dict[str, Any]) -> RawOffer | None:
    """Pojedynczy przedmiot z katalogu (stary lub nowy format) → ``RawOffer``.

    Zwraca ``None`` dla przedmiotów bez ceny. Waluta jest zachowana w ``currency``.
    """
    if "id" not in item or not item.get("title"):
        return None
    price, currency = parse_price(item.get("price"))
    if isinstance(item.get("price"), dict):
        currency = item["price"].get("currency_code") or currency
    currency = (currency or item.get("currency") or "PLN").upper()
    if price is None:
        return None
    params: dict[str, str] = {}
    cond = _condition_text(item)
    if cond:
        params["condition"] = _CONDITIONS[cond]
    fee, _ = parse_price(item.get("service_fee"))
    if fee is None:
        total, _ = parse_price(item.get("total_item_price"))
        fee = total - price if total and total > price else None
    if fee:
        params["buyer_fee"] = f"{fee:.2f}"
    created = None
    photo = item.get("photo") if isinstance(item.get("photo"), dict) else {}
    ts = (photo.get("high_resolution") or {}).get("timestamp")
    if isinstance(ts, (int, float)):
        created = datetime.fromtimestamp(ts).astimezone()
    url = item.get("url") or item.get("path") or f"/items/{item['id']}"
    return RawOffer(
        source=VintedAdapter.key,
        source_id=str(item["id"]),
        url=urljoin(BASE_URL, url),
        title=str(item.get("title") or "").strip(),
        price=price,
        description=str(item.get("description") or ""),
        currency=currency,
        photos=_photos(item),
        created_at=created,
        shipping_available=True,
        params=params,
    )


def find_items(data: Any) -> list[dict[str, Any]]:
    """Lista przedmiotów z odpowiedzi — pod kluczem ``items`` albo gdziekolwiek w strukturze."""
    if isinstance(data, dict) and isinstance(data.get("items"), list):
        return [x for x in data["items"] if isinstance(x, dict)]
    for node in walk(data):
        for value in node.values():
            if (isinstance(value, list) and value and isinstance(value[0], dict)
                    and "id" in value[0] and "title" in value[0]):
                return [x for x in value if isinstance(x, dict)]
    return []


@register
class VintedAdapter(SourceAdapter):
    key = "vinted"
    display_name = "Vinted"

    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.settings = settings
        self._token: str | None = None
        self._endpoint: str | None = None
        self.stats: dict[str, int] = {}

    async def _open_session(self, force: bool = False) -> None:
        if self._token and not force:
            return
        if force:
            self.http.cookies.clear()
        try:
            response = await self.http.request("HEAD", SESSION_URL)
            if response.status_code >= 400 or not self.http.cookies.get(TOKEN_COOKIE):
                response = await self.http.request("GET", BASE_URL + "/")
        except HttpError as e:
            raise source_error_from_http(e, "sesja Vinted") from e
        if response.status_code in (401, 403, 429):
            raise SourceBlocked(f"Vinted odrzucił otwarcie sesji (HTTP {response.status_code}) — "
                                "portal blokuje automatyczne pobieranie")
        self._token = self.http.cookies.get(TOKEN_COOKIE)
        if not self._token:
            raise SourceFormatChanged("Vinted nie wydał tokenu sesji (brak ciasteczka access_token_web) — "
                                      "zmienił się sposób logowania anonimowego")

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "Authorization": f"Bearer {self._token}",
                "Referer": SESSION_URL, "Origin": BASE_URL}

    async def search(self, query: SearchQuery) -> list[RawOffer]:
        self.stats = {"items_seen": 0, "foreign_currency": 0}
        await self._open_session()
        return await search_all_phrases(query.phrases, lambda p, out: self._search_phrase(p, query, out))

    async def _fetch(self, params: dict[str, Any]) -> Any:
        """Pobiera stronę katalogu, próbując kolejnych endpointów i odświeżając token przy 401."""
        endpoints = [self._endpoint] if self._endpoint else list(ENDPOINTS)
        last_error: HttpError | None = None
        for url in endpoints:
            for attempt in range(2):
                try:
                    data = await self.http.get_json(url, params=params, headers=self._headers(), use_cache=False)
                    self._endpoint = url
                    return data
                except HttpError as e:
                    last_error = e
                    if e.status == 401 and attempt == 0:  # token wygasł — nowa sesja i jeszcze raz
                        await self._open_session(force=True)
                        continue
                    break
            if last_error is not None and last_error.status not in (404, 410):
                raise last_error
        assert last_error is not None
        raise last_error

    async def _search_phrase(self, phrase: str, query: SearchQuery, out: dict[str, RawOffer]) -> None:
        for page in range(1, query.max_pages + 1):
            params: dict[str, Any] = {
                "search_text": phrase, "page": page, "per_page": PER_PAGE, "order": "newest_first",
            }
            # puste filtry trzeba pominąć — nowy endpoint odpowiada na nie błędem 400
            if query.price_min:
                params["price_from"] = int(query.price_min)
            if query.price_max:
                params["price_to"] = int(query.price_max)
            data = await self._fetch(params)
            items = find_items(data)
            if page == 1 and not items and not _looks_like_empty_result(data):
                raise SourceFormatChanged("odpowiedź Vinted nie zawiera listy przedmiotów — zmiana formatu API")
            for item in items:
                try:
                    raw = parse_item(item)
                except (KeyError, TypeError, ValueError) as e:
                    log.warning("Vinted: pominięto przedmiot %s: %s", item.get("id"), e)
                    continue
                if raw is None:
                    continue
                self.stats["items_seen"] += 1
                if raw.currency != "PLN":
                    self.stats["foreign_currency"] += 1
                    continue
                out.setdefault(raw.source_id, raw)
            pagination = data.get("pagination") if isinstance(data, dict) else None
            total_pages = int((pagination or {}).get("total_pages") or page)
            log.info("Vinted „%s” strona %d: %d przedmiotów", phrase, page, len(items))
            if not items or page >= total_pages or len(items) < PER_PAGE:
                break


def _looks_like_empty_result(data: Any) -> bool:
    """Poprawna odpowiedź bez wyników (np. brak ofert dla frazy)."""
    return isinstance(data, dict) and isinstance(data.get("items"), list) and not data["items"]
