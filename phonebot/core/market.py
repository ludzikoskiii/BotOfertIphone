"""Szacowanie wartości rynkowej na podstawie zebranych ofert."""
from __future__ import annotations

import math
import statistics

from .catalog import format_storage
from .models import MarketEstimate, MarketObservation
from .settings import Settings


def remove_outliers(prices: list[float]) -> list[float]:
    """Odrzuca wartości odstające metodą IQR (dla co najmniej 4 próbek)."""
    if len(prices) < 4:
        return list(prices)
    q1, _, q3 = statistics.quantiles(prices, n=4, method="inclusive")
    iqr = q3 - q1
    low, high = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return [p for p in prices if low <= p <= high]


def trim_low(prices: list[float], ratio: float) -> list[float]:
    """Odrzuca ceny dużo niższe od mediany (akcesoria, części, oszustwa, które przeszły filtry).

    IQR nie wystarcza, gdy tanich „śmieci” jest dużo — ten próg działa, dopóki stanowią mniejszość.
    """
    if ratio <= 0 or len(prices) < 4:
        return list(prices)
    floor = statistics.median(prices) * ratio
    return [p for p in prices if p >= floor]


def _clean(prices: list[float], settings: Settings) -> list[float]:
    return remove_outliers(trim_low(prices, settings.market_floor_ratio))


def _confidence(n: int, settings: Settings) -> str:
    if n >= 2 * settings.market_min_samples:
        return "wysoka"
    if n >= settings.market_min_samples:
        return "średnia"
    return "niska"


def manual_key(model: str, storage_gb: int | None) -> str:
    return f"{model}|{storage_gb or ''}"


def estimate_market_value(
    model: str | None,
    storage_gb: int | None,
    target_class: str,
    observations: list[MarketObservation],
    settings: Settings,
    *,
    exclude_offer_id: int | None = None,
) -> MarketEstimate:
    """Wartość rynkowa telefonu danego modelu/pojemności w klasie stanu ``target_class``.

    Kolejność źródeł:
    1. ręczna wartość z ustawień,
    2. mediana porównywalnych ofert (ten sam model, pojemność, klasa stanu),
    3. mediana z mniejszej próby (niska pewność),
    4. mediana innych pojemności przeliczona o ``storage_step_pct`` na każde podwojenie,
    5. dla klasy „new": wartość używanego × ``new_condition_multiplier``.
    Wynik (poza wartością ręczną) jest mnożony przez korektę cen wywoławczych.

    Oferty bez rozpoznanej pojemności nie są używane jako dane rynkowe (to najczęściej akcesoria).
    Dla takiej oferty wartość to mediana wszystkich pojemności modelu — z niską pewnością.
    """
    if not model:
        return MarketEstimate(None, 0, "nierozpoznany model", "brak")

    manual = settings.manual_market_values.get(manual_key(model, storage_gb))
    if manual:
        return MarketEstimate(float(manual), 0, "wartość ręczna z ustawień", "wysoka", float(manual))

    corr = settings.asking_price_correction
    obs = [
        o for o in observations
        if o.price >= settings.min_valid_price
        and o.condition.market_class == target_class
        and (exclude_offer_id is None or o.offer_id != exclude_offer_id)
    ]

    obs = [o for o in obs if o.storage_gb]  # bez pojemności = niepewne dane (często akcesoria)
    if not storage_gb:
        prices = _clean([o.price for o in obs], settings)
        if len(prices) >= settings.market_min_samples_fallback:
            median = statistics.median(prices)
            return MarketEstimate(round(median * corr, 2), len(prices),
                                  f"mediana {len(prices)} ofert wszystkich pojemności (pamięć nieznana)", "niska",
                                  median)
        return MarketEstimate(None, len(prices), "za mało danych rynkowych", "brak")

    same = _clean([o.price for o in obs if o.storage_gb == storage_gb], settings)
    if len(same) >= settings.market_min_samples_fallback:
        median = statistics.median(same)
        n = len(same)
        return MarketEstimate(
            round(median * corr, 2), n, f"mediana {n} ofert ({format_storage(storage_gb)})",
            _confidence(n, settings), median,
        )

    if storage_gb:
        step = 1 + settings.storage_step_pct / 100
        adjusted = [
            o.price * step ** math.log2(storage_gb / o.storage_gb)
            for o in obs
            if o.storage_gb and o.storage_gb != storage_gb
        ]
        adjusted = _clean(adjusted, settings)
        if len(adjusted) >= settings.market_min_samples_fallback:
            median = statistics.median(adjusted)
            return MarketEstimate(
                round(median * corr, 2), len(adjusted),
                f"mediana {len(adjusted)} ofert innych pojemności (przeliczona)", "niska", median,
            )

    if target_class == "new":
        used = estimate_market_value(model, storage_gb, "used", observations, settings,
                                     exclude_offer_id=exclude_offer_id)
        if used.value is not None:
            return MarketEstimate(
                round(used.value * settings.new_condition_multiplier, 2), used.sample_size,
                f"{used.method} × {settings.new_condition_multiplier:g} (nowy)", "niska", used.raw_median,
            )

    return MarketEstimate(None, len(same), "za mało danych rynkowych", "brak")
