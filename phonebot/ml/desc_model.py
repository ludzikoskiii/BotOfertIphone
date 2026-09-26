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
PROMPT_VERSION = 1

SYSTEM_PROMPT = """Czytasz ogłoszenia sprzedaży używanych iPhone'ów (zwykle po polsku, czasem po angielsku, \
czesku, słowacku lub niemiecku) dla osoby, która kupuje telefony, naprawia je i odsprzedaje. Wypisujesz \
wyłącznie fakty podane wprost w tytule lub opisie. Niczego nie zgadujesz — jeśli czegoś nie ma w tekście, \
wpisz null albo pustą listę. Odpowiadasz tylko w formacie JSON.

Pola:
- is_phone: true, jeśli sprzedawany jest sam telefon (także uszkodzony albo na części). false, jeśli \
przedmiotem jest tylko akcesorium (etui, szkło, ładowarka, kabel), puste pudełko, część zamienna, \
albo jest to ogłoszenie kupna lub zamiany.
- storage_gb: pamięć telefonu w GB podana w ogłoszeniu (np. 64, 128, 256, 512; 1 TB = 1024) albo null.
- battery_health: kondycja (pojemność) baterii w procentach podana w ogłoszeniu, np. „bateria 87%”, \
„kondycja 90”, „battery health 85%”. To nie jest poziom naładowania. Jeśli nie podano — null.
- for_parts: true tylko wtedy, gdy ogłoszenie mówi wprost, że telefon jest na części.
- defects: usterki, które telefon RZECZYWIŚCIE ma według ogłoszenia. Nie wpisuj usterek, którym sprzedający \
zaprzecza („ekran cały”, „Face ID działa”, „bez rys”), ani części już wymienionych na sprawne. Kody: \
screen (wyświetlacz, szyba, dotyk), back_glass (tylna szyba), battery (bateria słaba, poniżej 80%, komunikat \
serwisowy), charging_port, camera, camera_lens (szkiełko aparatu), face_id, speaker, microphone, buttons, \
housing (wgniecenia, wygięcia), no_power (nie włącza się, bootloop), water_damage (zalany).
- red_flags: sygnały ryzyka dla kupującego: icloud_lock (blokada iCloud lub aktywacji, nieznane hasło Apple ID), \
imei_blocked (zablokowany IMEI, czarna lista, kradziony), mdm (profil firmowy), replica (podróbka), simlock, \
no_signal (brak zasięgu), non_original_parts (zamienniki, nieoryginalny ekran lub bateria, komunikat \
o nieznanej części), untested („nie sprawdzałem”, „sprzedaję jak jest”).
- note: jedno krótkie zdanie po polsku — najważniejsza informacja dla kupującego (np. „Ekran wymieniony \
na zamiennik, bateria 79%.”) albo pusty tekst."""


def _nullable_int() -> dict[str, Any]:
    return {"anyOf": [{"type": "integer"}, {"type": "null"}]}


SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "is_phone": {"type": "boolean"},
        "storage_gb": _nullable_int(),
        "battery_health": _nullable_int(),
        "for_parts": {"type": "boolean"},
        "defects": {"type": "array", "items": {"type": "string", "enum": [d.value for d in Defect]}},
        "red_flags": {"type": "array", "items": {"type": "string", "enum": [f.value for f in TEXT_FLAGS]}},
        "note": {"type": "string"},
    },
    "required": ["is_phone", "storage_gb", "battery_health", "for_parts", "defects", "red_flags", "note"],
}


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

    out.defects = list(dict.fromkeys(Defect(d) for d in data.get("defects", []) if d in Defect._value2member_map_))
    known = {f.value for f in TEXT_FLAGS}
    out.flags = list(dict.fromkeys(RedFlag(f) for f in data.get("red_flags", []) if f in known))
    out.note = " ".join(str(data.get("note") or "").split())[:200]
    return out


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


def analyze(client, model: str, *, title: str, description: str, phone_model: str | None) -> DescFindings:
    """Jedno zapytanie do Ollamy (``client`` = ``OllamaClient``) i sprawdzenie wyniku."""
    data = client.chat_json(model, SYSTEM_PROMPT, build_user_prompt(title, description, phone_model), SCHEMA)
    return validate(data, title=title, description=description, phone_model=phone_model)
