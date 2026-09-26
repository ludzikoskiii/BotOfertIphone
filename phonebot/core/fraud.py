"""Wykrywanie możliwych oszustw — lokalnie i za darmo (reguły + skróty zdjęć, bez zewnętrznych usług).

Każdy sygnał ma wagę (punkty, do zmiany w Ustawieniach → Oszustwa). Suma punktów daje poziom ryzyka:
niskie / średnie / wysokie. Bardzo niska cena **razem z dowolnym innym sygnałem** dodaje premię — tak
wyglądają typowe oszustwa („tanio, tylko wysyłka, przedpłata BLIK-iem”).

Skutki (w ``services.evaluator``):

* średnie → werdykt najwyżej DO WERYFIKACJI,
* wysokie → czerwona etykieta „MOŻLIWE OSZUSTWO”, oferta nie trafia automatycznie do „Wybrane”
  i nie wywołuje powiadomień.

Sygnały sprzedawcy korzystają z danych, które podaje portal (Vinted — profil, eBay — opinie); gdy portal
ich nie podaje, sygnał po prostu nie działa (nie zgadujemy).
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .models import Offer
from .text import any_match, normalize, phrase

LEVELS = {"low": "niskie", "medium": "średnie", "high": "wysokie"}

SIGNALS: dict[str, tuple[str, int]] = {  # klucz → (opis, domyślna waga)
    "new_account": ("Nowe konto sprzedającego", 20),
    "no_reviews": ("Sprzedający bez żadnych opinii", 10),
    "negative_reviews": ("Dużo negatywnych opinii sprzedającego", 25),
    "many_expensive_new": ("Nowe konto / bez opinii z wieloma drogimi telefonami", 30),
    "contact_outside": ("Kontakt poza portalem (WhatsApp, Telegram, e-mail, numer telefonu)", 15),
    "foreign_phone": ("Zagraniczny numer telefonu w ogłoszeniu", 15),
    "shipping_only_cheap": ("Tylko wysyłka przy bardzo niskiej cenie", 15),
    "prepayment": ("Przedpłata / BLIK / link do płatności poza portalem", 30),
    "sealed_gift_cheap": ("„Nowy, zafoliowany” albo „prezent” wyraźnie poniżej ceny rynkowej", 20),
    "copied_description": ("Skopiowany opis — ten sam tekst w ogłoszeniu innego sprzedającego", 20),
    "duplicate_photo": ("To samo (albo prawie to samo) zdjęcie u innego sprzedającego / w innym mieście", 30),
    "stock_photo": ("Zdjęcie wygląda na katalogowe (jednolite białe tło), nie na prawdziwe zdjęcie", 15),
    "no_real_photos": ("Brak prawdziwych zdjęć", 15),
    "very_cheap": ("Cena bardzo niska względem rynku", 15),
}

SAFETY_TIPS = (
    "Nie płać z góry poza portalem (BLIK na telefon, przelew, „link do płatności” przysłany w wiadomości).",
    "Kupuj z ochroną kupującego portalu (np. Allegro, Vinted, eBay) albo przy odbiorze osobistym.",
    "Przy odbiorze sprawdź telefon: IMEI (*#06#) zgodny z pudełkiem, Ustawienia → Apple ID (wylogowany), "
    "„Znajdź mój iPhone” wyłączone, brak blokady aktywacji.",
    "Nie przenoś rozmowy na WhatsApp / Telegram / e-mail — tam portal Cię nie ochroni.",
    "Poproś o zdjęcie telefonu z kartką z dzisiejszą datą i Twoim imieniem.",
)


@dataclass
class FraudConfig:
    """Progi i wagi (Ustawienia → Oszustwa)."""

    enabled: bool = True
    weights: dict[str, int] = field(default_factory=dict)  # nadpisania domyślnych wag z ``SIGNALS``
    medium_threshold: int = 30
    high_threshold: int = 60
    combo_bonus: int = 20  # bardzo tanio + inny sygnał
    new_account_days: int = 30
    negative_pct: float = 90.0  # mniej pozytywnych opinii (%) = „dużo negatywnych”
    cheap_ratio: float = 0.55  # cena < 55% wartości rynkowej = bardzo tanio
    gift_ratio: float = 0.75  # „zafoliowany”/„prezent” poniżej 75% wartości
    expensive_price: float = 1500.0
    expensive_count: int = 3
    photo_distance: int = 6  # różnica skrótów zdjęć (bity z 64), poniżej której to „to samo zdjęcie”
    photos_per_run: int = 40  # ile zdjęć sprawdzać w tle na raz

    def weight(self, key: str) -> int:
        return int(self.weights.get(key, SIGNALS[key][1]))


@dataclass
class Signal:
    key: str
    label: str
    points: int
    detail: str = ""


@dataclass
class FraudAssessment:
    score: int = 0
    level: str = "low"  # low | medium | high
    signals: list[Signal] = field(default_factory=list)

    @property
    def label(self) -> str:
        return LEVELS[self.level]

    def reasons(self) -> list[str]:
        return [s.label + (f" ({s.detail})" if s.detail else "") for s in self.signals]


@dataclass
class SellerStats:
    """Co wiemy o sprzedającym (z profilu portalu albo z ogłoszenia)."""

    created_at: datetime | None = None
    reviews: int | None = None
    positive_pct: float | None = None
    negative: int | None = None


@dataclass
class FraudContext:
    """Dane z bazy potrzebne sygnałom „porównawczym” (opisy, zdjęcia, oferty sprzedającego)."""

    descriptions: dict[str, set[str]] = field(default_factory=dict)  # skrót opisu → sprzedający/miasta
    photos: dict[tuple[str, str], tuple[int, bool]] = field(default_factory=dict)  # (portal, id) → (dhash, katalog)
    photo_owner: dict[tuple[str, str], str] = field(default_factory=dict)  # (portal, id) → sprzedający/miasto
    sellers: dict[tuple[str, str], SellerStats] = field(default_factory=dict)
    expensive_by_seller: dict[tuple[str, str], int] = field(default_factory=dict)
    _bands: dict[tuple[int, int], list[tuple[str, str]]] | None = None

    def similar_photos(self, key: tuple[str, str], max_distance: int) -> list[tuple[str, str]]:
        """Oferty z podobnym zdjęciem. Indeks 8 pasm po 8 bitów: przy różnicy ≤ 7 bitów co najmniej jedno
        pasmo jest identyczne (zasada szufladkowa) — zamiast porównywać każde zdjęcie z każdym."""
        mine = self.photos.get(key)
        if mine is None:
            return []
        if self._bands is None:
            self._bands = {}
            for k, (h, _) in self.photos.items():
                for i in range(8):
                    self._bands.setdefault((i, (h >> (8 * i)) & 0xFF), []).append(k)
        seen: set[tuple[str, str]] = set()
        out = []
        for i in range(8):
            for k in self._bands.get((i, (mine[0] >> (8 * i)) & 0xFF), []):
                if k != key and k not in seen:
                    seen.add(k)
                    if hamming(self.photos[k][0], mine[0]) <= max_distance:
                        out.append(k)
        return out


# ------------------------------------------------------------- tekst ---

_EMAIL = re.compile(r"[\w.+-]+\s*(?:@|\(at\)|\[at\]|\s(?:małpa|malpa)\s)\s*[\w-]+\s*(?:\.|\(dot\)|\skropka\s)\s*[a-z]{2,}",
                    re.I)
_MESSENGERS = re.compile(r"whats\s?app|whatsap|watsap|łatsap|wa\.me|telegram|t\.me/|viber|signal\b|kik\b", re.I)
_PHONE = re.compile(r"(?<![\d])(?:\+|00)\s?(\d{1,3})[\s.-]?\(?\d{1,4}\)?(?:[\s.-]?\d{2,4}){2,4}(?![\d])")
_PL_PHONE = re.compile(r"(?<![\d])(?:\d{3}[\s.-]?\d{3}[\s.-]?\d{3})(?![\d])")
_PREPAY = (phrase(r"\bprzedplat\w*"), phrase(r"\bblik\w*"), phrase(r"\b(?:zaplata|platnosc|przelew|wplata) z gory"),
           phrase(r"\blink\w* do (?:platnosci|zaplaty|oplaty|kupna)"), phrase(r"\bwysle (?:ci |panu |pani )?link"),
           phrase(r"\b(?:western union|moneygram|paysafecard|bitcoin|kryptowalut\w*|revolut na numer)"))
_SHIP_ONLY = (phrase(r"\b(?:tylko|wylacznie|jedynie) wysylk\w*", negatable=False),
              phrase(r"\b(?:brak|nie ma) (?:mozliwosci )?odbior\w* osobist\w*", negatable=False),
              phrase(r"\bbez odbioru osobistego", negatable=False))
_GIFT = (phrase(r"\bzafoliowan\w*"), phrase(r"\bnieodpakowan\w*"), phrase(r"\b(?:nietrafion\w* )?prezent\w*"),
         phrase(r"\bwygran\w* w konkursie"), phrase(r"\bnowy nieuzywany"), phrase(r"\bsealed\b"))


def phone_numbers(text: str) -> list[str]:
    """Numery telefonów z tekstu (same cyfry, z kierunkowym gdy podany) — do czarnej listy i sygnałów."""
    out = []
    for m in _PHONE.finditer(text or ""):
        out.append(re.sub(r"\D", "", m.group(0)).removeprefix("00"))
    for m in _PL_PHONE.finditer(text or ""):
        digits = re.sub(r"\D", "", m.group(0))
        if digits[0] in "45678" and not any(o.endswith(digits) for o in out):  # komórki w Polsce
            out.append("48" + digits)
    return list(dict.fromkeys(out))


def desc_hash(text: str | None) -> str | None:
    """Skrót opisu do wykrywania kopii (tylko dłuższe opisy — krótkie „stan dobry” się powtarzają)."""
    norm = normalize(text).replace("|", " ")
    norm = re.sub(r"\s+", " ", norm).strip()
    if len(norm) < 80:
        return None
    return hashlib.sha1(norm.encode()).hexdigest()


def owner_key(offer: Offer) -> str:
    """Kto wystawił: login/ID sprzedającego, a gdy portal go nie podaje — portal + miasto."""
    p = offer.raw.params
    who = p.get("seller") or p.get("seller_id")
    if who:
        return f"{offer.raw.source}:{str(who).lower()}"
    return f"{offer.raw.source}:@{(offer.raw.city or '?').lower()}"


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


# ------------------------------------------------------------- ocena ---

def assess(offer: Offer, market_value: float | None, ctx: FraudContext, cfg: FraudConfig,
           now: datetime | None = None) -> FraudAssessment:
    if not cfg.enabled:
        return FraudAssessment()
    raw = offer.raw
    text = f"{raw.title}\n{raw.description or ''}"
    norm = normalize(text)
    sig: list[Signal] = []

    def add(key: str, detail: str = "") -> None:
        sig.append(Signal(key, SIGNALS[key][0], cfg.weight(key), detail))

    cheap = bool(market_value) and raw.price < cfg.cheap_ratio * market_value  # type: ignore[operator]
    # --- sprzedający ---
    seller_key = (raw.source, str(raw.params.get("seller_id") or ""))
    stats = ctx.sellers.get(seller_key) or SellerStats()
    if raw.params.get("seller_feedback") is not None:  # eBay: opinie w ogłoszeniu
        try:
            stats.reviews = int(float(raw.params["seller_feedback"]))
            stats.positive_pct = float(raw.params.get("seller_positive_pct") or 100)
        except ValueError:
            pass
    now = now or datetime.now(UTC)
    new = stats.created_at is not None and (now - stats.created_at).days < cfg.new_account_days
    if new:
        add("new_account", f"konto od {stats.created_at.astimezone():%d.%m.%Y}")
    if stats.reviews == 0:
        add("no_reviews")
    elif stats.reviews and stats.reviews >= 3 and (
            (stats.positive_pct is not None and stats.positive_pct < cfg.negative_pct)
            or (stats.negative is not None and stats.negative * 2 > stats.reviews)):
        add("negative_reviews", f"{stats.positive_pct:.0f}% pozytywnych" if stats.positive_pct is not None else "")
    if (new or stats.reviews == 0) and ctx.expensive_by_seller.get(seller_key, 0) >= cfg.expensive_count:
        add("many_expensive_new", f"{ctx.expensive_by_seller[seller_key]} ofert ≥ {cfg.expensive_price:.0f} zł")
    # --- tekst ---
    phones = phone_numbers(text)
    contacts = []
    if _MESSENGERS.search(text):
        contacts.append(_MESSENGERS.search(text).group(0).strip())
    if _EMAIL.search(text):
        contacts.append("e-mail")
    if phones:
        contacts.append("numer telefonu")
    if contacts:
        add("contact_outside", ", ".join(dict.fromkeys(contacts)))
    foreign = [p for p in phones if not p.startswith("48")]
    if foreign:
        add("foreign_phone", "+" + foreign[0][:3] + "…")
    if any_match(_PREPAY, norm):
        add("prepayment")
    if cheap and (any_match(_SHIP_ONLY, norm)):
        add("shipping_only_cheap")
    if market_value and raw.price < cfg.gift_ratio * market_value and any_match(_GIFT, norm):
        add("sealed_gift_cheap")
    h = desc_hash(raw.description)
    if h and len(ctx.descriptions.get(h, set()) - {owner_key(offer)}) > 0:
        add("copied_description")
    # --- zdjęcia ---
    key = (raw.source, raw.source_id)
    mine = ctx.photos.get(key)
    if not raw.photos:
        add("no_real_photos")
    elif mine is not None:
        me = owner_key(offer)
        dup = next((k for k in ctx.similar_photos(key, min(cfg.photo_distance, 7)) if ctx.photo_owner.get(k) != me),
                   None)
        if dup is not None:
            add("duplicate_photo", f"też w ogłoszeniu {dup[0]} {dup[1]}")
        if mine[1]:
            add("stock_photo")
    # --- cena ---
    if cheap:
        add("very_cheap", f"{raw.price / market_value:.0%} wartości rynkowej")  # type: ignore[operator]
    score = sum(s.points for s in sig)
    if cheap and len(sig) > 1:
        score += cfg.combo_bonus
        sig.append(Signal("combo", "Bardzo niska cena razem z innymi sygnałami", cfg.combo_bonus))
    level = "high" if score >= cfg.high_threshold else "medium" if score >= cfg.medium_threshold else "low"
    return FraudAssessment(score, level, sig)
