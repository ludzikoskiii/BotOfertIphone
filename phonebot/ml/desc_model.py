"""Czytanie opisu oferty lokalnym modelem językowym (Ollama): dane, których reguły nie wyłapały.

Tylko dla ofert „DO WERYFIKACJI” (tam jest wątpliwość), każda oferta raz. Model zwraca ściśle określony
JSON (schemat poniżej), a program sprawdza każdą wartość, zanim jej użyje:

* pamięć i kondycja baterii — tylko gdy ta liczba naprawdę występuje w tytule lub opisie
  (model nie może jej „wymyślić”), pamięć dodatkowo musi pasować do modelu iPhone'a;
* „na części” — tylko gdy tekst o tym mówi;
* usterki i czerwone flagi — tylko ze znanej listy kodów; AI może je dodać, nigdy nie usuwa wyniku reguł.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..core.catalog import storages_for
from ..core.models import Condition, Defect, Offer, RedFlag, merge_ai_findings
from ..core.text import normalize

# flagi, które da się rozpoznać z tekstu ogłoszenia (pozostałe liczy program)
TEXT_FLAGS = (RedFlag.ICLOUD_LOCK, RedFlag.IMEI_BLOCKED, RedFlag.MDM, RedFlag.REPLICA, RedFlag.SIMLOCK,
              RedFlag.NO_SIGNAL, RedFlag.NON_ORIGINAL_PARTS, RedFlag.UNTESTED)
MAX_DESCRIPTION_CHARS = 2500
PROMPT_VERSION = 2  # zmiana promptu = opisy czytane ponownie

SYSTEM_PROMPT = """Czytasz ogłoszenia z portali ogłoszeniowych o używanych iPhone'ach (zwykle po polsku, czasem \
po angielsku, czesku, słowacku lub niemiecku) dla osoby, która kupuje telefony, naprawia je i odsprzedaje. \
Odpowiadasz tylko w formacie JSON.

Zasady:
1. Wpisujesz wyłącznie to, co jest napisane wprost w tytule lub opisie. Niczego nie zgadujesz. Jeśli czegoś \
nie ma w tekście — null albo pusta lista.
2. Zaprzeczenie oznacza BRAK usterki lub blokady: „ekran cały”, „bez rys”, „Face ID działa”, „wszystko działa”, \
„bez blokad”, „bez simlocka”, „nie był naprawiany” — takich rzeczy nie wpisujesz.
3. Rysy, otarcia i ślady użytkowania to nie usterka. Część już wymieniona na sprawną (np. „bateria wymieniona \
w serwisie”) to nie usterka.
4. Przy każdej usterce i każdej fladze podajesz „quote”: dokładny fragment ogłoszenia (kilka słów, słowo \
w słowo), z którego to wynika.

Pola:
- is_phone: true, jeśli ktoś SPRZEDAJE telefon (także uszkodzony albo na części). false, jeśli to ogłoszenie \
kupna („kupię”, „skup”), zamiany („zamienię”) albo sprzedawane jest tylko akcesorium (etui, szkło, ładowarka), \
puste pudełko lub część zamienna.
- storage_gb: pamięć telefonu w GB, jeśli jest podana (1 TB = 1024), inaczej null.
- battery_health: kondycja (pojemność) baterii w procentach, jeśli jest podana, np. „bateria 87%”, \
„kondycja 90”. To nie jest poziom naładowania. Inaczej null.
- for_parts: true tylko wtedy, gdy ogłoszenie mówi wprost, że telefon jest „na części”.
- defects: usterki, które telefon ma według ogłoszenia. Kody: screen (zbity lub uszkodzony wyświetlacz, \
szyba, dotyk), back_glass (pęknięta tylna szyba), battery (bateria słaba, poniżej 80% albo komunikat \
serwisowy), charging_port (nie ładuje, gniazdo), camera, camera_lens (pęknięte szkiełko aparatu), face_id \
(Face ID nie działa), speaker, microphone, buttons, housing (wgniecenia, wygięta obudowa), no_power (nie \
włącza się, bootloop), water_damage (zalany).
- red_flags: icloud_lock (blokada iCloud lub aktywacji, nieznane hasło Apple ID), imei_blocked (zablokowany \
IMEI, czarna lista), mdm (profil firmowy MDM), replica (podróbka), simlock (telefon z simlockiem), no_signal \
(brak zasięgu), non_original_parts (nieoryginalna część albo komunikat o nieznanej części), untested \
(„nie sprawdzałem”, „sprzedaję jak jest”, stan nieznany).
- note: jedno krótkie zdanie po polsku: najważniejsza informacja dla kupującego, tylko z faktów z ogłoszenia. \
Może być pusty tekst."""


def _nullable_int() -> dict[str, Any]:
    return {"anyOf": [{"type": "integer"}, {"type": "null"}]}


def _evidence(codes: list[str]) -> dict[str, Any]:
    return {"type": "array", "items": {
        "type": "object",
        "properties": {"code": {"type": "string", "enum": codes}, "quote": {"type": "string"}},
        "required": ["code", "quote"],
    }}


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_phone": {"type": "boolean"},
        "storage_gb": _nullable_int(),
        "battery_health": _nullable_int(),
        "for_parts": {"type": "boolean"},
        "defects": _evidence([d.value for d in Defect]),
        "red_flags": _evidence([f.value for f in TEXT_FLAGS]),
        "note": {"type": "string"},
    },
    "required": ["is_phone", "storage_gb", "battery_health", "for_parts", "defects", "red_flags", "note"],
}

# Cytat, który mówi, że wszystko jest w porządku, nie może być dowodem usterki ani blokady.
_HEALTHY = re.compile(r"\b(caly|cala|cale|dziala|dzialaja|sprawn\w*|idealn\w*|bez rys\w*|bez uszkodzen|"
                      r"bez wad|oryginaln\w*|nie byl\w* naprawian\w*|works|working|perfect|no damage)\b")
_DAMAGED = re.compile(r"\b(nie dziala\w*|niesprawn\w*|nie laduje|nie wlacza|uszkodz\w*|zbit\w*|pekni\w*|peka\w*|"
                      r"rozbit\w*|zalan\w*|trzeszcz\w*|nie trzyma|slab\w*|wymiany|do wymiany|zamiennik\w*|"
                      r"nieoryginaln\w*|broken|cracked|not working|dead)\b")
# Cytat przy fladze musi mówić o tej fladze (model potrafi dopisać flagę z niepasującym cytatem).
_FLAG_WORDS = {
    RedFlag.ICLOUD_LOCK.value: r"icloud|apple ?id|aktywac|konto|hasl|blokad|lock",
    RedFlag.IMEI_BLOCKED.value: r"imei|czarn\w* list|kradzion|zastrzez|blacklist|zablokowan",
    RedFlag.MDM.value: r"mdm|firmow|profil|zarzadz",
    RedFlag.REPLICA.value: r"podrob|replik|kopi|fake|chinsk|klon",
    RedFlag.SIMLOCK.value: r"simlock|sim ?lock|siec|sieci|operator|orange|play|plus|t-?mobile|heyah|vodafone",
    RedFlag.NO_SIGNAL.value: r"zasieg|sieci|signal|modem|baseband",
    RedFlag.NON_ORIGINAL_PARTS.value: r"zamiennik|nieoryginal|nieznan\w* cz|oryginal|komunikat|non.?genuine",
    RedFlag.UNTESTED.value: r"jak jest|nie sprawdz|nie testow|nie wiem|stan nieznany|niesprawdz|untested|as is",
}
_ABSENT = re.compile(r"^(bez|brak|nie ma)\b|\b(bez|brak) (blokad|simlock|icloud|mdm)")


@dataclass
class DescFindings:
    """Sprawdzony wynik modelu dla jednego opisu."""

    is_phone: bool = True
    storage_gb: int | None = None
    battery_health: int | None = None
    for_parts: bool = False
    defects: list[Defect] = field(default_factory=list)
    flags: list[RedFlag] = field(default_factory=list)
    note: str = ""
    rejected: list[str] = field(default_factory=list)  # co model podał, ale program odrzucił (i dlaczego)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["defects"] = [d.value for d in self.defects]
        data["flags"] = [f.value for f in self.flags]
        return data

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> DescFindings:
        return cls(
            is_phone=bool(data.get("is_phone", True)),
            storage_gb=data.get("storage_gb"),
            battery_health=data.get("battery_health"),
            for_parts=bool(data.get("for_parts", False)),
            defects=[Defect(d) for d in data.get("defects", []) if d in Defect._value2member_map_],
            flags=[RedFlag(f) for f in data.get("flags", []) if f in RedFlag._value2member_map_],
            note=str(data.get("note") or ""),
            rejected=[str(x) for x in data.get("rejected", [])],
        )


def text_hash(title: str, description: str) -> str:
    """Oferta jest analizowana ponownie tylko po zmianie tytułu/opisu (albo wersji promptu)."""
    return hashlib.sha1(f"{PROMPT_VERSION}|{title}|{description}".encode()).hexdigest()[:16]


def build_user_prompt(title: str, description: str, phone_model: str | None) -> str:
    desc = (description or "").strip()[:MAX_DESCRIPTION_CHARS] or "(brak opisu)"
    hint = f"Model rozpoznany z tytułu: {phone_model}\n" if phone_model else ""
    return f"{hint}Tytuł: {title.strip()}\nOpis:\n{desc}"


def _numbers(text: str) -> set[int]:
    return {int(n) for n in re.findall(r"(?<!\d)(\d{1,4})(?!\d)", text)}


_PARTS = re.compile(r"na czesci|for parts|na dily|na diely|fur teile|defekt calosciowy")


def validate(data: dict[str, Any], *, title: str, description: str, phone_model: str | None) -> DescFindings:
    """Odpowiedź modelu → ``DescFindings``; wartości bez pokrycia w tekście są odrzucane."""
    text = normalize(f"{title} {description}")
    numbers = _numbers(text)
    out = DescFindings(is_phone=bool(data.get("is_phone", True)))

    storage = data.get("storage_gb")
    if isinstance(storage, int) and storage > 0:
        allowed = set(storages_for(phone_model))
        in_text = storage in numbers or (storage >= 1024 and storage // 1024 in numbers and "tb" in text)
        if storage not in allowed:
            out.rejected.append(f"pamięć {storage} GB nie pasuje do modelu")
        elif not in_text:
            out.rejected.append(f"pamięci {storage} GB nie ma w tekście")
        else:
            out.storage_gb = storage

    battery = data.get("battery_health")
    if isinstance(battery, int) and battery > 0:
        if not 40 <= battery <= 100:
            out.rejected.append(f"kondycja baterii {battery}% poza zakresem")
        elif battery not in numbers:
            out.rejected.append(f"kondycji baterii {battery}% nie ma w tekście")
        else:
            out.battery_health = battery

    if data.get("for_parts") is True:
        if _PARTS.search(text):
            out.for_parts = True
        else:
            out.rejected.append("„na części” bez takiego sformułowania w tekście")

    out.defects = [Defect(c) for c in _supported(data.get("defects"), {d.value for d in Defect}, text, out, flag=False)]
    out.flags = [RedFlag(c) for c in _supported(data.get("red_flags"), {f.value for f in TEXT_FLAGS}, text, out,
                                                 flag=True)]
    out.note = " ".join(str(data.get("note") or "").split())[:200]
    return out


def _flat(text: str) -> str:
    return re.sub(r"\s+", " ", normalize(text).replace("|", " ")).strip(" .,;:!")


def _supported(items: Any, known: set[str], text: str, out: DescFindings, *, flag: bool) -> list[str]:
    """Kody usterek/flag, które mają dowód: cytat z ogłoszenia, który nie jest zaprzeczeniem."""
    codes: list[str] = []
    for item in items or []:
        code, quote = (item.get("code"), item.get("quote")) if isinstance(item, dict) else (item, None)
        if code not in known or code in codes:
            continue
        if quote is not None:  # odpowiedź z cytatem (prompt v2) — sprawdź dowód
            q = _flat(str(quote))
            if len(q) < 3 or q not in _flat(text):
                out.rejected.append(f"{code}: cytatu „{quote}” nie ma w ogłoszeniu")
                continue
            if flag and _ABSENT.search(q):
                out.rejected.append(f"{code}: „{quote}” mówi, że blokady nie ma")
                continue
            if flag and code in _FLAG_WORDS and not re.search(_FLAG_WORDS[code], q):
                out.rejected.append(f"{code}: cytat „{quote}” nie mówi o tej fladze")
                continue
            if not flag and _HEALTHY.search(q) and not _DAMAGED.search(q):
                out.rejected.append(f"{code}: „{quote}” mówi, że wszystko działa")
                continue
        codes.append(code)
    return codes


def apply_to_offer(offer: Offer, *, enabled: bool = True, battery_threshold: int = 80) -> None:
    """Dokłada sprawdzony wynik opisu do ``offer.parsed`` (raz na wczytanie oferty).

    AI tylko uzupełnia: pamięć i baterię wpisuje, gdy reguły ich nie znalazły; usterki i flagi dodaje.
    Wynik dla innego (starszego) opisu jest pomijany — oferta zostanie przeczytana ponownie.
    """
    if offer.desc_applied:
        return
    offer.desc_applied = True
    layers = offer.layers
    if layers is None or (not layers.desc and not layers.desc_error):
        return
    if not enabled or layers.desc_hash != text_hash(offer.raw.title, offer.raw.description):
        layers.desc, layers.desc_error = None, None
        return
    if not layers.desc:
        return
    f = DescFindings.from_json(layers.desc)
    parsed = offer.parsed
    defects = list(f.defects)
    if parsed.battery_health is None and f.battery_health:
        parsed.battery_health = f.battery_health
        offer.ai_filled.append("battery")
        if f.battery_health < battery_threshold and Defect.BATTERY not in defects:
            defects.append(Defect.BATTERY)
    if parsed.storage_gb is None and f.storage_gb:
        parsed.storage_gb = f.storage_gb
        offer.ai_filled.append("storage")
    offer.ai_defects, offer.ai_flags = merge_ai_findings(parsed, defects, f.flags)
    if f.for_parts and parsed.condition is not Condition.FOR_PARTS:
        parsed.condition = Condition.FOR_PARTS
        offer.ai_filled.append("for_parts")
    offer.ai_note = f.note


def analyze(client, model: str, *, title: str, description: str, phone_model: str | None,
            think: bool = False) -> DescFindings:
    """Jedno zapytanie do Ollamy (``client`` = ``OllamaClient``) i sprawdzenie wyniku."""
    data = client.chat_json(model, SYSTEM_PROMPT, build_user_prompt(title, description, phone_model), SCHEMA,
                            think=think)
    return validate(data, title=title, description=description, phone_model=phone_model)
