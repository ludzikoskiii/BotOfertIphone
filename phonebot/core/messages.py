"""Szablony wiadomości do sprzedającego (bez AI): kupno, negocjacja ceny, pytania przed zakupem.

Szablony edytujesz w Ustawieniach → „Wiadomości”. W tekście można użyć pól w nawiasach klamrowych —
program wstawia dane oferty i wyceny. Gotowy tekst widać w panelu szczegółów (można go poprawić przed
skopiowaniem); przycisk „📋 Skopiuj wiadomość” kopiuje go do schowka — wysyłasz ją sam, w portalu.

Negocjacja ma trzy style: uprzejmy, konkretny i „szybki odbiór”. Argumenty są wyłącznie prawdziwe —
wynikają z ogłoszenia (usterki, bateria, rysy, brak pudełka) albo z wyceny (ceny podobnych ofert),
a propozycja odbioru osobistego pojawia się tylko, gdy oferta jest w Twoim promieniu odbioru.
"""
from __future__ import annotations

import math
import re

from .catalog import format_storage
from .models import Defect, Offer, RedFlag, Valuation, Verdict
from .text import any_match, normalize, phrase

TEMPLATE_KEYS = ("buy", "negotiate", "negotiate_concrete", "negotiate_pickup", "verify")
NEGOTIATION_STYLES = {  # styl → szablon
    "polite": "negotiate",
    "concrete": "negotiate_concrete",
    "pickup": "negotiate_pickup",
}
STYLE_NAMES = {"polite": "uprzejmy", "concrete": "konkretny", "pickup": "szybki odbiór"}
TEMPLATE_NAMES = {
    "buy": "Kupuję (KUPUJ)",
    "negotiate": "Negocjacja — uprzejmy (NEGOCJUJ)",
    "negotiate_concrete": "Negocjacja — konkretny (NEGOCJUJ)",
    "negotiate_pickup": "Negocjacja — szybki odbiór (NEGOCJUJ)",
    "verify": "Pytania przed zakupem (DO WERYFIKACJI)",
}
# ile argumentów wstawić, żeby wiadomość miała 3–5 zdań
MAX_ARGUMENTS = {"negotiate": 2, "negotiate_concrete": 2, "negotiate_pickup": 1}

DEFAULT_TEMPLATES = {
    "buy": (
        "Dzień dobry,\n"
        "interesuje mnie {telefon} za {cena}. Czy jest jeszcze dostępny? Mogę kupić od razu. {wysylka}\n"
        "Pozdrawiam"
    ),
    "negotiate": (
        "Dzień dobry, interesuje mnie {telefon} z Pana/Pani ogłoszenia. {argumenty}\n"
        "Czy zgodzi się Pan/Pani na {propozycja}? {odbior}\n"
        "Pozdrawiam serdecznie"
    ),
    "negotiate_concrete": (
        "Dzień dobry, piszę w sprawie: {telefon}. {argumenty}\n"
        "Proponuję {propozycja}. {odbior}\n"
        "Pozdrawiam"
    ),
    "negotiate_pickup": (
        "Dzień dobry, interesuje mnie {telefon}. {odbior} {argumenty}\n"
        "Czy za {propozycja} możemy się umówić?\n"
        "Pozdrawiam"
    ),
    "verify": (
        "Dzień dobry,\n"
        "interesuje mnie {telefon} za {cena}. Zanim kupię, mam kilka pytań:\n"
        "{pytania}\n"
        "Z góry dziękuję i pozdrawiam"
    ),
}

# szablon „negotiate” z wersji 1.4–1.6 — przy aktualizacji zastępowany nowym stylem uprzejmym
OLD_NEGOTIATE_TEMPLATE = (
    "Dzień dobry,\n"
    "interesuje mnie {telefon}. {argumenty}\n"
    "Czy cena {propozycja} byłaby do przyjęcia? Mogę kupić od razu. {wysylka}\n"
    "Pozdrawiam"
)

PLACEHOLDERS = {
    "telefon": "model z pamięcią, np. „iPhone 13 128 GB”",
    "model": "sam model, np. „iPhone 13”",
    "pamięć": "pamięć, np. „128 GB” (puste, gdy nieznana; można też pisać {pamiec})",
    "cena": "cena z ogłoszenia",
    "propozycja": "Twoja cena otwierająca (z rekomendacji negocjacji)",
    "argumenty": "prawdziwe argumenty: usterki z kosztem naprawy, bateria, rysy, brak pudełka, ceny podobnych ofert",
    "odbior": "szybki odbiór osobisty i gotówka (gdy oferta jest w promieniu odbioru) albo szybka płatność "
              "i wysyłka",
    "pytania": "pytania dopasowane do oferty (blokady, bateria, usterki, pamięć…)",
    "wysylka": "prośba o wysyłkę albo informacja o odbiorze osobistym",
}

_FIELD = re.compile(r"\{(\w+)\}")


def template_for(verdict: Verdict, val: Valuation | None = None, style: str = "polite") -> str:
    """Domyślny szablon dla werdyktu: KUPUJ → kupno, DO WERYFIKACJI → pytania, pozostałe → negocjacja
    w wybranym stylu (uprzejmy / konkretny / szybki odbiór)."""
    if verdict is Verdict.BUY:
        return "buy"
    if verdict is Verdict.VERIFY:
        return "verify"
    return NEGOTIATION_STYLES.get(style, "negotiate")


def compose(offer: Offer, val: Valuation, templates: dict[str, str], *, key: str | None = None,
            style: str = "polite", pickup_km: float = 0) -> tuple[str, str]:
    """Gotowa wiadomość dla oferty: (klucz szablonu, tekst). Pusty szablon w ustawieniach → domyślny."""
    key = key or template_for(val.verdict, val, style)
    template = templates.get(key) or DEFAULT_TEMPLATES[key]
    return key, render(template, offer, val, key=key, pickup_km=pickup_km)


def zl(value: float) -> str:
    return f"{value:,.0f} zł".replace(",", " ")


def round_offer(value: float) -> float:
    """Cena otwierająca zaokrąglona w dół do 10 zł (okrągłe kwoty lepiej wyglądają w negocjacji)."""
    return float(math.floor(value / 10) * 10)


def phone_name(offer: Offer) -> str:
    p = offer.parsed
    name = p.model or "iPhone"
    return f"{name} {format_storage(p.storage_gb)}" if p.storage_gb else name


_SCRATCHES = (phrase(r"\b(?:rys[aky]?\b|rysk\w*|zarysowan\w*|przetarc\w*|otarc\w*|slady uzytkowania)"),)
_NO_BOX = (phrase(r"\b(?:bez|brak\w*|nie mam|nie posiadam) (?:oryginalnego |orginalnego )?"
                  r"(?:pudel\w*|pudl\w*|opakowan\w*|kartonu|kartonik\w*)", negatable=False),
           phrase(r"\b(?:sam telefon|tylko telefon)\b", negatable=False))


def _text(offer: Offer) -> str:
    return normalize(f"{offer.raw.title} | {offer.raw.description or ''}")


def has_scratches(offer: Offer) -> bool:
    """Rysy / ślady użytkowania wymienione w ogłoszeniu („bez rys” się nie liczy)."""
    return any_match(_SCRATCHES, _text(offer))


def no_box(offer: Offer) -> bool:
    """Sprzedający sam pisze, że nie ma pudełka (albo sprzedaje „sam telefon”)."""
    return any_match(_NO_BOX, _text(offer))


def can_pickup(offer: Offer, pickup_km: float) -> bool:
    """Odbiór osobisty ma sens: oferta w promieniu odbioru (znana odległość)."""
    return offer.distance_km is not None and offer.distance_km <= pickup_km and pickup_km > 0


def pickup_line(offer: Offer, pickup_km: float) -> str:
    """Szybki odbiór i gotówka — tylko gdy to realne; inaczej szybka płatność i wysyłka."""
    if can_pickup(offer, pickup_km):
        return "Mogę szybko przyjechać po telefon i zapłacić gotówką przy odbiorze."
    if offer.raw.shipping_available is False:
        return "Mogę zapłacić od razu po ustaleniu szczegółów odbioru."
    return "Mogę zapłacić od razu, a telefon proszę wysłać paczkomatem."


def arguments(offer: Offer, val: Valuation) -> list[str]:
    """Rzeczowe argumenty do negocjacji — tylko takie, które wynikają z ogłoszenia i wyceny.

    Kolejność = siła argumentu (usterki z kosztem naprawy, bateria, części, rysy, pudełko, rynek)."""
    p = offer.parsed
    out: list[str] = []
    # koszt części z tabeli; pozycje „nieznany koszt (ryzyko)” to tylko zabezpieczenie wyceny — bez kwoty
    costs = {item.label.split(" (")[0].split(" –")[0]: item.amount for item in val.repair_items
             if "nieznany koszt" not in item.label}
    for d in p.defects:
        if d is Defect.BATTERY:
            continue
        cost = costs.get(d.label)
        out.append(f"{d.label} do naprawy" + (f" — to koszt ok. {zl(cost)}." if cost else "."))
    if p.battery_health and p.battery_health < 85:
        out.append(f"Kondycja baterii {p.battery_health}% — bateria nadaje się do wymiany.")
    elif Defect.BATTERY in p.defects:
        out.append("Bateria nadaje się do wymiany.")
    if RedFlag.NON_ORIGINAL_PARTS in p.flags:
        out.append("Telefon ma nieoryginalne części, co obniża jego wartość przy odsprzedaży.")
    if has_scratches(offer) and Defect.SCREEN not in p.defects and Defect.HOUSING not in p.defects:
        out.append("Jak Pan/Pani pisze, telefon ma rysy, a to obniża cenę przy odsprzedaży.")
    if no_box(offer):
        out.append("Telefon jest bez pudełka, co też obniża jego wartość.")
    market = val.market.value
    if market and market < offer.price:
        out.append(f"Podobne egzemplarze sprzedają się za ok. {zl(round_offer(market))}.")
    return out


def questions(offer: Offer, val: Valuation) -> list[str]:
    """Pytania przed zakupem — dopasowane do tego, czego w ogłoszeniu brakuje albo co budzi wątpliwość."""
    p, flags = offer.parsed, set(val.flags)
    out: list[str] = []
    if RedFlag.AI_PHOTO_CONFLICT in flags or RedFlag.AI_TEXT_CONFLICT in flags or RedFlag.AI_DESC_CONFLICT in flags:
        out.append("Czy sprzedaje Pan/Pani cały telefon (a nie samo etui, szkło albo pudełko)?")
    if p.storage_gb is None:
        out.append("Jaka jest pojemność pamięci?")
    if p.battery_health is None:
        out.append("Jaka jest kondycja baterii (Ustawienia → Bateria → Kondycja baterii)?")
    out.append("Czy telefon jest wylogowany z Apple ID i nie ma blokady iCloud ani simlocka?")
    if p.defects:
        listed = ", ".join(d.label.lower() for d in p.defects)
        out.append(f"Poza tym, co opisane ({listed}) — czy wszystko działa: Face ID, aparaty, głośniki, ładowanie?")
    else:
        out.append("Czy wszystko działa: Face ID, aparaty, głośniki, ładowanie, zasięg?")
    out.append("Czy telefon był naprawiany albo ma wymieniane części (ekran, bateria)?")
    if RedFlag.PRICE_UNREALISTIC in flags or RedFlag.PROFIT_UNREALISTIC in flags:
        out.append("Czy to oryginalny iPhone? Cena jest wyraźnie niższa niż zwykle.")
    if RedFlag.FOREIGN_SELLER in flags:
        out.append("Czy wysyła Pan/Pani do Polski i ile kosztuje wysyłka?")
    return out


def shipping_line(offer: Offer) -> str:
    if offer.raw.shipping_available is True:
        return "Proszę o wysyłkę (np. paczkomat InPost)."
    if offer.raw.shipping_available is False:
        return "Mogę odebrać osobiście."
    return "Czy możliwa jest wysyłka (np. paczkomat InPost)?"


def opening_price(offer: Offer, val: Valuation) -> float:
    neg = val.negotiation
    base = neg.opening_price or neg.max_price or val.max_buy_price or offer.price
    return round_offer(min(base, offer.price))


def values(offer: Offer, val: Valuation, *, max_args: int | None = None, pickup_km: float = 0) -> dict[str, str]:
    args = arguments(offer, val)
    if max_args is not None:
        args = args[:max_args]
    storage = format_storage(offer.parsed.storage_gb) if offer.parsed.storage_gb else ""
    return {
        "telefon": phone_name(offer),
        "model": offer.parsed.model or "iPhone",
        "pamięć": storage,
        "pamiec": storage,
        "odbior": pickup_line(offer, pickup_km),
        "cena": zl(offer.price),
        "propozycja": zl(opening_price(offer, val)),
        "argumenty": " ".join(args),
        "pytania": "\n".join(f"- {q}" for q in questions(offer, val)),
        "wysylka": shipping_line(offer),
    }


def render(template: str, offer: Offer, val: Valuation, *, key: str | None = None, pickup_km: float = 0) -> str:
    """Wstawia dane oferty w miejsce pól {…}. Nieznane pola zostają bez zmian (widać literówkę).

    ``key`` (szablon negocjacji) ogranicza liczbę argumentów, żeby wiadomość miała 3–5 zdań."""
    data = values(offer, val, max_args=MAX_ARGUMENTS.get(key or ""), pickup_km=pickup_km)
    text = _FIELD.sub(lambda m: data.get(m.group(1), m.group(0)), template)
    lines = [re.sub(r"[ \t]{2,}", " ", line).rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()
