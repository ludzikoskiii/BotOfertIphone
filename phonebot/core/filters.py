"""Odsiewanie ogłoszeń, które nie są sprzedażą telefonu (akcesoria, części, „kupię")."""
from __future__ import annotations

import re

from .text import normalize

# Ogłoszenia zaczynające się od tych słów to akcesoria/części, nie telefony.
_ACCESSORY_START = re.compile(
    r"^(?:nowe? |oryginaln\w* |apple )?(?:etui|case|pokrowiec|szklo|szkielko|folia|ladowark\w*|kabel|zasilacz|"
    r"pudelko|pudelka|obudow\w*|plecki|korpus|ramka|wyswietlacz\w*|ekran\w*|lcd|oled|bateri\w*|akumulator\w*|"
    r"tasm\w*|plyt\w*|aparat\w*|glosni\w*|uchwyt\w*|sluchawk\w*|airpods|apple watch|ipad|macbook|imac|"
    r"magsafe|adapter|klapk\w*|szyb\w*|zestaw czesci|czesci do)\b"
)
_ACCESSORY_ANYWHERE = re.compile(r"\b(?:etui do|case do|szklo hartowane do|folia do|pokrowiec do|ladowarka do)\b")
_WANTED = re.compile(r"^(?:kupie|kupimy|skupuje|skupujemy|skup|zamienie|zamiana|poszukuje|szukam)\b")


def listing_rejection_reason(title: str) -> str | None:
    """Zwraca powód odrzucenia ogłoszenia albo ``None``, gdy to sprzedaż telefonu."""
    t = normalize(title)
    if _WANTED.search(t):
        return "ogłoszenie kupna/zamiany"
    if _ACCESSORY_START.search(t) or _ACCESSORY_ANYWHERE.search(t):
        return "akcesorium lub część"
    return None
