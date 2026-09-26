"""Ustawienia aplikacji — wszystkie progi i koszty edytowalne z GUI.

Ustawienia są serializowane do JSON i trzymane w bazie SQLite. Przy odczycie
brakujące klucze dostają wartości domyślne, a nieznane są ignorowane, więc
dodawanie nowych opcji nie psuje zapisanych ustawień.
"""
from __future__ import annotations

import dataclasses
import json
import typing
from dataclasses import dataclass, field
from typing import Any

from .listing_filter import ListingFilterConfig
from .messages import DEFAULT_TEMPLATES, OLD_NEGOTIATE_TEMPLATE
from .models import Mode, RedFlag
from .sanity import SanityConfig
from .selection import SelectionCriteria
from .view_filter import ViewFilter

MIN_PROFIT_MODES = ("amount", "percent", "max", "min")
MIN_PROFIT_MODE_LABELS = {
    "amount": "Tylko kwota",
    "percent": "Tylko procent",
    "max": "Większa z obu (bezpieczniej)",
    "min": "Mniejsza z obu",
}


@dataclass
class ProfitRule:
    """Minimalny zysk: kwota [zł] i/lub procent od zainwestowanej kwoty."""

    min_amount: float = 150.0
    min_percent: float = 20.0
    mode: str = "max"


@dataclass
class SalesChannel:
    """Kanał, w którym sprzedajesz telefon po zakupie/naprawie."""

    name: str
    commission_pct: float = 0.0
    fixed_fee: float = 0.0
    shipping_cost: float = 0.0  # wysyłka opłacana przez Ciebie przy sprzedaży


def _default_channels() -> list[SalesChannel]:
    # Wartości orientacyjne — zweryfikuj aktualne cenniki portali.
    return [
        SalesChannel("OLX", commission_pct=0.0, fixed_fee=0.0, shipping_cost=0.0),
        SalesChannel("Allegro", commission_pct=8.0, fixed_fee=1.0, shipping_cost=0.0),
        SalesChannel("Vinted", commission_pct=0.0, fixed_fee=0.0, shipping_cost=0.0),
    ]


def _default_penalties() -> dict[str, int]:
    return {
        RedFlag.ICLOUD_LOCK.value: 60,
        RedFlag.IMEI_BLOCKED.value: 50,
        RedFlag.MDM.value: 40,
        RedFlag.REPLICA.value: 60,
        RedFlag.FOR_PARTS_UNEXPLAINED.value: 20,
        RedFlag.SUSPICIOUSLY_CHEAP.value: 20,
        RedFlag.NO_PHOTOS.value: 15,
        RedFlag.SIMLOCK.value: 10,
        RedFlag.NO_SIGNAL.value: 20,
        RedFlag.NON_ORIGINAL_PARTS.value: 10,
        RedFlag.UNTESTED.value: 15,
        RedFlag.UNKNOWN_REPAIR_COST.value: 10,
        RedFlag.PRICE_UNREALISTIC.value: 30,
        RedFlag.PROFIT_UNREALISTIC.value: 20,
        RedFlag.STORAGE_UNKNOWN.value: 5,
        RedFlag.SERIAL_SELLER.value: 50,
        RedFlag.FOREIGN_SELLER.value: 5,
        RedFlag.AI_TEXT_CONFLICT.value: 20,
        RedFlag.AI_PHOTO_CONFLICT.value: 20,
        RedFlag.AI_LOW_CONFIDENCE.value: 10,
        RedFlag.AI_DESC_CONFLICT.value: 20,
    }


def _default_categories() -> dict[str, dict[str, str]]:
    """Kategorie telefonów na portalach: ID kategorii i adres, pod którym portal ją pokazuje."""
    return {
        # Allegro Lokalnie: „Telefony i akcesoria” (ID 4) — węższej kategorii dla telefonów portal nie ma
        "allegro_lokalnie": {"id": "4", "path": "elektronika/telefony-i-akcesoria-4"},
        # Sprzedajemy.pl: Elektronika > Telefony i akcesoria > Telefony komórkowe > Apple iPhone (ID 1390)
        "sprzedajemy": {"id": "1390", "path": "elektronika/telefony-i-akcesoria/telefony-komorkowe/apple-iphone"},
        # Vinted: „Telefony komórkowe” (ID 3661) — API katalogu ignoruje filtr kategorii, zostaje tylko informacyjnie
        "vinted": {"id": "3661", "path": ""},
    }


VINTED_COUNTRY_MODES = {
    "ship": "Z Polski i z zagranicy z wysyłką do Polski (zagraniczne oznaczone flagą)",
    "pl": "Tylko oferty z Polski (język tytułu + kraj z profilu sprzedawcy)",
}

# wersja domyślnych ustawień: zmiana domyślnej wartości, którą trzeba raz przenieść do zapisanych ustawień
# 2 — oferty z zagranicy widoczne (Vinted: „pl” → „ship”)
# 3 — nowy szablon negocjacji (styl uprzejmy), jeśli zapisany był niezmieniony stary
SETTINGS_VERSION = 3


# kolumny tabeli ukryte domyślnie (nazwy z ui.table_model.Col, małymi literami)
DEFAULT_HIDDEN_COLUMNS = ("photo", "condition", "battery", "market", "score", "flags", "location", "added", "link")


@dataclass
class MlConfig:
    """Lokalne AI (darmowe, na Twoim komputerze): klasyfikator tytułów, analiza zdjęć i opisów (Ollama)."""

    text_enabled: bool = True
    photo_enabled: bool = True
    # tytuł: „telefon” od tej pewności = zgodne z regułami; inna klasa od tej pewności = sprzeczność
    text_phone_conf: float = 0.60
    text_conflict_conf: float = 0.60
    # zdjęcie: „smartfon” od tej pewności = zgodne; akcesorium/pudełko od tej pewności = sprzeczność
    photo_phone_conf: float = 0.50
    photo_conflict_conf: float = 0.80  # test na 80 zdjęciach: przy 80% zero telefonów uznanych za etui
    retrain_after_labels: int = 50  # automatyczne douczanie po tylu nowych oznaczeniach
    learn_from_hidden: bool = True  # ukryte oferty = słaba wskazówka „to nie telefon”
    # opcjonalny lokalny model językowy (Ollama) czyta opisy ofert „DO WERYFIKACJI”
    llm_enabled: bool = False
    llm_url: str = "http://127.0.0.1:11434"
    llm_model: str = "qwen3:8b"
    llm_fetch_pages: bool = True  # opis ze strony oferty, gdy wyniki wyszukiwania go nie mają
    llm_think: bool = False  # tryb „myślenia” (Qwen3): dokładniej, ale kilka razy wolniej


@dataclass
class Settings:
    settings_version: int = SETTINGS_VERSION  # do jednorazowych zmian zapisanych ustawień po aktualizacji
    # --- tryb i lokalizacja ---
    mode: str = Mode.REPAIR.value
    location_name: str = "Kacwin"
    home_lat: float = 49.3494
    home_lon: float = 20.3019

    # --- minimalny zysk (osobno dla trybów) ---
    profit_repair: ProfitRule = field(default_factory=ProfitRule)
    profit_resell: ProfitRule = field(default_factory=ProfitRule)

    # --- sprzedaż ---
    sales_channels: list[SalesChannel] = field(default_factory=_default_channels)
    active_sales_channel: str = "OLX"
    packaging_cost: float = 5.0

    # --- zakup ---
    buy_shipping_cost: float = 15.0
    # opłata kupującego per portal: [procent ceny, kwota stała] — wartości orientacyjne
    buyer_fees: dict[str, list[float]] = field(
        default_factory=lambda: {"allegro_lokalnie": [0.0, 0.0], "vinted": [5.0, 2.9],
                                 "sprzedajemy": [0.0, 0.0]}
    )
    pickup_cost_per_km: float = 1.0  # liczone w obie strony
    pickup_flat_cost: float = 100.0  # gdy brak wysyłki i nieznana odległość

    # --- naprawa (samodzielna) ---
    own_labor_cost: float = 0.0  # wycena własnej pracy za jedną naprawę
    parts_shipping_cost: float = 12.0
    unknown_defect_risk_cost: float = 200.0
    battery_health_threshold: int = 80

    # --- wartość rynkowa ---
    market_window_days: int = 30
    market_min_samples: int = 5
    market_min_samples_fallback: int = 2
    asking_price_correction: float = 0.90
    new_condition_multiplier: float = 1.15
    storage_step_pct: float = 8.0
    min_valid_price: float = 50.0
    market_floor_ratio: float = 0.30  # ceny poniżej tej części mediany nie liczą się do wyceny rynkowej
    manual_market_values: dict[str, float] = field(default_factory=dict)  # "iPhone 13|128" -> zł

    # --- negocjacje i werdykt ---
    negotiation_margin_pct: float = 15.0
    negotiable_bonus_pct: float = 10.0
    opening_ratio: float = 0.88
    buy_try_discount_pct: float = 5.0
    hard_flags_force_skip: bool = False
    suspicious_price_ratio_working: float = 0.5
    suspicious_price_ratio_damaged: float = 0.15
    flag_penalties: dict[str, int] = field(default_factory=_default_penalties)
    score_green: int = 65
    score_yellow: int = 40

    # --- pobieranie ---
    enabled_sources: dict[str, bool] = field(
        default_factory=lambda: {"allegro_lokalnie": True, "vinted": True, "sprzedajemy": True}
    )
    request_delay_s: float = 4.0
    source_timeout_s: float = 180.0  # maks. czas pobierania z jednego portalu
    blocked_cooldown_minutes: int = 180  # po blokadzie portalu automat nie odpytuje go przez tyle minut
    offer_stale_days: int = 7  # ukryj oferty niewidziane od tylu dni
    max_pages_per_query: int = 3
    watched_models: list[str] = field(default_factory=list)  # pusta = wszystkie
    # kategorie telefonów per portal (puste „path” = szukaj we wszystkich kategoriach)
    source_categories: dict[str, dict[str, str]] = field(default_factory=_default_categories)
    # minimalna cena pobierania per portal — tanie akcesoria odpadają już na portalu (Vinted nie filtruje kategorii)
    source_min_price: dict[str, float] = field(default_factory=lambda: {"vinted": 150.0})
    # Vinted: kraj sprzedawcy — domyślnie także oferty z zagranicy (z flagą „Sprzedawca z zagranicy”)
    vinted_country_mode: str = "ship"
    seller_lookups_per_scan: int = 20  # ilu sprzedawców sprawdzić na skan (profil = 1 zapytanie)
    price_min: float = 0.0
    price_max: float = 0.0  # 0 = bez limitu

    # --- filtr ogłoszeń (akcesoria, części, „kupię”, test ceny) ---
    listing_filter: ListingFilterConfig = field(default_factory=ListingFilterConfig)
    # --- zabezpieczenia werdyktu (testy sensowności, limity przy flagach, sprzedawcy seryjni) ---
    sanity: SanityConfig = field(default_factory=SanityConfig)
    # --- lista „Wybrane”: kryteria automatyczne ---
    selection: SelectionCriteria = field(default_factory=SelectionCriteria)
    # --- lokalne AI (etap 2) ---
    ml: MlConfig = field(default_factory=MlConfig)

    # --- filtry widoku (zapamiętywane) ---
    view_filter: ViewFilter = field(default_factory=ViewFilter)

    # --- odświeżanie i powiadomienia ---
    refresh_minutes: int = 15
    minimize_to_tray: bool = True
    notify_desktop: bool = True
    notify_price_drops: bool = True
    notify_max_per_scan: int = 5
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- wygląd ---
    ui_theme: str = "system"  # system | light | dark
    ui_font_pt: int = 10
    hidden_columns: list[str] = field(default_factory=lambda: list(DEFAULT_HIDDEN_COLUMNS))
    column_widths: dict[str, int] = field(default_factory=dict)
    # ostatnie sortowanie tabeli (osobno dla każdej listy): {"all": [["verdict", "desc"], ["profit", "desc"]]}
    table_sort: dict[str, list] = field(default_factory=dict)
    table_list: str = "all"  # ostatnio otwarta lista: all („Wszystkie oferty”) | picked („Wybrane”)
    splitter_sizes: list[int] = field(default_factory=list)  # filtry | tabela | szczegóły
    filters_visible: bool = True
    details_visible: bool = True

    # --- wiadomości do sprzedającego (szablony, bez AI) ---
    message_templates: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_TEMPLATES))
    negotiation_style: str = "polite"  # polite | concrete | pickup (szybki odbiór)
    pickup_radius_km: int = 50  # „przyjadę i zapłacę gotówką” tylko dla ofert w tym promieniu

    # ---------------------------------------------------------------- API ---

    @property
    def mode_enum(self) -> Mode:
        return Mode(self.mode)

    def profit_rule(self, mode: Mode) -> ProfitRule:
        return self.profit_repair if mode is Mode.REPAIR else self.profit_resell

    def sales_channel(self) -> SalesChannel:
        for ch in self.sales_channels:
            if ch.name == self.active_sales_channel:
                return ch
        return self.sales_channels[0] if self.sales_channels else SalesChannel("Brak")

    def penalty(self, flag: RedFlag) -> int:
        return int(self.flag_penalties.get(flag.value, 0))

    def to_json(self) -> str:
        return json.dumps(dataclasses.asdict(self), ensure_ascii=False, indent=2)

    @classmethod
    def from_json(cls, text: str | None) -> Settings:
        if not text:
            return cls()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return cls()
        return _from_dict(cls, data)


def _from_dict(cls: type, data: Any) -> Any:
    """Tolerancyjna deserializacja dataclass: brakujące pola = domyślne."""
    if not isinstance(data, dict):
        return cls() if _all_fields_have_defaults(cls) else None
    hints = typing.get_type_hints(cls)
    kwargs: dict[str, Any] = {}
    for f in dataclasses.fields(cls):
        if f.name not in data:
            continue
        kwargs[f.name] = _convert(hints[f.name], data[f.name])
    try:
        obj = cls(**kwargs)
    except TypeError:
        return cls()
    if isinstance(obj, Settings):
        # nowe flagi dodane w kolejnych wersjach dostają domyślną karę
        for key, value in _default_penalties().items():
            obj.flag_penalties.setdefault(key, value)
        # dawna opcja „poważne flagi wymuszają ODPUŚĆ” → limit werdyktu przy poważnej fladze
        if obj.hard_flags_force_skip:
            obj.sanity.hard_flag_cap = "ODPUŚĆ"
            obj.hard_flags_force_skip = False
        # ustawienia zapisane przez starszą wersję: raz przenieś zmienione wartości domyślne
        saved_version = data.get("settings_version", 1)
        if isinstance(saved_version, int) and saved_version < 2 and obj.vinted_country_mode == "pl":
            obj.vinted_country_mode = "ship"  # oferty z zagranicy widoczne (można wrócić w Ustawieniach)
        if isinstance(saved_version, int) and saved_version < 3:
            if obj.message_templates.get("negotiate", "").strip() == OLD_NEGOTIATE_TEMPLATE.strip():
                obj.message_templates["negotiate"] = DEFAULT_TEMPLATES["negotiate"]
        for key, text in DEFAULT_TEMPLATES.items():  # nowe szablony dochodzą do zapisanych ustawień
            obj.message_templates.setdefault(key, text)
        obj.settings_version = SETTINGS_VERSION
        # listy słów zapisane przez starszą wersję: dopisz nowe słowa (np. akcesoria w innych językach)
        saved = data.get("listing_filter")
        if isinstance(saved, dict):
            version = saved.get("defaults_version", 1)
            if isinstance(version, int) and version < obj.listing_filter.defaults_version:
                obj.listing_filter.upgrade_defaults(version)
    return obj


def _convert(tp: Any, value: Any) -> Any:
    origin = typing.get_origin(tp)
    if dataclasses.is_dataclass(tp):
        return _from_dict(tp, value)
    if origin is list:
        (item_tp,) = typing.get_args(tp)
        if dataclasses.is_dataclass(item_tp):
            return [_from_dict(item_tp, v) for v in value if isinstance(v, dict)]
        return list(value)
    if origin is dict:
        return dict(value)
    if tp is float and isinstance(value, (int, float)):
        return float(value)
    if tp is int and isinstance(value, (int, float)):
        return int(value)
    return value


def _all_fields_have_defaults(cls: type) -> bool:
    return all(
        f.default is not dataclasses.MISSING or f.default_factory is not dataclasses.MISSING
        for f in dataclasses.fields(cls)
    )
