"""Oszustwo „sprzedaż zdjęcia iPhone'a zamiast telefonu”.

Ogłoszenie wygląda jak oferta telefonu (model w tytule, niska cena), ale przedmiotem sprzedaży jest zdjęcie,
wydruk, plakat albo obrazek telefonu — zwykle zaznaczone drobnym dopiskiem na końcu opisu.

Sygnały pewne (każdy wystarczy):
* kategoria portalu typu „Sztuka > Fotografia”, „Obrazy i plakaty”, „Grafiki” (nazwa albo ID z ustawień),
* jednoznaczne sformułowanie: „to jest tylko zdjęcie”, „przedmiotem sprzedaży jest zdjęcie”, „photo only”,
  „nur Foto”, „jen fotka”, „tik nuotrauka”… — także ukryte: rozstrzelone litery („t y l k o  z d j ę c i e”),
  kropki między literami, emoji 📷/🖼, znaki niewidoczne, małe kapitaliki i indeksy górne,
* słowo „zdjęcie / foto / plakat / obraz…” w tytule PRZED nazwą modelu („Zdjęcie iPhone 15 Pro”).

Sygnały słabe (jeden = DO WERYFIKACJI z wyjaśnieniem):
* takie słowo w tytule PO nazwie modelu, „wydruk / plakat / poster / obrazek” w opisie,
* kategoria ogólna (dekoracje, kolekcje, sztuka, antyki, rękodzieło),
* CLIP: zdjęcie wygląda na wydruk, plakat albo zrzut ekranu ogłoszenia.

Bardzo niska cena + którykolwiek słaby sygnał albo dwa słabe sygnały = pewne wykrycie.

Fałszywe alarmy: słowo „zdjęcie” w zwykłym znaczeniu nic nie znaczy — „więcej zdjęć na priv”, „zdjęcia
prawdziwe”, „stan jak na zdjęciach”, „wyślę zdjęcie telefonu z IMEI” są pomijane (frazy wykluczające i słowa
przed frazą). „Zdjęcia poglądowe” to osobny sygnał ochrony przed oszustwami (``core.fraud``), nie sprzedaż zdjęcia.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache

from .text import normalize

# ------------------------------------------------------------ ustawienia ---


def _strong_phrases() -> list[str]:
    """Jednoznaczne sformułowania (bez polskich znaków; ostatnie słowo może mieć dowolną końcówkę;
    „$” na końcu = fraza musi kończyć zdanie, np. „to nie jest telefon.” ale nie „…telefon firmowy”)."""
    return [
        # polski
        "tylko zdjecie", "zdjecie tylko$", "jedynie zdjecie", "wylacznie zdjecie", "samo zdjecie", "to jest zdjecie",
        "sprzedaje zdjecie", "sprzedam zdjecie", "sprzedaz zdjecia", "sprzedaje fotografie", "sprzedaje wydruk",
        "przedmiotem sprzedazy jest", "przedmiotem aukcji jest", "przedmiotem ogloszenia jest",
        "przedmiotem transakcji jest", "otrzymasz zdjecie", "otrzymujesz zdjecie", "kupujesz zdjecie",
        "kupujacy otrzymuje zdjecie", "nie jest to telefon$", "to nie jest telefon$", "nie sprzedaje telefonu",
        "telefon nie jest przedmiotem", "zdjecie telefonu", "zdjecie iphon", "zdjecie apple iphon",
        "fotografia telefonu", "fotografia iphon", "fotka iphon", "fotka telefonu", "wydruk zdjecia",
        "wydrukowane zdjecie", "zdjecie wydrukowane", "zdjecie na papierze", "wydruk iphon", "obrazek iphon",
        "obrazek telefonu", "plakat iphon", "plakat z iphon", "plakat z telefonem", "grafika iphon",
        # angielski
        "photo only", "only photo", "only the photo", "only a photo", "picture only", "only a picture",
        "only the picture", "just a photo", "just the photo", "just a picture", "this is a photo",
        "this is a picture", "photo of iphon", "photo of the phone", "photo of a phone", "picture of iphon",
        "picture of the phone", "picture of a phone", "printed photo", "print of iphon", "poster of iphon",
        "not a phone$", "not the phone$", "not an actual phone", "you will receive a photo",
        "you are buying a photo", "selling a photo", "selling the photo",
        # niemiecki
        "nur foto", "nur das foto", "nur ein foto", "nur bild", "nur das bild", "nur ein bild", "nur abbildung",
        "nur fotografie", "foto vom iphon", "foto von iphon", "foto des iphon", "bild vom iphon", "bild von iphon",
        "bild des iphon", "poster iphon", "kein handy$", "kein telefon$", "kein iphon", "verkauft wird ein foto",
        "verkaufe ein foto", "verkaufe das foto", "verkauft wird nur",
        # czeski / słowacki
        "pouze fotka", "pouze foto", "jen fotka", "jen foto", "pouze obrazek", "jen obrazek", "fotka iphon",
        "obrazek iphon", "neni to telefon$", "neni telefon$", "prodavam fotku", "prodavam fotografii",
        "len fotka", "iba fotka", "len foto", "iba foto", "len obrazok", "iba obrazok", "nie je to telefon$",
        "predavam fotku", "predavam fotografiu",
        # litewski
        "tik nuotrauka", "tai nuotrauka", "nuotrauka iphon", "parduodu nuotrauka", "ne telefonas$",
        # francuski / włoski / hiszpański
        "seulement la photo", "photo uniquement", "solo foto", "solo la foto", "solo imagen", "solo la imagen",
    ]


def _photo_words() -> list[str]:
    """Słowa „zdjęcie / obraz / plakat” (całe słowa) — w tytule przed modelem: pewny sygnał, po modelu: słaby."""
    return ["zdjecie", "foto", "fotka", "fotografia", "fotografie", "obraz", "obrazek", "plakat", "poster",
            "wydruk", "print", "grafika", "photo", "picture", "bild", "nuotrauka", "obrazok", "fotku", "fotografii"]


def _desc_words() -> list[str]:
    """W opisie — słaby sygnał (słowo „zdjęcie” w opisie jest zbyt częste, więc go tu nie ma)."""
    return ["wydruk", "wydrukowane", "wydrukowany", "plakat", "poster", "obrazek", "printed", "printout"]


def _exclusions() -> list[str]:
    """Frazy, w których słowo o zdjęciu ma zwykłe znaczenie — pomijane (bez polskich znaków, prefiksy)."""
    return [
        "wiecej zdjec", "dodatkowe zdjeci", "kolejne zdjeci", "zdjecia prawdziw", "prawdziwe zdjeci",
        "zdjecia rzeczywist", "zdjecia realn", "realne zdjeci", "zdjecia wlasn", "wlasne zdjeci",
        "zdjecia oryginaln", "oryginalne zdjeci", "zdjecia aktualn", "aktualne zdjeci", "jak na zdjeci",
        "na zdjeci", "ze zdjec", "zdjecia na priv", "zdjecia na pw", "zdjecia na prosb", "zdjecia na zyczenie",
        "zdjecia ekranu", "zdjecie ekranu", "zrzut ekranu", "zdjecia z aparatu", "robi zdjeci", "robi super",
        "robi swietne", "robi piekne", "aparat do zdjec", "zdjecia wykonane", "foto real", "fotos real",
        "real photos", "more photos", "more pictures", "photos on request", "mehr fotos", "mehr bilder",
        "echte fotos", "echte bilder", "fotos auf anfrage", "vice fotek", "dalsi fotky", "viac fotiek",
        "brak obrazu", "bez obrazu", "obraz ok", "obraz dziala", "obraz sprawny", "ramka na zdjeci",
        "etui na zdjeci", "kieszonka na zdjeci", "drukarka do zdjec", "drukarka zdjec",
    ]


def _guard_words() -> list[str]:
    """Słowa tuż PRZED frazą, po których to zwykłe zdanie („wyślę zdjęcie telefonu z IMEI”)."""
    return ["wysle", "wysylam", "przesle", "przesylam", "podesle", "podsylam", "dodaje", "dodam", "dodalem",
            "wstawie", "wstawiam", "zalaczam", "moge", "chetnie", "wiecej", "kolejne", "dodatkowe", "zrobie",
            "send", "more", "additional", "schicke", "sende", "poslu", "poslem", "nie", "not", "nicht"]


def _stock_phrases() -> list[str]:
    """„Zdjęcia poglądowe” — podejrzane (sygnał ochrony przed oszustwami), ale NIE sprzedaż zdjęcia."""
    return ["zdjecia pogladow", "zdjecie pogladow", "zdjecia ilustracyjn", "zdjecie ilustracyjn",
            "zdjecia przykladow", "zdjecie przykladow", "zdjecia z internetu", "zdjecie z internetu",
            "zdjecia ze strony producenta", "zdjecia producenta", "foto pogladowe", "fotka pogladowa",
            "stock photo", "photos for illustration", "illustration only", "symbolbild", "beispielbild",
            "beispielfoto", "ilustracni foto", "ilustracne foto"]


def _strong_categories() -> list[str]:
    """Ostatni poziom kategorii portalu (fragment nazwy) → pewny sygnał."""
    return ["zdjecia", "fotografi", "obrazy", "plakat", "grafik", "reprodukc", "wydruk", "pocztowk", "poster",
            "prints", "bilder", "obrazky", "fotky", "nuotrauk"]


def _weak_categories() -> list[str]:
    """Kategoria ogólna (fragment nazwy na dowolnym poziomie) → słaby sygnał (kolekcjonerski iPhone jest możliwy)."""
    return ["dekoracj", "kolekc", "sztuk", "antyk", "rekodziel", "design", "gadzet", "pamiatk", "art"]


def _equipment_words() -> list[str]:
    """Kategorie sprzętu fotograficznego („Elektronika > Fotografia”) — to NIE są zdjęcia na sprzedaż."""
    return ["aparat", "obiektyw", "elektronik", "akcesoria fotograficzne", "drukark", "kamer"]


def _category_ids() -> dict[str, dict[str, list[str]]]:
    """ID kategorii sprawdzone na portalach (sonda probe-photo-categories, wrzesień 2026)."""
    return {
        # Allegro Lokalnie: Kolekcje i sztuka > Sztuka > Fotografia (321811) — pewne;
        # Sztuka (321902), Kolekcje (6), Design i antyki (26013), Rękodzieło (76593) — słabe
        "allegro_lokalnie": {"strong": ["321811"], "weak": ["321902", "6", "26013", "76593"]},
    }


@dataclass
class PhotoScamConfig:
    """Ustawienia wykrywania (Ustawienia → Oszustwa → Sprzedaż zdjęcia zamiast telefonu)."""

    enabled: bool = True
    strong_phrases: list[str] = field(default_factory=_strong_phrases)
    photo_words: list[str] = field(default_factory=_photo_words)
    desc_words: list[str] = field(default_factory=_desc_words)
    exclusions: list[str] = field(default_factory=_exclusions)
    guard_words: list[str] = field(default_factory=_guard_words)
    stock_phrases: list[str] = field(default_factory=_stock_phrases)
    strong_categories: list[str] = field(default_factory=_strong_categories)
    weak_categories: list[str] = field(default_factory=_weak_categories)
    equipment_words: list[str] = field(default_factory=_equipment_words)
    category_ids: dict[str, dict[str, list[str]]] = field(default_factory=_category_ids)
    clip_threshold: float = 0.80  # CLIP: wydruk / plakat / zrzut ekranu — od tej pewności (słaby sygnał)
    cheap_ratio: float = 0.45  # „bardzo niska cena” — poniżej tej części wartości rynkowej

    def key(self) -> tuple:
        """Do pamięci podręcznej: zmiana list = nowe wzorce."""
        return (tuple(self.strong_phrases), tuple(self.photo_words), tuple(self.desc_words),
                tuple(self.exclusions), tuple(self.guard_words), tuple(self.stock_phrases))


# ------------------------------------------------------------ ukryty tekst ---

_INVISIBLE = re.compile(r"[­​-‏‪-‮⁠-⁤﻿]")
_EMOJI_PHOTO = re.compile(r"[\U0001F4F7\U0001F4F8\U0001F5BC\U0001F39E\U0001F3A8]️?")
_SMALL_CAPS = str.maketrans(dict(zip("ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘʀꜱᴛᴜᴠᴡʏᴢ", "abcdefghijklmnoprstuvwyz", strict=True)))
# cyrylica udająca łacinę („zdjеcie” z cyrylickim „е”)
_HOMOGLYPHS = str.maketrans({"а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x", "і": "i",
                             "ј": "j", "ѕ": "s", "к": "k", "м": "m", "т": "t", "н": "h", "в": "b", "А": "a",
                             "Е": "e", "О": "o", "Р": "p", "С": "c", "Т": "t", "К": "k", "М": "m", "Н": "h"})
# pojedyncze litery rozdzielone spacją/kropką/myślnikiem/gwiazdką: „t y l k o”, „z.d.j.ę.c.i.e”
_LETTER = r"[^\W\d_]"
_SPACED = re.compile(rf"(?<![^\W\d_]){_LETTER}(?:[ .\-_*·•~+]{{1,3}}{_LETTER}(?![^\W\d_])){{3,}}")


def unhide(text: str) -> tuple[str, bool]:
    """Tekst z odkrytymi ukrytymi dopiskami + czy coś było ukryte."""
    if not text:
        return "", False
    before = text
    s = _INVISIBLE.sub("", text)
    s = _EMOJI_PHOTO.sub(" zdjecie ", s)
    s = s.translate(_SMALL_CAPS).translate(_HOMOGLYPHS)
    s = unicodedata.normalize("NFKC", s)  # indeksy górne, litery pełnej szerokości → zwykłe

    def collapse(m: re.Match) -> str:
        run = m.group(0)
        words = re.split(r"(?:\s{2,}|\s*[/|]\s*)", run)  # podwójna spacja = granica słowa
        return " " + " ".join("".join(ch for ch in w if ch.isalpha()) for w in words) + " "

    s = _SPACED.sub(collapse, s)
    return s, s != before


# ------------------------------------------------------------ dopasowania ---

@dataclass
class _Rules:
    strong: list[tuple[str, re.Pattern[str], bool]]  # (fraza, wzorzec, musi kończyć zdanie)
    by_first: dict[str, list[int]]  # pierwsze słowo frazy → numery fraz (szybki test wstępny bez regexów)
    single: list[int]  # frazy jednowyrazowe (prefiks — sprawdzane zawsze)
    compact: list[tuple[str, str]]  # (fraza, fraza bez spacji) — do rozstrzelonych liter
    photo_words: set[str]
    desc_words: set[str]
    exclusions: re.Pattern[str] | None
    guard: set[str]
    stock: re.Pattern[str] | None


def _alt(items: list[str]) -> re.Pattern[str] | None:
    items = [normalize(x) for x in items if normalize(x)]
    if not items:
        return None
    return re.compile(r"(?<![a-z0-9])(?:" + "|".join(re.escape(x).replace(r"\ ", r"\s+") for x in items) + ")")


@lru_cache(maxsize=8)
def _rules(key: tuple) -> _Rules:
    strong_list, photo, desc, excl, guard, stock = key
    strong = []
    compact = []
    for raw in strong_list:
        end = raw.strip().endswith("$")
        ph = normalize(raw.strip().rstrip("$"))
        if not ph:
            continue
        pat = re.compile(r"(?<![a-z0-9])" + re.escape(ph).replace(r"\ ", r"\s+"))
        strong.append((ph, pat, end))
        compact.append((ph, ph.replace(" ", "")))
    by_first: dict[str, list[int]] = {}
    single = []
    for i, (ph, _, _) in enumerate(strong):
        words = ph.split()
        if len(words) == 1:
            single.append(i)
        else:
            by_first.setdefault(words[0], []).append(i)
    return _Rules(strong, by_first, single, compact, {normalize(w) for w in photo if normalize(w)},
                  {normalize(w) for w in desc if normalize(w)}, _alt(list(excl)),
                  {normalize(w) for w in guard if normalize(w)}, _alt(list(stock)))


def _excluded_spans(text: str, r: _Rules) -> list[tuple[int, int]]:
    return [(m.start(), m.end() + 12) for m in r.exclusions.finditer(text)] if r.exclusions else []


def _inside(pos: int, spans: list[tuple[int, int]]) -> bool:
    return any(a <= pos < b for a, b in spans)


_NEGATIONS = {"nie", "not", "nicht", "kein", "neni", "ne"}
_STOCK_NEAR = re.compile(r"(?<![a-z])(?:zdjeci|zdjec|foto|fotk)\w*(?:\s+\S+){0,3}?\s+(?:pogladow|ilustracyjn|przykladow)\w*")


def _guarded(text: str, start: int, r: _Rules) -> bool:
    """Słowo tuż przed frazą (w tym samym zdaniu) zmienia sens: „wyślę zdjęcie telefonu”, „to nie tylko zdjęcie”."""
    before: list[str] = []
    for w in reversed(text[:start].split()):
        if w == "|":
            break
        before.append(w)
        if len(before) == 3:
            break
    # przeczenie sięga 3 słów („nie jest to tylko zdjęcie”), pozostałe słowa — 2 („wyślę zdjęcie”)
    return any(w in r.guard for w in before[:2]) or any(w in _NEGATIONS for w in before)


def _sentence_end_after(text: str, end: int) -> bool:
    nxt = text[end:].split()
    return not nxt or nxt[0] == "|" or nxt[0] in ("tylko", "lecz", "ale", "a", "only", "but", "sondern", "nur")


def strong_phrase(text: str, r: _Rules) -> str | None:
    words = set(text.split())
    candidates = sorted({i for w in words & r.by_first.keys() for i in r.by_first[w]} | set(r.single))
    if not candidates:
        return None  # typowy przypadek: żadna fraza nie może wystąpić
    spans = _excluded_spans(text, r)
    for i in candidates:
        _ph, pat, needs_end = r.strong[i]
        for m in pat.finditer(text):
            if _inside(m.start(), spans) or _guarded(text, m.start(), r):
                continue
            # dokończenie słowa (prefiks): „zdjecie iphon” → „zdjecie iphonea”
            end = m.end()
            while end < len(text) and text[end].isalnum():
                end += 1
            if needs_end and not _sentence_end_after(text, end):
                continue
            after = text[end:end + 40]
            if re.match(r"(?:\s+\S{1,2})?\s*(?:\|\s*)?(?:pogladow|ilustracyjn|przykladow)", after):
                continue  # „zdjęcie iPhone'a poglądowe” — to „zdjęcia poglądowe”, nie sprzedaż zdjęcia
            return text[m.start():end]
    return None


def compact_phrase(runs: list[str], r: _Rules) -> str | None:
    """Rozstrzelone litery sklejone w jeden ciąg: „tylkozdjecie” → fraza „tylko zdjecie”."""
    for run in runs:
        c = re.sub(r"[^a-z]", "", normalize(run))
        for ph, comp in r.compact:
            if len(comp) >= 8 and comp in c:
                return ph
    return None


# ------------------------------------------------------------------- wynik ---

@dataclass
class PhotoScamResult:
    strong: list[str] = field(default_factory=list)  # powody pewne
    weak: list[str] = field(default_factory=list)  # powody słabe
    stock_photos: str | None = None  # „zdjęcia poglądowe” (sygnał ochrony przed oszustwami)
    hidden: bool = False  # fraza była ukryta (rozstrzelone litery, znaki niewidoczne, emoji)
    keyword: str | None = None

    @property
    def level(self) -> str:
        if self.strong or len(self.weak) >= 2:
            return "certain"
        return "weak" if self.weak else "none"

    def with_price(self, price: float, market: float | None, cfg: PhotoScamConfig) -> PhotoScamResult:
        """Bardzo niska cena + słaby sygnał = pewne wykrycie."""
        if self.weak and not self.strong and market and price < cfg.cheap_ratio * market:
            return PhotoScamResult([f"bardzo niska cena ({price / market:.0%} wartości rynkowej) i "
                                    + "; ".join(self.weak)], self.weak, self.stock_photos, self.hidden, self.keyword)
        return self

    def reason(self) -> str:
        why = self.strong or self.weak
        return "sprzedaż zdjęcia zamiast telefonu — " + "; ".join(why[:3])


def _category_signal(category: str | None, category_id: str | None, source: str | None,
                     cfg: PhotoScamConfig) -> tuple[str | None, str | None]:
    """(powód pewny, powód słaby) z kategorii portalu."""
    ids = cfg.category_ids.get(source or "", {})
    cat_ids = {str(category_id)} if category_id else set()
    if category:
        cat_ids |= set(re.findall(r"-(\d+)(?:\b|$)", category))
    if cat_ids & set(map(str, ids.get("strong", []))):
        return f"kategoria portalu „{category or category_id}” (zdjęcia / sztuka)", None
    strong_hit = weak_hit = None
    if category:
        segments = [normalize(p) for p in re.split(r"[>/»]", category) if normalize(p)]
        path = " ".join(segments)
        last = segments[-1] if segments else ""
        equipment = any(normalize(w) in path for w in cfg.equipment_words if normalize(w))
        if not equipment and any(normalize(w) in last for w in cfg.strong_categories if normalize(w)):
            strong_hit = f"kategoria portalu „{category}” (zdjęcia / obrazy / plakaty)"
        elif not equipment and any(re.search(rf"(?<![a-z]){re.escape(normalize(w))}", path)
                                   for w in cfg.weak_categories if normalize(w)):
            weak_hit = f"kategoria portalu „{category}” (dekoracje / kolekcje / sztuka)"
    if not strong_hit and not weak_hit and cat_ids & set(map(str, ids.get("weak", []))):
        weak_hit = f"kategoria portalu „{category or category_id}” (kolekcje / sztuka)"
    return strong_hit, weak_hit


_MODEL = re.compile(r"^(?:iphone|iphon|ajfon|apple)")


_CONFIGS: dict[tuple, PhotoScamConfig] = {}


def config_fingerprint(cfg: PhotoScamConfig) -> tuple:
    return (cfg.enabled, cfg.key(), tuple(cfg.strong_categories), tuple(cfg.weak_categories),
            tuple(cfg.equipment_words), repr(sorted(cfg.category_ids.items())), cfg.clip_threshold)


def detect(title: str, description: str | None, cfg: PhotoScamConfig, *, category: str | None = None,
           category_id: str | None = None, source: str | None = None,
           clip_score: float | None = None, fingerprint: tuple | None = None) -> PhotoScamResult:
    """Sygnały z tekstu, kategorii i (opcjonalnie) zdjęcia. Cenę dokłada ``with_price``.

    Wynik zapamiętany (te same dane = ten sam wynik) — odświeżenie listy nie powtarza wyrażeń regularnych.
    ``fingerprint`` — ``config_fingerprint(cfg)`` policzony raz na przebieg (przy wielu ofertach)."""
    if not cfg.enabled:
        return PhotoScamResult()
    fp = fingerprint or config_fingerprint(cfg)
    _CONFIGS.setdefault(fp, cfg)
    cached = _detect_cached(fp, title or "", description or "", category, None if category_id is None
                            else str(category_id), source, None if clip_score is None else round(clip_score, 3))
    return PhotoScamResult(list(cached.strong), list(cached.weak), cached.stock_photos, cached.hidden,
                           cached.keyword)


@lru_cache(maxsize=20000)
def _detect_cached(fp: tuple, title: str, description: str, category: str | None, category_id: str | None,
                   source: str | None, clip_score: float | None) -> PhotoScamResult:
    return _detect(title, description, _CONFIGS[fp], category=category, category_id=category_id, source=source,
                   clip_score=clip_score)


def _detect(title: str, description: str | None, cfg: PhotoScamConfig, *, category: str | None = None,
            category_id: str | None = None, source: str | None = None,
            clip_score: float | None = None) -> PhotoScamResult:
    res = PhotoScamResult()
    r = _rules(cfg.key())
    t_raw, t_hidden = unhide(title or "")
    d_raw, d_hidden = unhide(description or "")
    t, d = normalize(t_raw), normalize(d_raw)
    # --- kategoria ---
    strong_cat, weak_cat = _category_signal(category, category_id, source, cfg)
    if strong_cat:
        res.strong.append(strong_cat)
    if weak_cat:
        res.weak.append(weak_cat)
    # --- jednoznaczne sformułowania (tytuł i opis, także ukryte) ---
    for text, where, hidden in ((t, "tytuł", t_hidden), (d, "opis", d_hidden)):
        hit = strong_phrase(text, r)
        if hit:
            res.strong.append(f"{where}: „{hit}”")
            res.keyword = res.keyword or hit
            res.hidden = res.hidden or hidden
            break
    if not res.strong:
        runs = [m.group(0) for m in _SPACED.finditer((title or "") + "\n" + (description or ""))]
        hit = compact_phrase(runs, r)
        if hit:
            res.strong.append(f"ukryty dopisek rozstrzelonymi literami: „{hit}”")
            res.keyword, res.hidden = hit, True
    # --- słowo o zdjęciu w tytule: przed modelem = pewne, po modelu = słabe ---
    spans = _excluded_spans(t, r)
    tokens = [(m.group(0), m.start()) for m in re.finditer(r"[a-z0-9]+", t)]
    model_at = next((i for i, (tok, _) in enumerate(tokens) if _MODEL.match(tok)), None)
    for i, (tok, pos) in enumerate(tokens):
        if tok not in r.photo_words or _inside(pos, spans):
            continue
        prev = [x for x, _ in tokens[max(0, i - 2):i]]
        if any(p in ("na", "do", "z", "ze", "for", "fur") or p.startswith(("ramk", "etui", "drukark", "kiesz"))
               for p in prev):
            continue  # „etui z kieszonką na zdjęcie”, „ramka na zdjęcie” — akcesorium, nie ta sprawa
        if model_at is not None and i < model_at and not res.strong:
            res.strong.append(f"tytuł zaczyna się od „{tok}” przed nazwą modelu")
            res.keyword = res.keyword or tok
        elif model_at is not None and i > model_at:
            res.weak.append(f"w tytule słowo „{tok}”")
            res.keyword = res.keyword or tok
        break
    # --- opis: wydruk / plakat / obrazek (słabe) ---
    dspans = _excluded_spans(d, r)
    for m in re.finditer(r"[a-z0-9]+", d):
        if m.group(0) in r.desc_words and not _inside(m.start(), dspans) and not _guarded(d, m.start(), r):
            res.weak.append(f"w opisie słowo „{m.group(0)}”")
            break
    # --- zdjęcie (CLIP) ---
    if clip_score is not None and clip_score >= cfg.clip_threshold:
        res.weak.append(f"zdjęcie wygląda na wydruk, plakat albo zrzut ekranu (CLIP {clip_score:.0%})")
    # --- „zdjęcia poglądowe” (osobny sygnał) ---
    joined = t + " | " + d
    m = (r.stock.search(joined) if r.stock else None) or _STOCK_NEAR.search(joined)
    if m:
        res.stock_photos = m.group(0)
    return res
