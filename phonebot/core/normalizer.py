"""Wyciąganie modelu, pojemności, stanu i usterek z tytułu/opisu ogłoszenia."""
from __future__ import annotations

import re

from .catalog import ALL_STORAGES, MAX_KNOWN_GENERATION, MODELS_BY_NAME, storages_for
from .models import Condition, Defect, ParsedInfo, RawOffer
from .red_flags import detect_flags
from .text import Phrase, any_match, normalize, phrase

# ---------------------------------------------------------------- model ---

_GEN = r"(?P<gen>(?:1[0-9]e?|[6-9]s?)(?![0-9])|(?:x[sr]?|se|air)(?![a-z0-9]))"
_VAR = r"(?:\s?(?P<var>pro max|promax|pro|plus|max|mini|air|\+))?"
_SE = r"(?:\s(?P<se>20(?:16|20|22)|[123](?: ?gen\w*| generacj\w*)?)\b)?"
_IPHONE_RE = re.compile(r"\b(?:iphone|iphon|ifon|ajfon|i phone)\s?(?:apple\s)?" + _GEN + _VAR + _SE)
_BARE_RE = re.compile(r"^(?:apple\s)?" + _GEN + _VAR + _SE)

_SE_GENERATIONS = {"2016": "2016", "1": "2016", "2020": "2020", "2": "2020", "2022": "2022", "3": "2022"}


def _canonical_model(gen: str, var: str | None, se: str | None) -> str | None:
    var = (var or "").strip()
    if var == "promax":
        var = "pro max"
    if gen == "se":
        key = se.strip() if se else ""
        year = _SE_GENERATIONS.get(key[:4] if key.startswith("20") else key[:1])
        return f"iPhone SE ({year})" if year else "iPhone SE"
    if gen == "air":
        return "iPhone Air"
    if gen in ("x", "xr", "xs"):
        if gen == "xs" and var in ("max", "pro max"):
            return "iPhone XS Max"
        if var:
            return None
        return f"iPhone {gen.upper()}"
    if gen.endswith("e") and gen[:-1].isdigit():
        name = f"iPhone {gen}"
        return name if (name in MODELS_BY_NAME or int(gen[:-1]) > MAX_KNOWN_GENERATION) else None

    base = f"iPhone {gen}"
    number = int(gen.rstrip("s"))
    suffix = {
        "": "",
        "pro": " Pro",
        "pro max": " Pro Max",
        "max": " Pro Max" if number >= 11 else "",
        "plus": " Plus",
        "+": " Plus",
        "mini": " mini",
    }.get(var)
    if var == "air":
        return "iPhone Air" if number == 17 else None
    if suffix is None or (var == "max" and number < 11):
        return None
    name = base + suffix
    if name in MODELS_BY_NAME or number > MAX_KNOWN_GENERATION:
        return name
    return None


def parse_model(text: str, *, bare: bool = False) -> str | None:
    """Zwraca kanoniczną nazwę modelu (np. ``iPhone 13 Pro Max``) lub ``None``.

    ``text`` musi być już znormalizowany. ``bare=True`` pozwala rozpoznać tytuły
    bez słowa „iPhone" (np. „13 Pro 128GB") — używane, gdy kategoria portalu to iPhone.
    """
    for m in _IPHONE_RE.finditer(text):
        name = _canonical_model(m.group("gen"), m.group("var"), m.group("se"))
        if name:
            return name
    if bare:
        m = _BARE_RE.match(text)
        if m:
            return _canonical_model(m.group("gen"), m.group("var"), m.group("se"))
    return None


# -------------------------------------------------------------- storage ---

_STORAGE_RE = re.compile(r"\b(\d{2,4})\s?(?:gb|g|giga\w*)\b|\b([12])\s?(?:tb|tera\w*)\b")
_BARE_STORAGE_RE = re.compile(r"\b(64|128|256|512)\b")


def parse_storage(text: str, model: str | None, *, allow_bare: bool = False) -> int | None:
    valid = storages_for(model)
    for m in _STORAGE_RE.finditer(text):
        gb = int(m.group(1)) if m.group(1) else int(m.group(2)) * 1024
        if gb in valid:
            return gb
    if allow_bare and model:
        for m in _BARE_STORAGE_RE.finditer(text):
            gb = int(m.group(1))
            if gb in valid:
                return gb
    return None


# -------------------------------------------------------------- bateria ---

_BATTERY_RE = re.compile(
    r"\b(?:bateri\w*|kondycj\w*|battery|bat|aku\w*|stan baterii)\b[^|0-9]{0,25}?(\d{2,3})\s?%"
    r"|\b(\d{2,3})\s?%\s?(?:kondycj\w*|bateri\w*)"
)


def parse_battery_health(text: str) -> int | None:
    for m in _BATTERY_RE.finditer(text):
        value = int(m.group(1) or m.group(2))
        if 40 <= value <= 100:
            return value
    return None


# ----------------------------------------------------------- negocjacje ---

_NOT_NEGOTIABLE = (
    phrase(r"\bbez (?:negocjacji|targowania|negocjowania)", negatable=False),
    phrase(r"\bcena (?:ostateczna|stala|nie podlega negocjacji)", negatable=False),
    phrase(r"\bnie (?:negocjuje|schodze z ceny|opuszczam|podlega negocjacji)", negatable=False),
)
_NEGOTIABLE = (
    phrase(r"\b(?:do negocjacji|do dogadania|do uzgodnienia|negocjowaln\w*)\b"),
    phrase(r"\bmozliw\w* (?:negocjacj\w*|negocjowani\w*|rabat)"),
)


def parse_negotiable(text: str) -> bool | None:
    if any_match(_NOT_NEGOTIABLE, text):
        return False
    if any_match(_NEGOTIABLE, text):
        return True
    return None


# -------------------------------------------------------------- usterki ---

_BREAK = r"(?:zbit|pekniet|rozbit|popekan|stluczon|uszkodzon)\w*"
_FILL = r"(?:(?:jest|lekko|troche|mocno|calkowicie|caly|cala|cale|delikatnie|minimalnie|ma)\s)?"

DEFECT_PHRASES: dict[Defect, tuple[Phrase, ...]] = {
    Defect.BACK_GLASS: (
        phrase(rf"\b{_BREAK}\s(?:\w+\s)?(?:tyl(?:\b|n)|plec\w*|klap\w*)"),
        phrase(rf"\b(?:tyl|plecki|tyln\w* (?:szyb\w*|klapk\w*|panel\w*)|szyb\w* (?:z )?tylu|tyl obudowy)\s{_FILL}{_BREAK}"),
    ),
    Defect.CAMERA_LENS: (
        phrase(rf"\b{_BREAK}\s(?:szkiel\w*|szybk\w*|oczk\w*|obiektyw\w*|soczewk\w*)\s(?:od\s)?aparat"),
        phrase(rf"\b{_BREAK}\s(?:oczk\w*|obiektyw\w*|soczewk\w*|szkielk\w*)"),
        phrase(rf"\b(?:szkiel\w*|szybk\w*|oczk\w*|obiektyw\w*|soczewk\w*)\s(?:od\s)?aparatu\s{_FILL}{_BREAK}"),
    ),
    Defect.SCREEN: (
        phrase(rf"\b{_BREAK}\s(?:(?!tyl|plec)\w+\s)?(?:ekran|szyb\w*+(?!\s(?:od\s)?aparat)|wyswietlacz\w*|lcd|display)"),
        phrase(rf"(?<!tylna )(?<!tylnia )(?<!tylnej )\b(?:ekran|szyb(?:a|ka)|wyswietlacz)\s{_FILL}{_BREAK}"),
        phrase(r"\bpajacz\w*"),
        phrase(r"\b(?:zielon\w*|rozow\w*|bial\w*) (?:pas\w*|lini\w*|kresk\w*)"),
        phrase(r"\b(?:pas\w*|lini\w*|plam\w*|smug\w*) na (?:ekranie|wyswietlaczu)"),
        phrase(r"\bnie ?dziala (?:dotyk|ekran|wyswietlacz)", negatable=False),
        phrase(r"\b(?:dotyk|ekran|wyswietlacz) (?:nie ?dziala|nie reaguje|wariuje|sam klika)", negatable=False),
        phrase(r"\b(?:ekran|wyswietlacz|szybka) do wymiany"),
    ),
    Defect.BATTERY: (
        phrase(r"\b(?:slab\w*|zuzyt\w*|padniet\w*|kiepsk\w*|spuchnie\w*|napuchniet\w*) bateri"),
        phrase(r"\bbateri\w* (?:do wymiany|slab\w*|zuzyt\w*|padniet\w*|kiepsk\w*|spuchniet\w*|napuchniet\w*|szybko)"),
        phrase(r"\bbateri\w* (?:nie trzyma|trzyma slabo|slabo trzyma)", negatable=False),
        phrase(r"\b(?:komunikat )?serwis(?:u|owa)? bateri"),
        phrase(r"\bszybko (?:sie )?rozladowuj"),
        phrase(r"\bwylacza sie przy \d{1,2}\s?%"),
    ),
    Defect.CHARGING_PORT: (
        phrase(r"\bnie (?:laduje|chce ladowac|ladowal|chce sie ladowac)", negatable=False),
        phrase(r"\bproblem\w* z ladowaniem"),
        phrase(r"\b(?:port|gniazd\w*|zlacz\w*|wejsci\w*)\s(?:ladowania\s|lightning\s|usb c\s)?"
               r"(?:uszkodzon\w*|nie ?dziala|do wymiany|luzn\w*|wyrobion\w*|zalan\w*)"),
        phrase(r"\buszkodzon\w* (?:port|gniazd\w*|zlacz\w*)"),
        phrase(r"\bladuje (?:tylko|jedynie) (?:bezprzewodowo|indukcyjnie|na indukcji)", negatable=False),
    ),
    Defect.CAMERA: (
        phrase(r"\b(?:aparat\w*|kamer\w*)\s(?:(?:tyln\w*|przedni\w*|glown\w*|szerokokatn\w*|tele\w*)\s)?"
               r"(?:nie ?dziala|uszkodzon\w*|nie ostrzy|drga|trzesie|czarny|nie wlacza|brzeczy)", negatable=False),
        phrase(r"\b(?:uszkodzon\w*|niedzialaj\w*|nie dzialaj\w*|nie ?dziala) (?:aparat|kamer)", negatable=False),
    ),
    Defect.FACE_ID: (
        phrase(r"\b(?:face ?id|faceid)(?:\s(?:jest|tez|rowniez|niestety))?\s"
               r"(?:nie ?dziala|uszkodzon\w*|off|nie ?aktywn\w*|niesprawn\w*|padniet\w*|nie ?dostepn\w*|wylaczon\w*)",
               negatable=False),
        phrase(r"\b(?:brak|bez|nie ?dziala|uszkodzon\w*|niesprawn\w*|niedzialaj\w*|nie dzialaj\w*)\s(?:face ?id|faceid)",
               negatable=False),
    ),
    Defect.SPEAKER: (
        phrase(r"\bglosni\w*\s(?:(?:gorny|dolny|rozmow\w*|do rozmow|przy uchu|multimedialny)\s)?"
               r"(?:nie ?dziala|uszkodzon\w*|charczy|trzeszczy|rzezi|gra cicho|slabo gra)", negatable=False),
        phrase(r"\b(?:nie ?dziala|uszkodzon\w*) glosni", negatable=False),
        phrase(r"\bnie slychac rozmowcy", negatable=False),
    ),
    Defect.MICROPHONE: (
        phrase(r"\bmikrofon\w*\s(?:nie ?dziala|uszkodzon\w*)", negatable=False),
        phrase(r"\b(?:nie ?dziala|uszkodzon\w*) mikrofon", negatable=False),
        phrase(r"\b(?:nie slychac mnie|rozmowca mnie nie slyszy|mnie nie slychac)", negatable=False),
    ),
    Defect.BUTTONS: (
        phrase(r"\b(?:przycisk\w*|guzik\w*|power)\s(?:(?:zasilania|glosnosci|power|boczny|wyciszania|home)\s)?"
               r"(?:nie ?dziala|zapada\w*|uszkodzon\w*|zacina)", negatable=False),
        phrase(r"\b(?:nie ?dziala|uszkodzon\w*) (?:przycisk|guzik|power)", negatable=False),
    ),
    Defect.HOUSING: (
        phrase(r"\b(?:wgniecen\w*|wgniot\w*|pogiet\w*|wygiet\w*|krzyw\w* obudow\w*)"),
        phrase(r"\bobudow\w* (?:uszkodzon\w*|pogiet\w*|wygiet\w*|krzyw\w*)"),
        phrase(r"\bobit\w* (?:rog\w*|rant\w*|ramk\w*|naroz\w*)"),
    ),
    Defect.NO_POWER: (
        phrase(r"\bnie (?:wlacza|odpala|startuje|uruchamia)\b", negatable=False),
        phrase(r"\b(?:sie|telefon) nie (?:wlacza|odpala|uruchamia)", negatable=False),
        phrase(r"\b(?:brak|nie daje) oznak zycia", negatable=False),
        phrase(r"\b(?:bootloop|boot loop|zapetla sie|wisi na (?:jablku|logo)|zawiesza sie na (?:jablku|logo))"),
        phrase(r"\bmartw\w* (?:telefon|plyt\w*)"),
    ),
    Defect.WATER_DAMAGE: (
        phrase(r"\b(?:zalan\w*|po zalaniu|zamoczon\w*|podtopion\w*)"),
        phrase(r"\bwpadl\w* do (?:wody|wanny|toalety|kibla|jeziora|basenu)"),
        phrase(r"\bkontakt\w* z (?:woda|ciecza|plynem)"),
    ),
}

_FOR_PARTS = (
    phrase(r"\bna czesci\b"),
    phrase(r"\b(?:dawca|jako dawca|na dawce)\b"),
)
_DAMAGED = (
    phrase(r"\b(?:uszkodzon\w*|niesprawn\w*|do naprawy|zepsut\w*|popsut\w*)\b"),
)
_NEW = (
    phrase(r"\b(?:fabrycznie nowy|nowy nieuzywany|zafoliowan\w*|nieotwieran\w*|nowka sztuka|nowy w folii|"
           r"nowy zaplombowany|sealed|nieuzywan\w*|nowy z salonu|nowy nierozpakowan\w*)"),
)
_LIKE_NEW = (
    phrase(r"\b(?:stan idealny|idealny stan|jak nowy|stan jak nowy|stan igla|bez sladow uzytkowania|"
           r"bez rys\w*|perfekcyjny stan|stan perfekcyjny|stan salonowy)"),
)

_PARAM_CONDITIONS = {
    "new": Condition.NEW,
    "nowe": Condition.NEW,
    "nowy": Condition.NEW,
    "used": Condition.GOOD,
    "uzywane": Condition.GOOD,
    "uzywany": Condition.GOOD,
    "damaged": Condition.DAMAGED,
    "uszkodzone": Condition.DAMAGED,
    "uszkodzony": Condition.DAMAGED,
    "broken": Condition.DAMAGED,
    "for_parts": Condition.FOR_PARTS,
    "na czesci": Condition.FOR_PARTS,
}


def detect_defects(text: str, battery_health: int | None, battery_threshold: int) -> list[Defect]:
    found = [d for d, patterns in DEFECT_PHRASES.items() if any_match(patterns, text)]
    if battery_health is not None and battery_health < battery_threshold and Defect.BATTERY not in found:
        found.append(Defect.BATTERY)
    return found


def detect_condition(text: str, defects: list[Defect], param_condition: str | None) -> Condition:
    param = _PARAM_CONDITIONS.get(normalize(param_condition)) if param_condition else None
    if any_match(_FOR_PARTS, text):
        return Condition.FOR_PARTS
    if any(not d.cosmetic for d in defects):
        return Condition.DAMAGED
    if param in (Condition.DAMAGED, Condition.FOR_PARTS):
        return param
    if any_match(_DAMAGED, text):
        return Condition.DAMAGED
    if param is Condition.NEW or any_match(_NEW, text):
        return Condition.NEW
    if any_match(_LIKE_NEW, text):
        return Condition.LIKE_NEW
    return Condition.GOOD


def parse_offer(raw: RawOffer, *, battery_threshold: int = 80, assume_iphone: bool = True) -> ParsedInfo:
    """Pełna analiza ogłoszenia (bez wyceny)."""
    title = normalize(raw.title)
    desc = normalize(raw.description)
    params_model = normalize(raw.params.get("model", ""))
    full = f"{title} | {desc}"

    model = (
        parse_model(title, bare=assume_iphone)
        or parse_model(params_model, bare=True)
        or parse_model(desc)
    )
    storage = (
        parse_storage(title, model, allow_bare=True)
        or parse_storage(normalize(raw.params.get("storage", "")), model, allow_bare=True)
        or parse_storage(desc, model)
    )
    battery = parse_battery_health(full)
    defects = detect_defects(full, battery, battery_threshold)
    condition = detect_condition(full, defects, raw.params.get("condition"))
    negotiable = raw.negotiable if raw.negotiable is not None else parse_negotiable(full)
    flags = detect_flags(full, raw, condition, defects, desc)
    return ParsedInfo(
        model=model,
        storage_gb=storage,
        condition=condition,
        defects=defects,
        flags=flags,
        battery_health=battery,
        negotiable=negotiable,
    )


def valid_storage(model: str | None, gb: int | None) -> bool:
    return gb is not None and gb in (storages_for(model) if model else ALL_STORAGES)
