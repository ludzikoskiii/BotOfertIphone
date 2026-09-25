"""Adaptery portali. Import modułu rejestruje adapter w ``REGISTRY``."""
from . import olx  # noqa: F401
from .base import REGISTRY, SearchQuery, SourceAdapter, SourceError, register, search_phrases

__all__ = ["REGISTRY", "SearchQuery", "SourceAdapter", "SourceError", "register", "search_phrases"]
