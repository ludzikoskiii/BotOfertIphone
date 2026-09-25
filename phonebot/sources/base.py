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


@dataclass
class SearchQuery:
    mode: Mode
    phrases: list[str] = field(default_factory=lambda: ["iphone"])
    price_min: float | None = None
    price_max: float | None = None
    max_pages: int = 3


class SourceError(Exception):
    """Błąd źródła — izolowany na poziomie adaptera, nie przerywa pozostałych."""


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
