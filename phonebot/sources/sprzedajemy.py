"""Adapter Sprzedajemy.pl.

Sprzedajemy.pl nie ma publicznego API. Strona wyników wyszukiwania zawiera
listę ofert w standardowym formacie JSON-LD (``ItemList`` z produktami: tytuł,
cena, link, zdjęcie), którą czyta uniwersalny ekstraktor (``extract.py``).
Miasto jest częścią adresu ogłoszenia, np.
``/iphone-13-kielce-4-0010c9-nr69433240`` → Kielce.

Kategoria: wyszukiwanie odbywa się w kategorii „Apple iPhone” (ID 1390) przez jej adres
(``/elektronika/telefony-i-akcesoria/telefony-komorkowe/apple-iphone``). Parametr
``inp_category_id`` strony wyszukiwania jest ignorowany, więc adapter sprawdza ID kategorii
w odpowiedzi (``catid``) i ostrzega, gdy portal zmieni adres.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import urlsplit

from ..core.models import RawOffer
from ..core.places import find_place
from ..core.settings import Settings
from ..net.http import HttpClient
from .base import SearchQuery, SourceAdapter, SourceFormatChanged, register, search_all_phrases
from .extract import ExtractedOffer, offers_from_html

log = logging.getLogger(__name__)

BASE_URL = "https://sprzedajemy.pl"
SEARCH_URL = BASE_URL + "/wszystkie-ogloszenia"

_CATID_RE = re.compile(r'"catid":"([^"]+)"')
_ID_RE = re.compile(r"-nr(\d+)(?:$|[/?#])")
# „…-<miasto>-<kod kategorii>-<hash>-nr<id>”
_CITY_RE = re.compile(r"/(?P<slug>[a-z0-9-]+?)-\d+-[0-9a-f]{6}-nr\d+")
_CONDITIONS = {"newcondition": "new", "usedcondition": "used", "damagedcondition": "damaged",
               "nowy": "new", "używany": "used", "uszkodzony": "damaged"}


def offer_id(url: str | None, fallback: str) -> str:
    m = _ID_RE.search(url or "")
    return m.group(1) if m else fallback


def city_from_url(url: str | None) -> str | None:
    """Miasto z końcówki adresu ogłoszenia (dopasowane do listy miejscowości, gdy się da)."""
    m = _CITY_RE.search(urlsplit(url or "").path)
    if not m:
        return None
    words = m.group("slug").split("-")
    for n in (3, 2, 1):  # najpierw nazwy wielowyrazowe: „nowy-targ”, „bielsko-biala”
        if len(words) < n:
            continue
        candidate = " ".join(words[-n:])
        place = find_place(candidate)
        if place:
            return place.name
    return words[-1].capitalize() if words else None


def looks_like_no_results(html: str) -> bool:
    low = html.lower()
    return any(m in low for m in ("brak ogłoszeń", "nie znaleźliśmy", "0 ogłoszeń", "brak wyników"))


def to_raw(o: ExtractedOffer) -> RawOffer:
    params: dict[str, str] = {}
    if o.condition:
        params["condition"] = _CONDITIONS.get(o.condition.lower(), o.condition)
    if o.category:
        params["category"] = o.category
    return RawOffer(
        source=SprzedajemyAdapter.key,
        source_id=offer_id(o.url, o.id),
        url=o.url or BASE_URL,
        title=o.title,
        price=o.price,
        description=o.description,
        currency=o.currency,
        city=o.city or city_from_url(o.url),
        photos=o.photos,
        created_at=o.created_at,
        params=params,
    )


@register
class SprzedajemyAdapter(SourceAdapter):
    key = "sprzedajemy"
    display_name = "Sprzedajemy.pl"

    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.settings = settings

    async def search(self, query: SearchQuery) -> list[RawOffer]:
        return await search_all_phrases(query.phrases, lambda p, out: self._search_phrase(p, query, out))

    async def _search_phrase(self, phrase: str, query: SearchQuery, out: dict[str, RawOffer]) -> None:
        # Tylko pierwsza strona wyników na frazę — parametry stronicowania i filtrów cen
        # Sprzedajemy.pl nie są udokumentowane, więc ceny filtruje sama aplikacja.
        cat = self.category()
        url = f"{BASE_URL}/{cat['path']}" if cat["path"] else SEARCH_URL
        html = await self.http.get_text(url, params={"inp_text": phrase})
        if cat["id"]:
            m = _CATID_RE.search(html)
            if m and cat["id"] not in m.group(1).split():
                log.warning("Sprzedajemy.pl: strona nie jest w kategorii %s (catid %s) — sprawdź adres kategorii "
                            "w ustawieniach", cat["id"], m.group(1))
        extracted = [o for o in offers_from_html(html, BASE_URL) if o.currency == "PLN"]
        if not extracted:
            if looks_like_no_results(html):
                return
            raise SourceFormatChanged("nie znaleziono listy ofert (JSON-LD) na stronie — możliwa zmiana formatu")
        for o in extracted:
            raw = to_raw(o)
            price_min = self.price_floor(query)
            if price_min and raw.price < price_min:
                continue
            if query.price_max and raw.price > query.price_max:
                continue
            out.setdefault(raw.source_id, raw)
        log.info("Sprzedajemy.pl „%s”: %d ofert", phrase, len(extracted))
