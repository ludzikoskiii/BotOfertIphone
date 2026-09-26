"""Adapter Allegro — oficjalne REST API (``api.allegro.pl``), bez scrapowania.

Wymaga własnej, darmowej aplikacji w Allegro Developer (``apps.developer.allegro.pl``): Client ID
i Client Secret wpisujesz w Ustawieniach → Portale (zapisywane zaszyfrowane). Token pobierany jest
przepływem ``client_credentials`` (bez logowania na Twoje konto — konto nie może zostać zablokowane
przez program). Wyszukiwanie: ``GET /offers/listing`` w kategorii „Smartfony i telefony komórkowe”
(ID 165, do zmiany w ustawieniach) z filtrem stanu „Używany”/„Uszkodzony” — identyfikatory tych wartości
adapter odczytuje z ``GET /sale/categories/{id}/parameters`` (nie są wpisane na sztywno).

Uwaga: Allegro może ograniczać dostęp do ``/offers/listing`` dla nowych aplikacji. Wtedy portal pokazuje
status „ZABLOKOWANE” z wyjaśnieniem — program nie próbuje obchodzić tej decyzji.
"""
from __future__ import annotations

import base64
import logging
from typing import Any

import httpx

from ..core.models import RawOffer
from ..core.settings import Settings
from ..net.http import HttpClient, HttpError
from .base import (
    SearchQuery,
    SourceAdapter,
    SourceBlocked,
    SourceError,
    SourceFormatChanged,
    register,
    search_all_phrases,
    source_error_from_http,
)
from .extract import parse_price

log = logging.getLogger(__name__)

API = "https://api.allegro.pl"
TOKEN_URL = "https://allegro.pl/auth/oauth/token"
ACCEPT = "application/vnd.allegro.public.v1+json"
DEFAULT_CATEGORY = "165"  # Smartfony i telefony komórkowe
_WANTED_CONDITIONS = ("używan", "uszkodz")
PAGE = 60


class SourceNeedsKeys(SourceError):
    """Brak kluczy API w ustawieniach."""

    kind = "config"


def item_to_offer(item: dict[str, Any]) -> RawOffer | None:
    price, currency = parse_price((item.get("sellingMode") or {}).get("price"))
    if price is None or not item.get("id") or not item.get("name"):
        return None
    delivery = (item.get("delivery") or {}).get("lowestPrice")
    ship, _ = parse_price(delivery)
    seller = item.get("seller") or {}
    params: dict[str, str] = {}
    if ship is not None:
        params["shipping_cost"] = f"{ship:.2f}"
    if seller.get("id"):
        params["seller_id"] = str(seller["id"])
    if seller.get("login"):
        params["seller"] = str(seller["login"])
    if seller.get("company") is not None:
        params["seller_business"] = "1" if seller.get("company") else "0"
    images = [i.get("url") for i in item.get("images") or [] if isinstance(i, dict) and i.get("url")]
    return RawOffer(source=AllegroAdapter.key, source_id=str(item["id"]), url=f"https://allegro.pl/oferta/{item['id']}",
                    title=str(item["name"]).strip(), price=price, currency=(currency or "PLN").upper(),
                    photos=images[:3], shipping_available=True, params=params)


@register
class AllegroAdapter(SourceAdapter):
    key = "allegro"
    display_name = "Allegro"
    default_enabled = False
    requires_keys = True

    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.settings = settings
        self._token: str | None = None
        self._condition_filter: dict[str, list[str]] | None = None

    @staticmethod
    def configured(settings: Settings) -> bool:
        return bool(settings.allegro_client_id and settings.allegro_client_secret)

    async def _auth(self) -> None:
        if self._token:
            return
        s = self.settings
        if not self.configured(s):
            raise SourceNeedsKeys("Allegro: brak Client ID / Client Secret — Ustawienia → Portale")
        basic = base64.b64encode(f"{s.allegro_client_id}:{s.allegro_client_secret}".encode()).decode()
        try:
            r = await self.http.request("POST", TOKEN_URL, params={"grant_type": "client_credentials"},
                                        headers={"Authorization": f"Basic {basic}"})
        except HttpError as e:
            raise source_error_from_http(e, "Allegro (token)") from e
        if r.status_code in (400, 401):
            raise SourceNeedsKeys("Allegro odrzuciło klucze aplikacji (sprawdź Client ID i Client Secret)")
        if r.status_code != 200:
            raise SourceError(f"Allegro (token): HTTP {r.status_code}")
        self._token = r.json().get("access_token")
        if not self._token:
            raise SourceFormatChanged("Allegro: brak tokenu w odpowiedzi")

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept": ACCEPT}

    def _category_id(self) -> str:
        return self.category()["id"] or DEFAULT_CATEGORY

    async def _conditions(self) -> dict[str, list[str]]:
        """Filtr „Stan: używany / uszkodzony” — identyfikatory z API kategorii (raz na skan)."""
        if self._condition_filter is not None:
            return self._condition_filter
        self._condition_filter = {}
        try:
            data = await self.http.get_json(f"{API}/sale/categories/{self._category_id()}/parameters",
                                            headers=self._headers())
        except HttpError as e:
            log.info("Allegro: parametry kategorii niedostępne (%s) — bez filtra stanu", e)
            return self._condition_filter
        for p in data.get("parameters") or []:
            if str(p.get("name", "")).lower() != "stan":
                continue
            values = [str(v["id"]) for v in (p.get("dictionary") or [])
                      if any(w in str(v.get("value", "")).lower() for w in _WANTED_CONDITIONS)]
            if values:
                self._condition_filter = {f"parameter.{p['id']}": values}
        return self._condition_filter

    async def search(self, query: SearchQuery) -> list[RawOffer]:
        await self._auth()
        return await search_all_phrases(query.phrases, lambda p, out: self._search_phrase(p, query, out))

    async def _search_phrase(self, phrase: str, query: SearchQuery, out: dict[str, RawOffer]) -> None:
        conditions = await self._conditions()
        for page in range(query.max_pages):
            params: list[tuple[str, Any]] = [("phrase", phrase), ("category.id", self._category_id()),
                                             ("limit", PAGE), ("offset", page * PAGE), ("sort", "-startTime")]
            floor = self.price_floor(query)
            if floor:
                params.append(("price.from", int(floor)))
            if query.price_max:
                params.append(("price.to", int(query.price_max)))
            for key, values in conditions.items():
                params += [(key, v) for v in values]
            try:
                data = await self.http.get_json(f"{API}/offers/listing", params=httpx.QueryParams(params),
                                                headers=self._headers(), use_cache=False)
            except HttpError as e:
                if e.status == 403:
                    raise SourceBlocked("Allegro nie udostępnia wyszukiwania ofert (/offers/listing) tej aplikacji "
                                        "— Allegro ogranicza ten zasób; możesz złożyć wniosek o dostęp w Allegro "
                                        "Developer") from e
                raise
            items = (data.get("items") or {}) if isinstance(data, dict) else {}
            batch = [*(items.get("promoted") or []), *(items.get("regular") or [])]
            if not isinstance(items, dict):
                raise SourceFormatChanged("Allegro: odpowiedź bez listy ofert")
            for item in batch:
                raw = item_to_offer(item)
                if raw is not None and raw.currency == "PLN":
                    out.setdefault(raw.source_id, raw)
            total = int((data.get("searchMeta") or {}).get("availableCount") or 0)
            if len(batch) < PAGE or (page + 1) * PAGE >= total:
                break

