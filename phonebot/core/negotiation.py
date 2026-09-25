"""Werdykt, rekomendacja negocjacji i ocena punktowa oferty."""
from __future__ import annotations

import math

from .models import Negotiation, RedFlag, RowColor, Verdict
from .settings import Settings


def nice_floor(value: float) -> float:
    """Zaokrągla w dół do „ładnej" kwoty: 10 zł (<500), 50 zł (<2000), 100 zł (powyżej)."""
    if value <= 0:
        return 0.0
    step = 10 if value < 500 else 50 if value < 2000 else 100
    return float(math.floor(value / step) * step)


def floor10(value: float) -> float:
    return float(max(0, math.floor(value / 10) * 10))


def negotiation_margin(negotiable: bool | None, settings: Settings) -> float:
    """Jak daleko ponad max cenę zakupu (ułamek) warto jeszcze próbować negocjować."""
    if negotiable is False:
        return 0.0
    margin = settings.negotiation_margin_pct / 100
    if negotiable is True:
        margin += settings.negotiable_bonus_pct / 100
    return margin


def recommend(price: float, max_buy: float, negotiable: bool | None, settings: Settings) -> tuple[Verdict, Negotiation]:
    margin = negotiation_margin(negotiable, settings)
    if max_buy <= 0:
        return Verdict.SKIP, Negotiation(False, None, None, "Przy tej wartości rynkowej nie da się osiągnąć minimalnego zysku.")

    if price <= max_buy:
        if negotiable is False:
            return Verdict.BUY, Negotiation(False, None, price, "Cena poniżej maksymalnej — kupuj, sprzedający nie negocjuje.")
        opening = nice_floor(price * (1 - settings.buy_try_discount_pct / 100))
        if opening >= price or opening <= 0:
            return Verdict.BUY, Negotiation(False, None, price, "Cena poniżej maksymalnej — kupuj.")
        return Verdict.BUY, Negotiation(
            negotiable is True, opening, price,
            f"Cena poniżej maksymalnej ({max_buy:.0f} zł) — kupuj. Możesz spróbować zaproponować {opening:.0f} zł, "
            "ale nie ryzykuj utraty okazji.",
        )

    over_pct = (price - max_buy) / max_buy * 100
    if margin > 0 and price <= max_buy * (1 + margin):
        opening = nice_floor(max_buy * settings.opening_ratio)
        max_price = floor10(max_buy)
        return Verdict.NEGOTIATE, Negotiation(
            True, opening, max_price,
            f"Cena o {over_pct:.0f}% powyżej maksymalnej. Zaproponuj {opening:.0f} zł, "
            f"nie płać więcej niż {max_price:.0f} zł (obniżka o {price - max_price:.0f} zł).",
        )

    reason = "sprzedający nie negocjuje" if negotiable is False else "za dużo, by negocjować"
    return Verdict.SKIP, Negotiation(
        False, None, floor10(max_buy),
        f"Cena o {over_pct:.0f}% powyżej maksymalnej ({max_buy:.0f} zł) — {reason}.",
    )


_CONFIDENCE_ADJ = {"wysoka": 0, "średnia": -5, "niska": -12, "brak": -30}


def compute_score(
    verdict: Verdict,
    *,
    price: float,
    max_buy: float | None,
    profit: float | None,
    required: float | None,
    margin: float,
    confidence: str,
    flags: list[RedFlag],
    settings: Settings,
    mode_mismatch: bool = False,
) -> int:
    """Ocena 0–100: atrakcyjność ekonomiczna + pewność danych − kary za flagi."""
    if max_buy is None or profit is None or required is None:
        base = 15.0
    elif mode_mismatch:
        base = 10.0
    elif verdict is Verdict.BUY:
        excess = (profit - required) / max(required, 1.0)
        base = 70 + min(30.0, 30 * excess)
    elif verdict is Verdict.NEGOTIATE:
        gap = (price - max_buy) / max(max_buy * margin, 1.0)
        base = 40 + 25 * max(0.0, 1 - gap)
    else:
        over = (price - max_buy) / max(max_buy, 1.0) if max_buy > 0 else 1.0
        base = max(0.0, 35 * (1 - over))
    score = base + _CONFIDENCE_ADJ.get(confidence, 0) - sum(settings.penalty(f) for f in set(flags))
    return int(round(min(100.0, max(0.0, score))))


def color_for(score: int, settings: Settings) -> RowColor:
    if score >= settings.score_green:
        return RowColor.GREEN
    if score >= settings.score_yellow:
        return RowColor.YELLOW
    return RowColor.RED
