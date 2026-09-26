"""Modele domenowe: oferty, stany, usterki, czerwone flagi i wynik wyceny."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class Mode(StrEnum):
    REPAIR = "repair"
    RESELL = "resell"

    @property
    def label(self) -> str:
        return {"repair": "Naprawa → sprzedaż", "resell": "Szybki resell"}[self.value]


class Condition(StrEnum):
    NEW = "new"
    LIKE_NEW = "like_new"
    GOOD = "good"
    DAMAGED = "damaged"
    FOR_PARTS = "for_parts"

    @property
    def label(self) -> str:
        return _CONDITION_LABELS[self]

    @property
    def market_class(self) -> str:
        """Klasa porównawcza przy liczeniu mediany: new / used / damaged."""
        if self is Condition.NEW:
            return "new"
        if self in (Condition.LIKE_NEW, Condition.GOOD):
            return "used"
        return "damaged"


_CONDITION_LABELS = {
    Condition.NEW: "Nowy",
    Condition.LIKE_NEW: "Jak nowy",
    Condition.GOOD: "Używany sprawny",
    Condition.DAMAGED: "Uszkodzony",
    Condition.FOR_PARTS: "Na części",
}


class Defect(StrEnum):
    SCREEN = "screen"
    BACK_GLASS = "back_glass"
    BATTERY = "battery"
    CHARGING_PORT = "charging_port"
    CAMERA = "camera"
    CAMERA_LENS = "camera_lens"
    FACE_ID = "face_id"
    SPEAKER = "speaker"
    MICROPHONE = "microphone"
    BUTTONS = "buttons"
    HOUSING = "housing"
    NO_POWER = "no_power"
    WATER_DAMAGE = "water_damage"

    @property
    def label(self) -> str:
        return _DEFECT_LABELS[self]

    @property
    def cosmetic(self) -> bool:
        """Usterki, przy których telefon nadal jest „sprawny" (nie zmieniają stanu na uszkodzony)."""
        return self in (Defect.BATTERY, Defect.HOUSING)


_DEFECT_LABELS = {
    Defect.SCREEN: "Wyświetlacz / szyba",
    Defect.BACK_GLASS: "Tylna szyba",
    Defect.BATTERY: "Bateria",
    Defect.CHARGING_PORT: "Port ładowania",
    Defect.CAMERA: "Aparat",
    Defect.CAMERA_LENS: "Szkiełko aparatu",
    Defect.FACE_ID: "Face ID",
    Defect.SPEAKER: "Głośnik",
    Defect.MICROPHONE: "Mikrofon",
    Defect.BUTTONS: "Przyciski",
    Defect.HOUSING: "Obudowa (wgniecenia)",
    Defect.NO_POWER: "Nie włącza się",
    Defect.WATER_DAMAGE: "Po zalaniu",
}


class Severity(StrEnum):
    HARD = "hard"
    SOFT = "soft"


class RedFlag(StrEnum):
    ICLOUD_LOCK = "icloud_lock"
    IMEI_BLOCKED = "imei_blocked"
    MDM = "mdm"
    REPLICA = "replica"
    FOR_PARTS_UNEXPLAINED = "for_parts_unexplained"
    SUSPICIOUSLY_CHEAP = "suspiciously_cheap"
    NO_PHOTOS = "no_photos"
    SIMLOCK = "simlock"
    NO_SIGNAL = "no_signal"
    NON_ORIGINAL_PARTS = "non_original_parts"
    UNTESTED = "untested"
    UNKNOWN_REPAIR_COST = "unknown_repair_cost"
    PRICE_UNREALISTIC = "price_unrealistic"

    @property
    def label(self) -> str:
        return _FLAG_INFO[self][0]

    @property
    def severity(self) -> Severity:
        return _FLAG_INFO[self][1]


_FLAG_INFO = {
    RedFlag.ICLOUD_LOCK: ("Blokada iCloud / aktywacji", Severity.HARD),
    RedFlag.IMEI_BLOCKED: ("Zablokowany IMEI / czarna lista", Severity.HARD),
    RedFlag.MDM: ("Profil MDM (telefon firmowy)", Severity.HARD),
    RedFlag.REPLICA: ("Możliwa podróbka / replika", Severity.HARD),
    RedFlag.FOR_PARTS_UNEXPLAINED: ("„Na części” bez opisu usterki", Severity.SOFT),
    RedFlag.SUSPICIOUSLY_CHEAP: ("Podejrzanie niska cena", Severity.SOFT),
    RedFlag.NO_PHOTOS: ("Brak zdjęć", Severity.SOFT),
    RedFlag.SIMLOCK: ("Simlock", Severity.SOFT),
    RedFlag.NO_SIGNAL: ("Brak zasięgu (modem lub blokada IMEI)", Severity.SOFT),
    RedFlag.NON_ORIGINAL_PARTS: ("Nieoryginalne części", Severity.SOFT),
    RedFlag.UNTESTED: ("Niesprawdzony / „sprzedaję jak jest”", Severity.SOFT),
    RedFlag.UNKNOWN_REPAIR_COST: ("Nieznany koszt naprawy", Severity.SOFT),
    RedFlag.PRICE_UNREALISTIC: ("Cena nierealnie niska — sprawdź ogłoszenie", Severity.HARD),
}


class Verdict(StrEnum):
    BUY = "KUPUJ"
    NEGOTIATE = "NEGOCJUJ"
    SKIP = "ODPUŚĆ"


class RowColor(StrEnum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class OfferStatus(StrEnum):
    NEW = "new"
    WATCHED = "watched"
    HIDDEN = "hidden"


@dataclass
class RawOffer:
    """Oferta w postaci zwróconej przez adapter portalu (przed normalizacją)."""

    source: str
    source_id: str
    url: str
    title: str
    price: float
    description: str = ""
    currency: str = "PLN"
    city: str | None = None
    region: str | None = None
    lat: float | None = None
    lon: float | None = None
    photos: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    shipping_available: bool | None = None
    negotiable: bool | None = None
    params: dict[str, str] = field(default_factory=dict)


@dataclass
class ParsedInfo:
    """Dane wyciągnięte z tytułu, opisu i parametrów ogłoszenia."""

    model: str | None = None
    storage_gb: int | None = None
    condition: Condition = Condition.GOOD
    defects: list[Defect] = field(default_factory=list)
    flags: list[RedFlag] = field(default_factory=list)
    battery_health: int | None = None
    negotiable: bool | None = None


@dataclass
class Offer:
    raw: RawOffer
    parsed: ParsedInfo
    id: int | None = None
    status: OfferStatus = OfferStatus.NEW
    first_seen: datetime | None = None
    last_seen: datetime | None = None
    distance_km: float | None = None
    dedup_key: str | None = None
    ai_defects: list[Defect] = field(default_factory=list)  # znalezione tylko przez AI
    ai_flags: list[RedFlag] = field(default_factory=list)
    ai_note: str | None = None

    @property
    def price(self) -> float:
        return self.raw.price


def merge_ai_findings(parsed: ParsedInfo, defects: list[Defect], flags: list[RedFlag]) -> tuple[list, list]:
    """Dokłada do wyniku reguł znaleziska AI (tylko dodaje). Zwraca to, co AI dodało."""
    new_defects = [d for d in defects if d not in parsed.defects]
    new_flags = [f for f in flags if f not in parsed.flags]
    parsed.defects.extend(new_defects)
    parsed.flags.extend(new_flags)
    if any(not d.cosmetic for d in new_defects) and parsed.condition.market_class != "damaged":
        parsed.condition = Condition.DAMAGED
    return new_defects, new_flags


@dataclass
class MarketObservation:
    price: float
    storage_gb: int | None
    condition: Condition
    offer_id: int | None = None


@dataclass
class MarketEstimate:
    value: float | None
    sample_size: int
    method: str
    confidence: str  # "wysoka" | "średnia" | "niska" | "brak"
    raw_median: float | None = None


@dataclass
class CostItem:
    label: str
    amount: float


@dataclass
class Negotiation:
    worth_it: bool
    opening_price: float | None
    max_price: float | None
    note: str


@dataclass
class Valuation:
    mode: Mode
    market: MarketEstimate
    repair_items: list[CostItem]
    repair_cost: float
    cost_items: list[CostItem]
    total_costs: float
    expected_profit: float | None
    roi_pct: float | None
    required_profit: float | None
    max_buy_price: float | None
    verdict: Verdict
    negotiation: Negotiation
    score: int
    color: RowColor
    flags: list[RedFlag]
    reasons: list[str]

    @property
    def has_hard_flag(self) -> bool:
        return any(f.severity is Severity.HARD for f in self.flags)
