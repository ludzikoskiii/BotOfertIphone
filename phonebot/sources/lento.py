"""Adapter Lento.pl (strony kategorii, bez API).

Sprawdzone we wrześniu 2026 (``scripts/probe_portals.py``): Lento nie ma publicznego API, a robots.txt
zabrania automatycznego przeszukiwania (``/szukaj.html``). Dlatego adapter czyta tylko **kategorię
telefonów Apple** (Elektronika → Telefony i akcesoria → Telefony komórkowe → Apple), strona po stronie
(``?page=N``), z tym samym limitem zapytań co inne portale. Kategoria zawiera telefony — akcesoria
i ogłoszenia „kupię” odrzuca wspólny filtr programu.

Struktura listy: wiersz ``div.tablelist-tr[data-id]`` z linkiem ``a.title-list-item`` (tytuł, adres
``…,<id>.html``), fragmentem opisu, miastem (``a.licon-pin-f``) i miniaturą. Cena — tekst „… zł” w wierszu.
"""
from __future__ import annotations

import logging
import re

from selectolax.parser import HTMLParser, Node

from ..core.models import RawOffer
from ..core.settings import Settings
from ..net.http import HttpClient, HttpError
from .base import SearchQuery, SourceAdapter, SourceFormatChanged, register, source_error_from_http
from .extract import parse_price

log = logging.getLogger(__name__)

BASE_URL = "https://www.lento.pl"
CATEGORY_URL = BASE_URL + "/telefony-komorkowe/telefony-i-akcesoria/elektronika/apple.html"
_ID_RE = re.compile(r",(\d+)\.html")
_PRICE_RE = re.compile(r"(\d[\d\s ]{0,9}(?:[,.]\d{1,2})?)\s*zł", re.I)
_SUBDOMAIN_RE = re.compile(r"https?://([a-z0-9-]+)\.lento\.pl/")


def _city(row: Node, url: str) -> str | None:
    pin = row.css_first("a.licon-pin-f")
    if pin is not None and pin.text(strip=True):
        return pin.text(strip=True)
    m = _SUBDOMAIN_RE.match(url)
    if m and m.group(1) != "www":
        return m.group(1).replace("-", " ").title()
    return None


def parse_list(html: str) -> list[RawOffer]:
    """Oferty ze strony kategorii (bez sieci — testowane na zapisanej stronie)."""
    tree = HTMLParser(html)
    out: list[RawOffer] = []
    for row in tree.css("div.tablelist-tr[data-id]"):
        link = row.css_first("a.title-list-item")
        if link is None:
            continue
        url = link.attributes.get("href") or ""
        m = _ID_RE.search(url)
        oid = row.attributes.get("data-id") or (m.group(1) if m else None)
        title = link.text(strip=True)
        text = row.text(separator=" ", strip=True)
        pm = _PRICE_RE.search(text.replace(title, "", 1))
        price, _ = parse_price(pm.group(0)) if pm else (None, None)
        if not oid or not title or price is None:
            continue
        snippet = row.css_first("p")
        photos = []
        for img in row.css("img"):
            src = img.attributes.get("src") or ""
            if src.startswith("http") and src not in photos:
                photos.append(src.replace("/thumbnail/", "/original/"))
        out.append(RawOffer(
            source=LentoAdapter.key, source_id=str(oid), url=url, title=title, price=price,
            description=snippet.text(strip=True) if snippet is not None else "", city=_city(row, url),
            photos=photos[:1], params={"category": "apple"}))
    return out


@register
class LentoAdapter(SourceAdapter):
    key = "lento"
    display_name = "Lento"

    def __init__(self, http: HttpClient, settings: Settings):
        self.http = http
        self.settings = settings

    async def search(self, query: SearchQuery) -> list[RawOffer]:
        """Frazy nie są używane (wyszukiwarka Lento jest wyłączona w robots.txt) — tylko kategoria Apple."""
        out: dict[str, RawOffer] = {}
        for page in range(1, max(1, query.max_pages) + 1):
            params = {"page": page} if page > 1 else None
            try:
                html = await self.http.get_text(CATEGORY_URL, params=params)
            except HttpError as e:
                if out:  # dalsze strony nie są konieczne
                    log.info("Lento: strona %d niedostępna (%s) — kończę", page, e)
                    break
                raise source_error_from_http(e, "Lento") from e
            offers = parse_list(html)
            if page == 1 and not offers and "tablelist-tr" not in html:
                raise SourceFormatChanged("Lento: strona kategorii nie zawiera listy ogłoszeń — zmiana wyglądu strony")
            floor = self.price_floor(query) or 0
            for o in offers:
                if o.price >= floor and (not query.price_max or o.price <= query.price_max):
                    out.setdefault(o.source_id, o)
            log.info("Lento strona %d: %d ogłoszeń", page, len(offers))
            if not offers or f"page={page + 1}" not in html:
                break
        return list(out.values())
