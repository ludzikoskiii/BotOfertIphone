"""Wielostopniowy filtr ogłoszeń: odrzuca akcesoria, części, ogłoszenia „kupię” i śmieci.

Etapy (w kolejności):
1. **kategoria portalu** — gdy portal ją podaje: akcesoria/części → odrzuć, telefony → przepuść dalej;
2. **ogłoszenie kupna/zamiany** — „kupię”, „szukam”, „zamienię” (ale nie „sprzedam lub zamienię”);
3. **akcesorium lub część** — ocena KONTEKSTU, nie samego słowa:
   „Etui do iPhone 13” → akcesorium, „iPhone 13 128GB + etui gratis” → telefon,
   „Sam wyświetlacz iPhone 11” → część, „iPhone 11 zbity wyświetlacz” → telefon;
4. **wymagany model** — tytuł musi zawierać rozpoznawalny model iPhone'a;
5. **test ceny** (``check_price``) — cena poniżej np. 15% mediany rynkowej: ogłoszenie jest
   sprawdzane dokładniej (opis); jeśli opis wskazuje na akcesorium — odrzucenie,
   w przeciwnym razie oferta zostaje, ale z flagą „cena nierealnie niska — sprawdź”.

Listy słów kluczowych są w ustawieniach (``ListingFilterConfig``) i można je edytować.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .text import normalize

# --------------------------------------------------------------- konfiguracja ---


def _accessories() -> list[str]:
    return ["etui", "case", "pokrowiec", "futerał", "szkło", "szkiełko", "szkło hartowane", "folia", "ładowarka",
            "ładowarki", "zasilacz", "kabel", "kable", "przewód", "pudełko", "pudełka", "atrapa", "obudowa",
            "pasek", "uchwyt", "powerbank", "magsafe", "słuchawki", "airpods", "rysik", "adapter", "stacja dokująca",
            "pierścień", "popsocket", "naklejka", "skin"]


def _parts() -> list[str]:
    return ["wyświetlacz", "ekran", "lcd", "oled", "płyta główna", "płyta", "bateria", "akumulator", "taśma",
            "flex", "korpus", "ramka", "klapka", "tylna szyba", "szybka", "aparat", "kamera", "głośnik",
            "mikrofon", "port ładowania", "gniazdo", "czytnik", "tacka sim", "szufladka", "zestaw części",
            "części"]


@dataclass
class ListingFilterConfig:
    """Edytowalne listy słów (bez rozróżniania wielkości liter i polskich znaków)."""

    accessory_words: list[str] = field(default_factory=_accessories)
    part_words: list[str] = field(default_factory=_parts)
    wanted_words: list[str] = field(default_factory=lambda: ["kupię", "skupuję", "skup", "szukam", "poszukuję",
                                                             "zamienię", "zamiana", "wymienię"])
    # słowa, po których akcesorium w tytule jest DODATKIEM do telefonu
    addon_markers: list[str] = field(default_factory=lambda: ["+", "z", "ze", "i", "oraz", "wraz", "plus",
                                                              "dodatkowo", "komplet", "zestaw", "gratis",
                                                              "w zestawie", "w komplecie", "razem"])
    # słowa, po których część to JEDYNY przedmiot sprzedaży
    single_part_markers: list[str] = field(default_factory=lambda: ["sam", "sama", "samo", "same", "tylko",
                                                                    "wyłącznie"])
    phone_category_words: list[str] = field(default_factory=lambda: ["telefon", "smartfon", "komórk", "iphone"])
    accessory_category_words: list[str] = field(default_factory=lambda: [
        "akcesori", "etui", "pokrowc", "ładowark", "słuchawk", "części", "obudow", "folie", "szkła", "kable"])
    suspicious_price_ratio: float = 0.15  # poniżej tej części mediany → sprawdź dokładniej


# ------------------------------------------------------------------ wynik ---


@dataclass
class FilterDecision:
    accepted: bool
    stage: str = ""  # category | wanted | accessory | part | model | price
    reason: str = ""
    keyword: str | None = None  # słowo, które przesądziło (do poprawiania list)
    suspicious: bool = False  # zaakceptowana, ale wymaga sprawdzenia (np. podejrzanie niska cena)

    @staticmethod
    def ok() -> FilterDecision:
        return FilterDecision(True)


STAGE_LABELS = {
    "category": "kategoria portalu",
    "wanted": "ogłoszenie kupna/zamiany",
    "accessory": "akcesorium",
    "part": "część zamienna",
    "model": "brak modelu iPhone'a",
    "price": "cena nierealnie niska",
    "manual": "odrzucona ręcznie",
}

# ------------------------------------------------------------ pomocnicze ---

_STORAGE_RE = re.compile(r"\b\d{2,4}\s?(?:gb|g|tb)\b")
_MODEL_RE = re.compile(r"\b(?:iphone|iphon|ajfon)\b|\b(?:1[0-9]|[6-9]|x[sr]?|se)\s?(?:pro|max|plus|mini|\+)")
_PHONE_EVIDENCE = re.compile(
    r"\b(?:telefon\w*|smartfon\w*|sprawn\w*|bateri\w* \d{2,3}|kondycj\w*|\d{2,3}\s?%|face ?id|icloud|"
    r"gwarancj\w*|simlock|dual sim|stan (?:idealny|bardzo dobry|dobry|bdb)|zablokowan\w*|na czesci|"
    r"uszkodzon\w*|zbit\w*|pekniet\w*|nie wlacza|nie laduje|do naprawy|dawca)")
_DAMAGE_NEAR = re.compile(r"(?:zbit|pekniet|rozbit|uszkodzon|popekan|do wymiany|nie dziala|niedziala|nie wlacza|"
                          r"wymienion|wymienian|slab|zuzyt|padniet|pasy|paski|nowa|nowy)\w*")
_TO_PREP = ("do", "na", "dla", "pod", "kompatybiln", "pasuje")


def _phrases(words: list[str]) -> list[list[str]]:
    out = []
    for w in words:
        toks = normalize(w).replace("|", " ").split()
        if toks:
            out.append(toks)
    # dłuższe frazy najpierw („szkło hartowane” przed „szkło”)
    return sorted(out, key=len, reverse=True)


class _PhraseIndex:
    """Frazy pogrupowane po pierwszych 4 literach pierwszego słowa — ``_find`` sprawdza tylko
    frazy, które mogą pasować do danego słowa tytułu (zamiast wszystkich fraz × wszystkich pozycji)."""

    __slots__ = ("phrases", "by_key")

    def __init__(self, phrases: list[list[str]]):
        self.phrases = phrases
        self.by_key: dict[str, list[int]] = {}
        for order, phrase in enumerate(phrases):
            self.by_key.setdefault(phrase[0][:4], []).append(order)


def _word_match(token: str, word: str) -> bool:
    return token == word or (len(word) >= 4 and token.startswith(word[:max(4, len(word) - 2)]))


def _find(tokens: list[str], phrases: list[list[str]] | _PhraseIndex) -> list[tuple[int, int, str]]:
    """Wystąpienia fraz: (start, koniec, fraza). Dopasowanie po początku słowa (odmiana: etui/etuii, ładowarką).

    Kolejność jak w liście fraz (dłuższe najpierw); słowo zajęte przez frazę nie pasuje już do innej.
    """
    index = phrases if isinstance(phrases, _PhraseIndex) else _PhraseIndex(phrases)
    candidates = sorted((order, i) for i, t in enumerate(tokens) for order in index.by_key.get(t[:4], ()))
    found: list[tuple[int, int, str]] = []
    taken: set[int] = set()
    for order, i in candidates:
        phrase = index.phrases[order]
        n = len(phrase)
        if i + n > len(tokens) or any(j in taken for j in range(i, i + n)):
            continue
        if all(_word_match(tokens[i + k], phrase[k]) for k in range(n)):
            found.append((i, i + n, " ".join(phrase)))
            taken.update(range(i, i + n))
    return sorted(found)


def _tokens(title: str) -> list[str]:
    return normalize(title).replace("|", " ").split()


def _first_model_index(tokens: list[str]) -> int | None:
    for i, t in enumerate(tokens):
        if t in ("iphone", "iphon", "ajfon"):
            return i
    return None


def _phone_evidence(text: str) -> int:
    score = 2 if _STORAGE_RE.search(text) else 0
    score += min(3, len(_PHONE_EVIDENCE.findall(text)))
    return score


@dataclass
class _Hit:
    word: str
    kind: str  # accessory | part
    subject: bool  # czy to przedmiot sprzedaży (True) czy dodatek/opis usterki (False)
    why: str


class ListingFilter:
    def __init__(self, config: ListingFilterConfig | None = None, whitelist: set[tuple[str, str]] | None = None):
        self.config = config or ListingFilterConfig()
        self.whitelist = whitelist or set()
        c = self.config
        self._acc = _PhraseIndex(_phrases(c.accessory_words))
        self._parts = _PhraseIndex(_phrases(c.part_words))
        self._wanted = {normalize(w) for w in c.wanted_words}
        self._addon = _PhraseIndex(_phrases(c.addon_markers))
        self._single = {normalize(w) for w in c.single_part_markers}
        self._phone_cat = [normalize(w) for w in c.phone_category_words]
        self._acc_cat = [normalize(w) for w in c.accessory_category_words]

    # ------------------------------------------------------------- etapy ---

    def check_category(self, category: str | None) -> FilterDecision | None:
        """Etap 1: kategoria z portalu. ``None`` = brak rozstrzygnięcia (idź dalej)."""
        if not category:
            return None
        cat = normalize(category)
        acc = next((w for w in self._acc_cat if w and w in cat), None)
        phone = next((w for w in self._phone_cat if w and w in cat), None)
        if acc and not (phone and cat.rfind(phone) > cat.rfind(acc)):
            return FilterDecision(False, "category", f"kategoria portalu „{category}” to akcesoria/części", acc)
        return None

    def check_wanted(self, tokens: list[str]) -> FilterDecision | None:
        """Etap 2: „kupię / szukam / zamienię” — chyba że to też sprzedaż („sprzedam lub zamienię”)."""
        if not tokens:
            return None
        selling = any(t.startswith("sprzeda") for t in tokens)
        hit = next((t for t in tokens[:3] if t in self._wanted), None)
        if hit is None:
            hit = next((t for t in tokens if t in self._wanted and t in ("kupie", "skupuje", "szukam", "poszukuje")),
                       None)
        if hit and not selling:
            return FilterDecision(False, "wanted", f"ogłoszenie kupna lub zamiany („{hit}”)", hit)
        return None

    def _classify_hit(self, tokens: list[str], start: int, end: int, word: str, kind: str,
                      model_idx: int | None, phone_score: int) -> _Hit:
        before = tokens[max(0, start - 3):start]
        after = tokens[end:end + 3]
        after_text = " ".join(after)
        window_before = " ".join(before)
        # dodatek: „+ etui”, „z pudełkiem”, „etui gratis”, „w zestawie ładowarka”
        addon = bool(_find(before[-2:], self._addon)) or bool(re.search(r"\bgratis|\bw zestawie|\bw komplecie",
                                                                                    after_text))
        if kind == "part":
            if before and before[-1] == "na" and word.startswith("czesc"):
                return _Hit(word, kind, False, "cały telefon „na części”")
            if any(t in self._single for t in before[-2:]):
                return _Hit(word, kind, True, f"„{before[-1]} {word}” — sama część")
            # opis usterki: „zbity wyświetlacz”, „wyświetlacz do wymiany”, „bateria 85%”
            near = " ".join(tokens[max(0, start - 4):end + 3])
            if _DAMAGE_NEAR.search(near) or re.search(r"\b\d{2,3}\s?%", near):
                return _Hit(word, kind, False, "opis usterki telefonu")
        if addon:
            return _Hit(word, kind, False, "dodatek do telefonu")
        if after and any(after[0].startswith(p) for p in _TO_PREP):
            return _Hit(word, kind, True, f"„{word} {after[0]} …” — przedmiot sprzedaży")
        if start == 0 or (model_idx is not None and start < model_idx and not window_before.startswith("sprzeda")):
            # słowo przed modelem: „Etui iPhone 13”, „Wyświetlacz iPhone 11”
            return _Hit(word, kind, phone_score < 3, "słowo przed nazwą modelu")
        # po nazwie modelu bez znacznika dodatku — rozstrzyga siła dowodów, że to telefon
        return _Hit(word, kind, phone_score < 2, "słowo po nazwie modelu")

    def check_accessory(self, title: str) -> FilterDecision | None:
        """Etap 3: akcesorium / część jako przedmiot sprzedaży (z oceną kontekstu)."""
        tokens = _tokens(title)
        text = " ".join(tokens)
        model_idx = _first_model_index(tokens)
        phone_score = _phone_evidence(text)
        hits = [self._classify_hit(tokens, s, e, w, "accessory", model_idx, phone_score)
                for s, e, w in _find(tokens, self._acc)]
        part_spans = [x for x in _find(tokens, self._parts)]
        hits += [self._classify_hit(tokens, s, e, w, "part", model_idx, phone_score) for s, e, w in part_spans]
        subjects = [h for h in hits if h.subject]
        if not subjects:
            return None
        h = subjects[0]
        if h.kind == "part" and re.search(r"\b(?:kompletn\w*|caly telefon|cały telefon|telefon)\b", text):
            return None  # „iPhone 11 kompletny, płyta sprawna” — cały telefon
        stage = h.kind
        label = "akcesorium" if h.kind == "accessory" else "pojedyncza część"
        return FilterDecision(False, stage, f"{label}: „{h.word}” ({h.why})", h.word)

    def check(self, title: str, *, model: str | None, category: str | None = None,
              source: str | None = None, source_id: str | None = None) -> FilterDecision:
        """Etapy 1–4 (bez ceny). ``model`` = wynik rozpoznania modelu z normalizera."""
        if source and source_id and (source, source_id) in self.whitelist:
            return FilterDecision.ok()
        tokens = _tokens(title)
        for decision in (self.check_category(category), self.check_wanted(tokens), self.check_accessory(title)):
            if decision is not None:
                return decision
        if not model:
            return FilterDecision(False, "model", "w tytule nie rozpoznano modelu iPhone'a")
        return FilterDecision.ok()

    def check_price(self, price: float, market_median: float | None, description: str,
                    source: str | None = None, source_id: str | None = None) -> FilterDecision:
        """Etap 5: cena nierealnie niska względem mediany → dokładniejsza analiza opisu."""
        ratio = self.config.suspicious_price_ratio
        if not market_median or price >= market_median * ratio:
            return FilterDecision.ok()
        pct = price / market_median * 100
        if source and source_id and (source, source_id) in self.whitelist:
            return FilterDecision(True, "price", f"cena to {pct:.0f}% wartości rynkowej", suspicious=True)
        desc_decision = self._check_description(description)
        if desc_decision is not None:
            return FilterDecision(False, "price", f"cena {price:.0f} zł to tylko {pct:.0f}% wartości rynkowej, "
                                                  f"a opis wskazuje na {desc_decision}", None)
        return FilterDecision(True, "price", f"cena {price:.0f} zł to tylko {pct:.0f}% wartości rynkowej "
                                             "— sprawdź ogłoszenie przed zakupem", suspicious=True)

    def _check_description(self, description: str) -> str | None:
        """Czy opis (przy podejrzanie niskiej cenie) mówi, że sprzedawany jest dodatek/część."""
        text = normalize(description).replace("|", " ")
        if not text:
            return None
        if re.search(r"\b(?:sprzedam|sprzedaje|na sprzedaz|oferuje)\s(?:\w+\s){0,2}(?:etui|case|pudelko|ladowark\w*|"
                     r"kabel|szklo|folie|atrap\w*|obudow\w*)", text):
            return "akcesorium"
        if re.search(r"\b(?:sam|sama|samo|tylko|wylacznie)\s(?:\w+\s)?(?:plyt\w*|wyswietlacz\w*|ekran\w*|bateri\w*|"
                     r"obudow\w*|korpus\w*|pudelk\w*|etui)", text):
            return "pojedynczą część lub akcesorium"
        if re.search(r"\b(?:atrapa|makieta|dummy|zabawka)\b", text):
            return "atrapę"
        return None
