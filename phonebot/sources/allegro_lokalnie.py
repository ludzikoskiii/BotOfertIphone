"""Adapter Allegro Lokalnie.

Publiczne REST API Allegro nie obejmuje ogłoszeń Allegro Lokalnie, więc adapter
pobiera strony wyników wyszukiwania i wyciąga oferty z danych osadzonych
w stronie (JSON / JSON-LD) — patrz ``extract.py``.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from ..core.models import RawOffer
from ..core.settings import Settings
from ..net.http import HttpClient
from .base import SearchQuery, SourceAdapter, SourceFormatChanged, register, search_all_phrases
from .extract import ExtractedOffer, offers_from_html

log = logging.getLogger(__name__)

BASE_URL = "https://allegrolokalnie.pl"
SEARCH_URL = BASE_URL + "/oferty/q/{phrase}"

_CONDITIONS = {
    "new": "new", "nowy": "new", "nowe": "new", "newcondition": "new",
    "used": "used", "uzywany": "used", "używany": "used", "używane": "used", "usedcondition": "used",
    "damaged": "damaged", "uszkodzony": "damaged", "uszkodzone": "damaged", "damagedcondition": "damaged",
}


def looks_like_no_results(html: str) -> bool:
    """Strona poprawnie się wczytała, ale fraza nie ma wyników (a nie zmiana formatu)."""
    low = html.lower()
    return any(m in low for m in ("brak wyników", "nie znaleźliśmy", "nie znalezlismy", "0 ogłoszeń"))


def to_raw(o: ExtractedOffer) -> RawOffer:
    params: dict[str, str] = {}
    if o.condition:
        params["condition"] = _CONDITIONS.get(o.condition.lower(), o.condition)
    if o.category:
        params["category"] = o.category
    raw = o.raw
    delivery = raw.get("delivery") or raw.get("shipping") or raw.get("deliveryAvailable")
    shipping = None
    if isinstance(delivery, bool):
        shipping = delivery
    elif isinstance(delivery, dict):
        shipping = bool(delivery.get("available", delivery.get("enabled", True)))
    return RawOffer(
        source=AllegroLokalnieAdapter.key,
        source_id=o.id,
        url=o.url or f"{BASE_URL}/oferta/{o.id}",
        title=o.title,
        price=o.price,
        description=o.description,
        currency=o.currency,
        city=o.city,
        photos=o.photos,
        created_at=o.created_at,
        shipping_available=shipping,
        params=params,
    )


@register
class AllegroLokalnieAdapter(SourceAdapter):
    key = "allegro_lokalnie"
    display_name = "Allegro Lokalnie"

    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.settings = settings

    async def search(self, query: SearchQuery) -> list[RawOffer]:
        return await search_all_phrases(query.phrases, lambda p, out: self._search_phrase(p, query, out))

    async def _search_phrase(self, phrase: str, query: SearchQuery, out: dict[str, RawOffer]) -> None:
        url = SEARCH_URL.format(phrase=quote(phrase))
        for page in range(1, query.max_pages + 1):
            params: dict[str, str | int] = {"sort": "startingTime-desc"}
            if page > 1:
                params["page"] = page
            if query.price_min:
                params["price_from"] = int(query.price_min)
            if query.price_max:
                params["price_to"] = int(query.price_max)
            html = await self.http.get_text(url, params=params)
            extracted = [o for o in offers_from_html(html, BASE_URL) if o.currency == "PLN"]
            if page == 1 and not extracted:
                if looks_like_no_results(html):
                    return
                raise SourceFormatChanged("nie znaleziono danych ofert na stronie — możliwa zmiana formatu serwisu")
            new = 0
            for o in extracted:
                if o.id not in out:
                    out[o.id] = to_raw(o)
                    new += 1
            log.info("Allegro Lokalnie „%s” strona %d: %d ofert (%d nowych)", phrase, page, len(extracted), new)
            if new == 0:
                break
