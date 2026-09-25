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

from .models import Mode, RedFlag

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
    }


@dataclass
class Settings:
    # --- tryb i lokalizacja ---
    mode: str = Mode.REPAIR.value
    location_name: str = "Kacwin"
    home_lat: float = 49.3494
    home_lon: float = 20.3019
    search_radius_km: int = 0  # 0 = cała Polska (zakupy głównie z wysyłką)
    shipping_only: bool = False  # pokazuj tylko oferty z wysyłką

    # --- minimalny zysk (osobno dla trybów) ---
    profit_repair: ProfitRule = field(default_factory=ProfitRule)
    profit_resell: ProfitRule = field(default_factory=ProfitRule)

    # --- sprzedaż ---
    sales_channels: list[SalesChannel] = field(default_factory=_default_channels)
    active_sales_channel: str = "OLX"
    packaging_cost: float = 5.0

    # --- zakup ---
    buy_shipping_cost: float = 15.0
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
        default_factory=lambda: {"olx": True, "allegro_lokalnie": True, "vinted": True}
    )
    request_delay_s: float = 4.0
    max_pages_per_query: int = 3
    watched_models: list[str] = field(default_factory=list)  # pusta = wszystkie
    price_min: float = 0.0
    price_max: float = 0.0  # 0 = bez limitu

    # --- odświeżanie i powiadomienia ---
    refresh_minutes: int = 15
    notify_desktop: bool = True
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""

    # --- analiza opisów przez AI (opcjonalna) ---
    llm_enabled: bool = False
    anthropic_api_key: str = ""

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
