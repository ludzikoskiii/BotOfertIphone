"""Szablony wiadomości do sprzedającego (bez AI): kupno, negocjacja ceny, pytania przed zakupem.

Szablony edytujesz w Ustawieniach → „Wiadomości”. W tekście można użyć pól w nawiasach klamrowych —
program wstawia dane oferty i wyceny. Wiadomość kopiuje przycisk „📋 Skopiuj wiadomość” w panelu
szczegółów; wysyłasz ją sam, w portalu.
"""
from __future__ import annotations

import math
import re

from .catalog import format_storage
from .models import Defect, Offer, RedFlag, Valuation, Verdict

TEMPLATE_KEYS = ("buy", "negotiate", "verify")
TEMPLATE_NAMES = {
    "buy": "Kupuję (KUPUJ)",
    "negotiate": "Negocjacja ceny (NEGOCJUJ)",
    "verify": "Pytania przed zakupem (DO WERYFIKACJI)",
}

DEFAULT_TEMPLATES = {
    "buy": (
        "Dzień dobry,\n"
        "interesuje mnie {telefon} za {cena}. Czy jest jeszcze dostępny? Mogę kupić od razu. {wysylka}\n"
        "Pozdrawiam"
    ),
    "negotiate": (
        "Dzień dobry,\n"
        "interesuje mnie {telefon}. {argumenty}\n"
        "Czy cena {propozycja} byłaby do przyjęcia? Mogę kupić od razu. {wysylka}\n"
        "Pozdrawiam"
    ),
    "verify": (
        "Dzień dobry,\n"
        "interesuje mnie {telefon} za {cena}. Zanim kupię, mam kilka pytań:\n"
        "{pytania}\n"
        "Z góry dziękuję i pozdrawiam"
    ),
}

PLACEHOLDERS = {
    "telefon": "model z pamięcią, np. „iPhone 13 128 GB”",
    "model": "sam model, np. „iPhone 13”",
    "pamiec": "pamięć, np. „128 GB” (puste, gdy nieznana)",
    "cena": "cena z ogłoszenia",
    "propozycja": "Twoja cena otwierająca (z rekomendacji negocjacji)",
    "argumenty": "argumenty do negocjacji: usterki z kosztem naprawy, bateria, ceny podobnych ofert",
    "pytania": "pytania dopasowane do oferty (blokady, bateria, usterki, pamięć…)",
    "wysylka": "prośba o wysyłkę albo informacja o odbiorze osobistym",
}

_FIELD = re.compile(r"\{(\w+)\}")


def template_for(verdict: Verdict, val: Valuation | None = None) -> str:
    """Domyślny szablon dla werdyktu: KUPUJ → kupno, DO WERYFIKACJI → pytania, pozostałe → negocjacja."""
    if verdict is Verdict.BUY:
        return "buy"
    if verdict is Verdict.VERIFY:
        return "verify"
    return "negotiate"


def zl(value: float) -> str:
    return f"{value:,.0f} zł".replace(",", " ")


def round_offer(value: float) -> float:
    """Cena otwierająca zaokrąglona w dół do 10 zł (okrągłe kwoty lepiej wyglądają w negocjacji)."""
    return float(math.floor(value / 10) * 10)


def phone_name(offer: Offer) -> str:
    p = offer.parsed
    name = p.model or "iPhone"
    return f"{name} {format_storage(p.storage_gb)}" if p.storage_gb else name


def arguments(offer: Offer, val: Valuation) -> list[str]:
    """Rzeczowe argumenty do negocjacji — tylko takie, które wynikają z ogłoszenia i wyceny."""
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


def values(offer: Offer, val: Valuation) -> dict[str, str]:
    args = arguments(offer, val)
    return {
        "telefon": phone_name(offer),
        "model": offer.parsed.model or "iPhone",
        "pamiec": format_storage(offer.parsed.storage_gb) if offer.parsed.storage_gb else "",
        "cena": zl(offer.price),
        "propozycja": zl(opening_price(offer, val)),
        "argumenty": " ".join(args),
        "pytania": "\n".join(f"- {q}" for q in questions(offer, val)),
        "wysylka": shipping_line(offer),
    }


def render(template: str, offer: Offer, val: Valuation) -> str:
    """Wstawia dane oferty w miejsce pól {…}. Nieznane pola zostają bez zmian (widać literówkę)."""
    data = values(offer, val)
    text = _FIELD.sub(lambda m: data.get(m.group(1), m.group(0)), template)
    lines = [re.sub(r"[ \t]{2,}", " ", line).rstrip() for line in text.splitlines()]
    return "\n".join(lines).strip()
