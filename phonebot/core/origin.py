"""Pochodzenie oferty: Polska czy zagranica i — jeśli wiadomo — z jakiego kraju.

Skąd program to wie (od najpewniejszego):

* **lokalizacja przedmiotu** podana przez portal (eBay: ``item_country``),
* **kraj z profilu sprzedawcy** (Vinted: sprawdzany w tle, zapamiętany w bazie — ``Offer.seller_country``),
* **flaga „Sprzedawca z zagranicy”** bez znanego kraju — zwykle tytuł w obcym języku (np. czeski / słowacki),
* eBay z rynku innego niż Polska bez lokalizacji — zagranica, kraj nieznany,
* pozostałe portale (Allegro Lokalnie, OLX, Sprzedajemy, Lento, Allegro) — tylko oferty z Polski.

Flagi państw (emoji) celowo nie są używane: Windows wyświetla je jako dwie litery.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from .language import detect_language
from .models import Offer, RedFlag

ORIGINS = {"pl": "Polska", "foreign": "Zagranica"}
FOREIGN_MARK = "🌍"

COUNTRY_NAMES = {
    "PL": "Polska", "CZ": "Czechy", "SK": "Słowacja", "DE": "Niemcy", "LT": "Litwa", "LV": "Łotwa",
    "EE": "Estonia", "AT": "Austria", "HU": "Węgry", "UA": "Ukraina", "FR": "Francja", "IT": "Włochy",
    "ES": "Hiszpania", "PT": "Portugalia", "NL": "Holandia", "BE": "Belgia", "LU": "Luksemburg",
    "IE": "Irlandia", "GB": "Wielka Brytania", "DK": "Dania", "SE": "Szwecja", "FI": "Finlandia",
    "NO": "Norwegia", "CH": "Szwajcaria", "RO": "Rumunia", "BG": "Bułgaria", "HR": "Chorwacja",
    "SI": "Słowenia", "GR": "Grecja", "CY": "Cypr", "MT": "Malta", "US": "USA", "CA": "Kanada",
    "JP": "Japonia", "CN": "Chiny", "HK": "Hongkong",
}
LANGUAGE_NAMES = {"cs/sk": "czeski / słowacki", "cs": "czeski", "sk": "słowacki", "de": "niemiecki",
                  "lt": "litewski", "lv": "łotewski", "et": "estoński", "hu": "węgierski", "en": "angielski",
                  "fr": "francuski", "it": "włoski", "es": "hiszpański", "uk": "ukraiński", "ro": "rumuński"}
_BASIS = {"item": "lokalizacja przedmiotu podana przez portal", "profile": "kraj z profilu sprzedawcy",
          "language": "tytuł w obcym języku", "flag": "oznaczona jako oferta z zagranicy",
          "market": "zagraniczny rynek portalu", "portal": "portal z ofertami z Polski"}


@dataclass(frozen=True)
class Origin:
    foreign: bool
    country: str | None = None  # kod ISO, np. "CZ"; None = nieznany
    basis: str = "portal"  # item | profile | language | flag | market | portal
    language: str | None = None  # język tytułu, gdy tylko on wskazuje zagranicę (np. "cs/sk")

    @property
    def key(self) -> str:
        return "foreign" if self.foreign else "pl"

    @property
    def name(self) -> str:
        if self.country:
            return COUNTRY_NAMES.get(self.country, self.country)
        return "zagranica" if self.foreign else "Polska"

    @property
    def label(self) -> str:
        """Krótko, do tabeli i listy na telefonie: „Polska”, „🌍 Czechy”, „🌍 zagranica (czeski / słowacki)”."""
        if not self.foreign:
            return self.name
        if self.country:
            return f"{FOREIGN_MARK} {self.name}"
        lang = LANGUAGE_NAMES.get(self.language or "", self.language)
        return f"{FOREIGN_MARK} zagranica" + (f" ({lang})" if lang else "")

    def describe(self) -> str:
        """Do szczegółów oferty: kraj i skąd to wiadomo."""
        if not self.foreign:
            return f"Polska — {_BASIS[self.basis]}"
        if self.country:
            return f"{FOREIGN_MARK} {self.name} ({self.country}) — {_BASIS[self.basis]}"
        if self.language:
            return (f"{FOREIGN_MARK} zagranica, kraj nieznany — język tytułu: "
                    f"{LANGUAGE_NAMES.get(self.language, self.language)}")
        return f"{FOREIGN_MARK} zagranica, kraj nieznany — {_BASIS[self.basis]}"


@lru_cache(maxsize=4096)
def _title_language(title: str) -> str | None:
    guess = detect_language(title)
    return guess.language if guess.foreign else None


def offer_origin(offer: Offer) -> Origin:
    raw = offer.raw
    item = (raw.params.get("item_country") or "").upper()
    if item:
        return Origin(item != "PL", item, "item")
    seller = (offer.seller_country or "").upper()
    if seller:
        return Origin(seller != "PL", seller, "profile")
    if RedFlag.FOREIGN_SELLER in offer.parsed.flags:
        lang = _title_language(raw.title)
        return Origin(True, None, "language" if lang else "flag", lang)
    market = raw.params.get("market")
    if market and market != "EBAY_PL":
        return Origin(True, None, "market")
    return Origin(False, "PL", "portal")


def is_foreign(offer: Offer) -> bool:
    """Szybka wersja ``offer_origin(...).foreign`` dla filtrów (bez rozpoznawania języka)."""
    raw = offer.raw
    item = raw.params.get("item_country")
    if item:
        return item.upper() != "PL"
    if offer.seller_country:
        return offer.seller_country.upper() != "PL"
    if RedFlag.FOREIGN_SELLER in offer.parsed.flags:
        return True
    market = raw.params.get("market")
    return bool(market) and market != "EBAY_PL"
