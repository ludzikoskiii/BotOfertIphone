"""Adapter eBay — oficjalne Browse API (``api.ebay.com/buy/browse/v1``), bez scrapowania.

Wymaga darmowego konta eBay Developers (``developer.ebay.com``): App ID (Client ID) i Cert ID
(Client Secret) wpisujesz w Ustawieniach → Portale. Token aplikacji: ``client_credentials`` ze
standardowym zakresem ``https://api.ebay.com/oauth/api_scope`` (bez logowania na Twoje konto eBay).

Wyszukiwanie w kategorii „Cell Phones & Smartphones” (ID 9355) na wybranych rynkach (np. EBAY_DE,
EBAY_GB, EBAY_US) z filtrem ``deliveryCountry:PL`` — tylko oferty z wysyłką do Polski — oraz stanem
używany / na części. Koszt wysyłki do Polski podaje eBay (nagłówek ``X-EBAY-C-ENDUSERCTX``
z krajem dostawy).

Ceny przeliczane na złote kursem NBP. Dla wysyłek spoza UE doliczany jest szacunek VAT importowego,
cła i opłaty za odprawę (Ustawienia → Portale). Każda oferta dostaje flagę „Zakup na odległość”
(korekta oceny do ustawienia), a iPhone 14 i nowsze z USA — „tylko eSIM” (niższa cena odsprzedaży).
"""
from __future__ import annotations

import base64
import logging
from typing import Any

from ..core.currency import NBP_URL, Rates, fallback_rates, import_costs, parse_nbp
from ..core.models import RawOffer
from ..core.settings import Settings
from ..net.http import HttpClient, HttpError
from .allegro_api import SourceNeedsKeys
from .base import (
    SearchQuery,
    SourceAdapter,
    SourceError,
    SourceFormatChanged,
    register,
    search_all_phrases,
    source_error_from_http,
)
from .extract import parse_price

log = logging.getLogger(__name__)

API = "https://api.ebay.com/buy/browse/v1/item_summary/search"
TOKEN_URL = "https://api.ebay.com/identity/v1/oauth2/token"
SCOPE = "https://api.ebay.com/oauth/api_scope"
CATEGORY = "9355"  # Cell Phones & Smartphones
MARKETS = {"EBAY_DE": "eBay Niemcy", "EBAY_GB": "eBay Wielka Brytania", "EBAY_US": "eBay USA",
           "EBAY_FR": "eBay Francja", "EBAY_IT": "eBay Włochy", "EBAY_ES": "eBay Hiszpania",
           "EBAY_NL": "eBay Holandia", "EBAY_AT": "eBay Austria", "EBAY_PL": "eBay Polska"}
# stan: 3000 używany, 7000 na części/uszkodzony, 2010–2030/2500 odnowiony
CONDITIONS = "conditionIds:{3000|7000|2010|2020|2030|2500}"
PAGE = 100

_RATES: Rates | None = None


def item_to_offer(item: dict[str, Any], market: str, rates: Rates, s: Settings) -> RawOffer | None:
    price, currency = parse_price(item.get("price"))
    if price is None or not item.get("itemId") or not item.get("title"):
        return None
    currency = (currency or "USD").upper()
    price_pln = rates.to_pln(price, currency)
    if price_pln is None:
        return None
    ship_pln = None
    for opt in item.get("shippingOptions") or []:
        cost, cur = parse_price(opt.get("shippingCost"))
        if cost is not None:
            ship_pln = rates.to_pln(cost, (cur or currency).upper())
            break
    location = item.get("itemLocation") or {}
    country = (location.get("country") or "").upper() or None
    imp = import_costs(price_pln, ship_pln or 0.0, country, vat_pct=s.ebay_vat_pct, duty_pct=s.ebay_duty_pct,
                       clearance_fee=s.ebay_clearance_fee)
    seller = item.get("seller") or {}
    params: dict[str, str] = {"market": market, "item_country": country or "", "original_price": f"{price:.2f}",
                              "original_currency": currency, "fx_date": rates.date, "remote_purchase": "1"}
    if ship_pln is not None:
        params["shipping_cost"] = f"{ship_pln:.2f}"
    if imp.total:
        params["import_cost"] = f"{imp.total:.2f}"
        params["import_detail"] = f"VAT {imp.vat:.0f} zł, cło {imp.duty:.0f} zł, odprawa {imp.clearance_fee:.0f} zł"
    if seller.get("username"):
        params["seller"] = str(seller["username"])
        params["seller_id"] = str(seller["username"])
    if seller.get("feedbackScore") is not None:
        params["seller_feedback"] = str(seller.get("feedbackScore"))
    if seller.get("feedbackPercentage") is not None:
        params["seller_positive_pct"] = str(seller.get("feedbackPercentage"))
    if item.get("condition"):
        params["condition_text"] = str(item["condition"])
    image = (item.get("image") or {}).get("imageUrl")
    city = location.get("city") or (f"({country})" if country else None)
    return RawOffer(source=EbayAdapter.key, source_id=str(item["itemId"]), url=str(item.get("itemWebUrl") or ""),
                    title=str(item["title"]).strip(), price=price_pln, currency="PLN",
                    city=f"{city}, {country}" if location.get("city") and country else city,
                    photos=[image] if image else [], shipping_available=True, params=params)


@register
class EbayAdapter(SourceAdapter):
    key = "ebay"
    display_name = "eBay"
    international = True
    default_enabled = False
    requires_keys = True

    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.settings = settings
        self._token: str | None = None

    @staticmethod
    def configured(settings: Settings) -> bool:
        return bool(settings.ebay_client_id and settings.ebay_client_secret)

    async def _auth(self) -> None:
        if self._token:
            return
        s = self.settings
        if not self.configured(s):
            raise SourceNeedsKeys("eBay: brak App ID / Cert ID — Ustawienia → Portale")
        basic = base64.b64encode(f"{s.ebay_client_id}:{s.ebay_client_secret}".encode()).decode()
        try:
            r = await self.http.request("POST", TOKEN_URL, data={"grant_type": "client_credentials", "scope": SCOPE},
                                        headers={"Authorization": f"Basic {basic}",
                                                 "Content-Type": "application/x-www-form-urlencoded"})
        except HttpError as e:
            raise source_error_from_http(e, "eBay (token)") from e
        if r.status_code in (400, 401):
            raise SourceNeedsKeys("eBay odrzucił klucze aplikacji (sprawdź App ID i Cert ID — klucze „Production”)")
        if r.status_code != 200:
            raise SourceError(f"eBay (token): HTTP {r.status_code}")
        self._token = r.json().get("access_token")

    async def _rates(self) -> Rates:
        """Kursy NBP raz na uruchomienie programu (dziennie się zmieniają) — bez NBP: ostatnie albo awaryjne."""
        global _RATES
        if _RATES is None or _RATES.fallback:
            try:
                _RATES = parse_nbp(await self.http.get_json(NBP_URL))
            except (HttpError, KeyError, IndexError, TypeError, ValueError) as e:
                log.warning("NBP: kursy niedostępne (%s) — kursy przybliżone", e)
                _RATES = _RATES or fallback_rates()
        return _RATES

    async def search(self, query: SearchQuery) -> list[RawOffer]:
        await self._auth()
        rates = await self._rates()
        markets = [m for m in self.settings.ebay_markets if m in MARKETS] or ["EBAY_DE"]
        out: list[RawOffer] = []
        errors: list[SourceError] = []
        for market in markets:  # rynki niezależnie — błąd jednego nie blokuje pozostałych
            try:
                out += await search_all_phrases(
                    query.phrases, lambda p, acc, m=market: self._search_phrase(p, m, rates, query, acc))
            except SourceError as e:
                errors.append(e)
        if errors and not out:
            raise errors[0]
        return list({o.source_id: o for o in out}.values())

    async def _search_phrase(self, phrase: str, market: str, rates: Rates, query: SearchQuery,
                             out: dict[str, RawOffer]) -> None:
        headers = {"Authorization": f"Bearer {self._token}", "X-EBAY-C-MARKETPLACE-ID": market,
                   "X-EBAY-C-ENDUSERCTX": "contextualLocation=country%3DPL"}
        for page in range(query.max_pages):
            params = {"q": phrase, "category_ids": CATEGORY, "limit": PAGE, "offset": page * PAGE,
                      "filter": f"deliveryCountry:PL,buyingOptions:{{FIXED_PRICE}},{CONDITIONS}",
                      "sort": "newlyListed"}
            data = await self.http.get_json(API, params=params, headers=headers, use_cache=False)
            if not isinstance(data, dict):
                raise SourceFormatChanged("eBay: nieoczekiwana odpowiedź")
            items = data.get("itemSummaries") or []
            for item in items:
                raw = item_to_offer(item, market, rates, self.settings)
                if raw is None:
                    continue
                floor = self.price_floor(query) or 0
                if raw.price >= floor and (not query.price_max or raw.price <= query.price_max):
                    out.setdefault(raw.source_id, raw)
            if len(items) < PAGE or (page + 1) * PAGE >= int(data.get("total") or 0):
                break
