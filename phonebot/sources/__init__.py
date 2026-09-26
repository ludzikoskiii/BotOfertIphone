"""Adaptery portali. Import modułu rejestruje adapter w ``REGISTRY``."""
from . import allegro_lokalnie, olx, sprzedajemy, vinted  # noqa: F401
from .base import REGISTRY, SearchQuery, SourceAdapter, SourceError, register, search_phrases

# Kolejność wyświetlania portali w GUI (nieznane na końcu, alfabetycznie).
_ORDER = ("olx", "allegro_lokalnie", "vinted", "sprzedajemy", "facebook")


def source_names() -> dict[str, str]:
    """Klucz portalu → nazwa wyświetlana, w stałej kolejności (z rejestru adapterów)."""
    keys = sorted(REGISTRY, key=lambda k: (_ORDER.index(k) if k in _ORDER else len(_ORDER), k))
    return {k: REGISTRY[k].display_name for k in keys}


SOURCE_NAMES = source_names()

__all__ = ["REGISTRY", "SOURCE_NAMES", "SearchQuery", "SourceAdapter", "SourceError", "register", "search_phrases",
           "source_names"]
