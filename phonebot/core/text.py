"""Normalizacja tekstu ogłoszeń i dopasowywanie fraz z obsługą zaprzeczeń."""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_PL_MAP = str.maketrans("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ", "acelnoszzACELNOSZZ")
# litery spoza polskiego alfabetu, których rozkład Unicode nie zamienia na łacińskie
_EXTRA_MAP = str.maketrans({"ß": "ss", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D", "æ": "ae", "Æ": "AE", "œ": "oe",
                            "Œ": "OE", "ı": "i"})


def fold_accents(text: str) -> str:
    """Litery z akcentami innych języków → łacińskie (ü→u, ě→e, ė→e), żeby „Hülle” nie znikało."""
    if text.isascii():
        return text
    text = text.translate(_EXTRA_MAP)
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))

# Znaki kończące zdanie/fragment zamieniamy na separator " | ", żeby frazy
# nie „przeskakiwały" między zdaniami, a okno zaprzeczeń kończyło się na granicy.
_SENTENCE_BREAK = re.compile(r"[.!?;,\n\r\t()\[\]/\\]+")
_OTHER = re.compile(r"[^a-z0-9%+| ]+")
_SPACES = re.compile(r"\s+")

# Spójniki przedłużające zasięg przeczenia: „bez blokad icloud i simlocka".
CONJUNCTIONS = frozenset({"i", "oraz", "ani", "czy", "lub"})
NEGATION_WORDS = frozenset(
    {"nie", "bez", "brak", "zadnych", "zadnej", "zadnego", "zero", "wolny", "wolna", "nigdy"}
)
NOMINAL_NEGATIONS = frozenset({"bez", "brak", "wolny", "wolna", "zadnych", "zadnej", "zadnego"})


def normalize(text: str | None) -> str:
    """Małe litery, bez polskich znaków, interpunkcja zamieniona na separator ``|``."""
    if not text:
        return ""
    s = fold_accents(text.translate(_PL_MAP)).lower()
    s = s.replace("-", " ").replace("_", " ")
    s = _SENTENCE_BREAK.sub(" | ", s)
    s = _OTHER.sub(" ", s)
    s = _SPACES.sub(" ", s)
    return s.strip()


def is_negated(text: str, start: int, window: int = 2) -> bool:
    """Czy tuż przed pozycją ``start`` (w obrębie zdania) stoi słowo przeczące."""
    words = text[:start].split()
    before: list[str] = []
    for w in reversed(words):
        if w == "|":
            break
        before.append(w)
    if any(w in NEGATION_WORDS for w in before[:window]):
        return True
    # Po spójniku przeczenie rzeczownikowe obejmuje kolejne elementy wyliczenia
    # („bez blokad icloud i simlocka"), ale „nie włącza się i zbity ekran" — już nie.
    if before and before[0] in CONJUNCTIONS:
        extended = before[1:window + 5]
        if any(w in NOMINAL_NEGATIONS for w in extended):
            return True
        if any(a == "ma" and b == "nie" for a, b in zip(extended, extended[1:], strict=False)):
            return True
    return False


@dataclass(frozen=True)
class Phrase:
    """Wzorzec frazy. ``negatable`` = odrzuć dopasowanie poprzedzone zaprzeczeniem."""

    pattern: re.Pattern[str]
    negatable: bool = True

    def search(self, text: str) -> bool:
        for m in self.pattern.finditer(text):
            if self.negatable and is_negated(text, m.start()):
                continue
            return True
        return False


def phrase(regex: str, negatable: bool = True) -> Phrase:
    return Phrase(re.compile(regex), negatable)


_COMBINED: dict[int, tuple[object, re.Pattern[str] | None]] = {}


def _combined(phrases: list[Phrase] | tuple[Phrase, ...]) -> re.Pattern[str] | None:
    """Jeden wzorzec „którakolwiek z fraz” — szybki test wstępny zamiast N osobnych wyszukiwań."""
    entry = _COMBINED.get(id(phrases))
    if entry is not None and entry[0] is phrases:
        return entry[1]
    sources = [p.pattern.pattern for p in phrases]
    # flagi globalne „(?i)” i odwołania „\1” nie przeżyją sklejenia w alternatywę
    safe = phrases and all(not src.startswith("(?") or src.startswith(("(?:", "(?=", "(?!", "(?<"))
                           for src in sources) and not any(re.search(r"\\\d", src) for src in sources)
    pattern = re.compile("|".join(f"(?:{src})" for src in sources)) if safe else None
    _COMBINED[id(phrases)] = (phrases, pattern)
    return pattern


def any_match(phrases: list[Phrase] | tuple[Phrase, ...], text: str) -> bool:
    combined = _combined(phrases)
    if combined is not None and combined.search(text) is None:
        return False  # żadna fraza nie występuje — typowy przypadek, bez sprawdzania zaprzeczeń
    return any(p.search(text) for p in phrases)
