"""Wykrywanie czerwonych flag w ogłoszeniu.

Flagi tekstowe są wykrywane tutaj; flagi zależne od wyceny (np. podejrzanie
niska cena, nieznany koszt naprawy) dodaje moduł ``valuation``.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .models import Condition, Defect, RedFlag
from .text import Phrase, any_match, phrase

if TYPE_CHECKING:
    from .models import RawOffer

FLAG_PHRASES: dict[RedFlag, tuple[Phrase, ...]] = {
    RedFlag.ICLOUD_LOCK: (
        phrase(r"\bblokad\w*\s(?:na\s)?(?:icloud|i cloud|aktywacji|apple ?id|konta apple|konto apple|konta icloud)"),
        phrase(r"\bzablokowan\w*\s(?:na\s|do\s)?(?:icloud|i cloud|apple ?id|kont\w*|koncie|aktywacj\w*)"),
        phrase(r"\bicloud\s(?:lock\w*|zablokowan\w*|blokada|na blokadzie|do zdjecia|do odblokowania|aktywny)"),
        phrase(r"\b(?:activation lock|na blokadzie|zalogowan\w* na (?:cudze|obce|czyjes|nieznane) konto)"),
        phrase(r"\b(?:nie znam|brak|zapomnial\w*|nie pamietam)\s(?:\w+\s){0,2}hasl\w*\s(?:do\s)?(?:icloud|apple ?id|konta)",
               negatable=False),
        phrase(r"\bna konto (?:icloud|apple) (?:poprzedniego|innego|bylego)"),
    ),
    RedFlag.IMEI_BLOCKED: (
        phrase(r"\bzablokowan\w*\simei"),
        phrase(r"\bimei\s(?:jest\s)?(?:zablokowan\w*|zastrzezon\w*|na czarnej liscie|blacklist\w*|zgloszon\w*)"),
        phrase(r"\b(?:czarn\w* lis(?:t|cie)\w*|blacklist\w*|blacklisted)"),
        phrase(r"\bzgloszon\w* jako (?:kradzion\w*|zgubion\w*|utracon\w*)"),
    ),
    RedFlag.MDM: (
        phrase(r"\bmdm\b"),
        phrase(r"\bprofil\w* (?:firmow\w*|mdm|zarzadzani\w*|korporacyjn\w*)"),
        phrase(r"\bzarzadzan\w* (?:przez|zdalnie przez) (?:firme|organizacje|pracodawce)"),
    ),
    RedFlag.REPLICA: (
        phrase(r"\b(?:replik\w*|podrob\w*|podrabian\w*|klon|kopia|hdc|chinsk\w* odpowiednik\w*|nie oryginal apple)\b"),
    ),
    RedFlag.SIMLOCK: (
        phrase(r"\bsimlock\w*"),
        phrase(r"\bzablokowan\w* (?:na|pod) (?:siec\w*|operatora|plus|play|orange|t mobile|tmobile|heyah)"),
        phrase(r"\bdziala tylko (?:w|z|na) (?:siec\w*|plus|play|orange|t mobile|tmobile)"),
    ),
    RedFlag.NO_SIGNAL: (
        phrase(r"\b(?:brak|nie lapie|nie ma|nie widzi|nie wykrywa)\s(?:zasiegu|sieci|karty sim|karty|sim)\b", negatable=False),
        phrase(r"\bno service\b"),
    ),
    RedFlag.NON_ORIGINAL_PARTS: (
        phrase(r"\bzamiennik\w*"),
        phrase(r"\bnieoryginaln\w* (?:ekran|wyswietlacz|bateri\w*|czesc\w*|szyb\w*)"),
        phrase(r"\b(?:ekran|wyswietlacz|bateri\w*) (?:nie ?oryginaln\w*|zamiennik\w*|nie ?orginaln\w*)"),
        phrase(r"\bnieznan\w* czesc\w*"),
    ),
    RedFlag.UNTESTED: (
        phrase(r"\b(?:nie sprawdzal\w*|nie testowan\w*|nie testowal\w*|nieprzetestowan\w*|niesprawdzon\w*)",
               negatable=False),
        phrase(r"\b(?:nie wiem czy dziala|sprzedaje jak jest|stan nieznany|bez gwarancji dzialania|nie znam stanu)",
               negatable=False),
    ),
}

# Minimalna długość opisu (po normalizacji), poniżej której „na części" bez
# wskazanej usterki uznajemy za niewyjaśnione.
MIN_EXPLAINED_DESCRIPTION = 120


def detect_flags(
    full_text: str,
    raw: RawOffer,
    condition: Condition,
    defects: list[Defect],
    description: str,
) -> list[RedFlag]:
    flags = [flag for flag, patterns in FLAG_PHRASES.items() if any_match(patterns, full_text)]
    if not raw.photos:
        flags.append(RedFlag.NO_PHOTOS)
    if condition is Condition.FOR_PARTS and not defects and len(description) < MIN_EXPLAINED_DESCRIPTION:
        flags.append(RedFlag.FOR_PARTS_UNEXPLAINED)
    return flags
