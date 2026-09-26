"""Interfejs adaptera portalu ogłoszeniowego.

Nowy portal = nowa klasa dziedzicząca po ``SourceAdapter`` zarejestrowana
dekoratorem ``@register``. Adapter tylko pobiera i parsuje oferty do
``RawOffer``; normalizacja, wycena i zapis dzieją się poza nim.
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import ClassVar

from ..core.models import Mode, RawOffer
from ..net.http import HttpError


@dataclass
class SearchQuery:
    mode: Mode
    phrases: list[str] = field(default_factory=lambda: ["iphone"])
    price_min: float | None = None
    price_max: float | None = None
    max_pages: int = 3


def search_phrases(watched_models: list[str], mode: Mode) -> list[str]:
    """Frazy wyszukiwania: ogólne (zbierają też dane rynkowe) + specyficzne dla trybu."""
    phrases = ["iphone"]
    phrases += [m.lower() for m in watched_models]
    if mode is Mode.REPAIR:
        phrases += ["iphone uszkodzony", "iphone zbity", "iphone na części"]
    return list(dict.fromkeys(phrases))


class SourceError(Exception):
    """Błąd źródła — izolowany na poziomie adaptera, nie przerywa pozostałych."""

    kind = "error"


class SourceNetworkError(SourceError):
    """Brak połączenia z portalem (internet, DNS, proxy, timeout)."""

    kind = "network"


class SourceBlocked(SourceError):
    """Portal blokuje automatyczne pobieranie (403/429, captcha, ochrona antybotowa)."""

    kind = "blocked"


class SourceFormatChanged(SourceError):
    """Portal zmienił adres API lub strukturę danych — adapter wymaga aktualizacji."""

    kind = "changed"


def source_error_from_http(e: HttpError, context: str = "") -> SourceError:
    """Zamienia błąd HTTP na kategorię błędu źródła (do statusu w GUI)."""
    prefix = f"{context}: " if context else ""
    if e.network:
        return SourceNetworkError(f"{prefix}{e}")
    if e.blocked or e.status in (401, 403, 429):
        return SourceBlocked(f"{prefix}{e} — portal blokuje automatyczne pobieranie")
    if e.status in (404, 410):
        return SourceFormatChanged(f"{prefix}{e} — adres API już nie istnieje (portal zmienił API)")
    if e.status is None:  # np. odpowiedź nie jest JSON-em
        return SourceFormatChanged(f"{prefix}{e}")
    return SourceError(f"{prefix}{e}")


async def search_all_phrases(phrases: list[str], search_phrase) -> list:
    """Wspólna pętla po frazach: przerywa przy blokadzie/braku sieci, zwraca zebrane oferty.

    ``search_phrase(phrase, out)`` dopisuje oferty do słownika ``out``.
    Gdy żadna fraza nic nie zwróciła i wystąpił błąd — rzuca najpoważniejszy błąd.
    """
    out: dict = {}
    errors: list[SourceError] = []
    for phrase in phrases:
        try:
            await search_phrase(phrase, out)
        except HttpError as e:
            err = source_error_from_http(e, f"„{phrase}”")
            errors.append(err)
            if isinstance(err, (SourceBlocked, SourceNetworkError, SourceFormatChanged)):
                break
        except SourceError as e:
            errors.append(e)
            if e.kind != "error":  # blokada / sieć / zmiana formatu — kolejne frazy nic nie dadzą
                break
    if errors and not out:
        raise errors[0]
    return list(out.values())


class SourceAdapter(abc.ABC):
    #: identyfikator techniczny (klucz w ustawieniach i bazie)
    key: ClassVar[str]
    #: nazwa wyświetlana w GUI
    display_name: ClassVar[str]

    @abc.abstractmethod
    async def search(self, query: SearchQuery) -> list[RawOffer]:
        """Zwraca oferty pasujące do zapytania. Rzuca ``SourceError`` przy awarii."""


REGISTRY: dict[str, type[SourceAdapter]] = {}


def register(cls: type[SourceAdapter]) -> type[SourceAdapter]:
    REGISTRY[cls.key] = cls
    return cls
