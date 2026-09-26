"""Rozpoznawanie języka tytułu ogłoszenia: polski czy obcy (Vinted pokazuje oferty z innych krajów).

Heurystyka bez sieci i bez modeli:
* polskie litery (ą ę ł ś ż…) albo typowo polskie słowa → polski,
* litery innych alfabetów (ě ř ů, ä ö ü ß, ė į ų, é è ç…) → obcy,
* słowa typowe dla ogłoszeń w innych językach („obal”, „prodám”, „Hülle”, „parduodu”, „with”…) → obcy.

Tytuł bez słów (np. „iPhone 13 128GB”) jest „nieokreślony” — o kraju decyduje wtedy profil sprzedawcy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .text import fold_accents

_POLISH_LETTERS = re.compile(r"[ąćęłńśźżĄĆĘŁŃŚŹŻ]")
# litery, których nie ma w polskim (ó jest polskie, więc go tu nie ma)
_FOREIGN_LETTERS = {
    "cs/sk": re.compile(r"[ěřůťďňľĺŕôÉĚŘŮŤĎŇĽĹŔÔ]"),
    "de": re.compile(r"[äöüßÄÖÜ]"),
    "lt/lv": re.compile(r"[ėįųūāēīķļņģĖĮŲŪĀĒĪĶĻŅĢ]"),
    "fr/es/it": re.compile(r"[àâçèêëîïùûÿœñìòÀÂÇÈÊËÎÏÙÛŸŒÑ]"),
    "hu/ro": re.compile(r"[őűășțĂȘȚŐŰ]"),
}
# á é í ú ý — czeskie/słowackie/węgierskie/hiszpańskie; osobno, bo pojedynczo mniej pewne
_ACUTE = re.compile(r"[áéíúýÁÉÍÚÝ]")

# tylko słowa, których NIE używa się w polskich ogłoszeniach (np. bez „bateria”, „telefon”, „defekt”)
_FOREIGN_WORDS: dict[str, frozenset[str]] = {
    "cs/sk": frozenset({
        "obal", "obaly", "obalu", "kryt", "kryty", "krytu", "pouzdro", "puzdro", "prodam", "predam", "novy", "nova",
        "nove", "pouzity", "pouzita", "pouzivany", "zanovni", "funkcni", "funkcny", "funkcna", "stav", "stavu",
        "velmi", "velky", "barva", "farba", "cerna", "cierna", "cerny", "biela", "biely", "modra", "zlata",
        "ruzova", "seda", "stribrna", "strieborna", "sleva", "zaruka", "puvodni", "povodna", "mobil", "mobilni",
        "kapacita", "displej", "nabijecka", "nabijacka", "sklo", "krabice", "krabicka", "prasknuty", "nefunkcni",
        "nefunkcny", "ako",
    }),
    "de": frozenset({
        "und", "mit", "fur", "neu", "neuwertig", "gebraucht", "zustand", "handy", "hulle", "handyhulle",
        "schwarz", "weiss", "gut", "sehr", "akku", "ohne", "inkl", "versand", "ladegerat", "speicher", "farbe",
        "originalverpackung", "ovp",
    }),
    "lt/lv": frozenset({
        "parduodu", "parduodamas", "naujas", "nauja", "naudotas", "naudota", "deklas", "dekliukas", "telefonas",
        "bukle", "spalva", "juodas", "baltas", "geros", "labai", "stiklas", "ikroviklis", "idealios", "pardodu",
        "jauns", "lietots", "melns",
    }),
    "en": frozenset({
        "with", "for", "the", "great", "condition", "brand", "used", "only", "locked", "unlocked", "sealed",
        "excellent", "perfect", "works", "working", "cracked", "broken", "screen", "cover", "charger",
        "wallet", "includes", "new",
    }),
    "fr/es/it": frozenset({
        "avec", "neuf", "tres", "coque", "vendu", "noir", "blanc", "comme", "telephone", "portable", "nuovo",
        "usato", "ottimo", "stato", "colore", "nero", "bianco", "custodia", "nuevo", "usado", "estado", "funda",
        "negro", "blanco", "como", "carcasa",
    }),
}
# angielskie słowa pojawiają się w polskich ogłoszeniach („new”, „sealed”) — potrzeba co najmniej trzech
_MIN_HITS = {"en": 3}

# słowa wyłącznie polskie (z pisownią „sz/cz/rz/w”) — przesądzają, że tytuł jest po polsku
_POLISH_WORDS = frozenset({
    "sprzedam", "sprzedaje", "stan", "stanie", "kondycja", "kondycji", "sprawny", "sprawna", "uszkodzony",
    "uszkodzona", "zbity", "zbita", "pekniety", "peknieta", "gwarancja", "gwarancji", "nowy", "nowa", "uzywany",
    "uzywana", "kolor", "czarny", "bialy", "niebieski", "zielony", "fioletowy", "rozowy", "zloty", "srebrny",
    "grafitowy", "tytan", "polecam", "okazja", "tanio", "pilne", "zamiana", "blokad", "blokady", "oryginalny",
    "oryginalna", "szklo", "ladowarka", "pudelko", "zestaw", "czesci", "wysylka", "dziala", "zadbany", "zadbana",
    "wyswietlacz", "rysy", "bardzo",
})


@dataclass(frozen=True)
class LanguageGuess:
    foreign: bool
    language: str | None = None  # np. "cs/sk"
    evidence: str = ""  # co przesądziło — do pokazania w powodzie odrzucenia


def detect_language(title: str) -> LanguageGuess:
    """Czy tytuł jest napisany w obcym języku (nie po polsku)."""
    if not title:
        return LanguageGuess(False)
    if _POLISH_LETTERS.search(title):
        return LanguageGuess(False)
    for lang, pattern in _FOREIGN_LETTERS.items():
        m = pattern.search(title)
        if m:
            return LanguageGuess(True, lang, f"litera „{m.group(0)}”")
    words = re.findall(r"[a-z]+", fold_accents(title).lower())
    if any(w in _POLISH_WORDS for w in words):
        return LanguageGuess(False)
    for lang, vocab in _FOREIGN_WORDS.items():
        hits = [w for w in words if w in vocab]
        if hits and len(set(hits)) >= _MIN_HITS.get(lang, 1):
            return LanguageGuess(True, lang, "słowo „" + "”, „".join(dict.fromkeys(hits)) + "”")
    m = _ACUTE.search(title)
    if m:
        return LanguageGuess(True, "cs/sk", f"litera „{m.group(0)}”")
    return LanguageGuess(False)
