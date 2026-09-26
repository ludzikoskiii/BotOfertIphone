"""Wycena oferty: koszty, zysk, maksymalna cena zakupu i werdykt.

Oznaczenia:
    V  – wartość rynkowa po naprawie / przy odsprzedaży
    P  – cena ofertowa
    R  – koszt naprawy (części + wysyłka części + robocizna + ryzyko)
    Sb – koszt dostarczenia telefonu do Ciebie (wysyłka lub dojazd)
    Cs – koszty sprzedaży (prowizja od V, opłata stała, wysyłka, pakowanie)
    Zysk  = V − P − R − Sb − Cs
    Inwestycja = P + R + Sb
Maksymalna cena zakupu B to największa P, dla której Zysk ≥ wymagany zysk.
"""
from __future__ import annotations

from .models import (
    Condition,
    CostItem,
    MarketEstimate,
    Mode,
    Negotiation,
    Offer,
    RedFlag,
    Valuation,
    Verdict,
)
from .negotiation import color_for, compute_score, floor10, negotiation_margin, recommend
from .parts import PartsCatalog
from .settings import ProfitRule, Settings


def target_market_class(offer: Offer, mode: Mode) -> str:
    """Klasa stanu, w jakiej sprzedasz telefon (do porównania cen)."""
    if mode is Mode.RESELL and offer.parsed.condition is Condition.NEW:
        return "new"
    return "used"


def required_profit(investment: float, rule: ProfitRule) -> float:
    amount = rule.min_amount
    percent = rule.min_percent / 100 * investment
    return {"amount": amount, "percent": percent, "min": min(amount, percent)}.get(rule.mode, max(amount, percent))


def max_buy_price(net_value: float, extra_investment: float, rule: ProfitRule) -> float:
    """Największa cena zakupu B spełniająca warunek minimalnego zysku.

    ``net_value`` = V − R − Sb − Cs (wartość po odjęciu kosztów niezależnych od B),
    ``extra_investment`` = R + Sb (część inwestycji poza ceną zakupu).
    Warunek kwotowy: net − B ≥ A          →  B ≤ net − A
    Warunek procentowy: net − B ≥ p·(B+E) →  B ≤ (net − p·E) / (1 + p)
    """
    p = rule.min_percent / 100
    by_amount = net_value - rule.min_amount
    by_percent = (net_value - p * extra_investment) / (1 + p)
    value = {
        "amount": by_amount,
        "percent": by_percent,
        "min": max(by_amount, by_percent),
    }.get(rule.mode, min(by_amount, by_percent))
    return max(0.0, value)


def repair_costs(offer: Offer, parts: PartsCatalog, settings: Settings) -> tuple[list[CostItem], bool]:
    """Pozycje kosztów naprawy oraz informacja, czy któryś koszt jest nieznany."""
    items: list[CostItem] = []
    unknown = False
    model = offer.parsed.model
    for defect in offer.parsed.defects:
        row = parts.lookup(model, defect)
        if row is not None:
            note = f" ({row.note})" if row.note else ""
            src = "" if row.model == model else " – cena domyślna"
            items.append(CostItem(f"{defect.label}{note}{src}", row.price))
        else:
            unknown = True
            items.append(CostItem(f"{defect.label} – nieznany koszt (ryzyko)", settings.unknown_defect_risk_cost))
    if offer.parsed.condition is Condition.FOR_PARTS and not offer.parsed.defects:
        unknown = True
        items.append(CostItem("Nieokreślona usterka „na części” (ryzyko)", settings.unknown_defect_risk_cost))
    if items:
        if settings.parts_shipping_cost:
            items.append(CostItem("Wysyłka części", settings.parts_shipping_cost))
        if settings.own_labor_cost:
            items.append(CostItem("Własna robocizna", settings.own_labor_cost))
    return items, unknown


def acquisition_cost(offer: Offer, settings: Settings) -> CostItem:
    if offer.raw.shipping_available is False:
        if offer.distance_km is not None:
            cost = 2 * offer.distance_km * settings.pickup_cost_per_km
            return CostItem(f"Dojazd po odbiór ({offer.distance_km:.0f} km × 2)", round(cost, 2))
        return CostItem("Odbiór osobisty (koszt ryczałtowy)", settings.pickup_flat_cost)
    return CostItem("Wysyłka do Ciebie", settings.buy_shipping_cost)


def buyer_fee_rate(offer: Offer, settings: Settings) -> tuple[float, float]:
    """Opłata kupującego (np. ochrona kupujących na Vinted) jako (ułamek ceny, kwota stała).

    Dokładna kwota podana przez portal ma pierwszeństwo przed stawką z ustawień.
    """
    exact = offer.raw.params.get("buyer_fee")
    if exact and offer.price > 0:
        try:
            return float(exact) / offer.price, 0.0
        except ValueError:
            pass
    pct, fixed = (settings.buyer_fees.get(offer.raw.source) or [0.0, 0.0])[:2]
    return float(pct) / 100, float(fixed)


def buyer_fee_item(offer: Offer, settings: Settings) -> CostItem | None:
    rate, fixed = buyer_fee_rate(offer, settings)
    amount = round(offer.price * rate + fixed, 2)
    return CostItem("Opłata kupującego (ochrona kupujących)", amount) if amount > 0 else None


def selling_costs(value: float, settings: Settings) -> list[CostItem]:
    ch = settings.sales_channel()
    items = []
    if ch.commission_pct:
        items.append(CostItem(f"Prowizja {ch.name} ({ch.commission_pct:g}%)", round(value * ch.commission_pct / 100, 2)))
    if ch.fixed_fee:
        items.append(CostItem(f"Opłata stała {ch.name}", ch.fixed_fee))
    if ch.shipping_cost:
        items.append(CostItem("Wysyłka do kupującego", ch.shipping_cost))
    if settings.packaging_cost:
        items.append(CostItem("Pakowanie", settings.packaging_cost))
    return items


def evaluate(offer: Offer, market: MarketEstimate, parts: PartsCatalog, settings: Settings, mode: Mode) -> Valuation:
    price = offer.price
    flags = list(dict.fromkeys(offer.parsed.flags))
    reasons: list[str] = []

    repair_items, unknown_cost = repair_costs(offer, parts, settings)
    if unknown_cost:
        flags.append(RedFlag.UNKNOWN_REPAIR_COST)
    repair_total = round(sum(i.amount for i in repair_items), 2)
    acquisition = acquisition_cost(offer, settings)
    fee_item = buyer_fee_item(offer, settings)
    buy_items = [acquisition, *([fee_item] if fee_item else [])]
    buy_total = sum(i.amount for i in buy_items)
    rule = settings.profit_rule(mode)

    mode_mismatch = mode is Mode.RESELL and any(not d.cosmetic for d in offer.parsed.defects) or (
        mode is Mode.RESELL and offer.parsed.condition is Condition.FOR_PARTS
    )

    if market.value is None:
        reasons.append(f"Brak wyceny rynkowej: {market.method}. Uzupełnij wartość ręczną w ustawieniach.")
        cost_items = buy_items
        score = compute_score(Verdict.SKIP, price=price, max_buy=None, profit=None, required=None, margin=0,
                              confidence="brak", flags=flags, settings=settings)
        return Valuation(
            mode=mode, market=market, repair_items=repair_items, repair_cost=repair_total,
            cost_items=cost_items, total_costs=round(repair_total + buy_total, 2),
            expected_profit=None, roi_pct=None, required_profit=None, max_buy_price=None,
            verdict=Verdict.SKIP, negotiation=Negotiation(False, None, None, "Brak danych do negocjacji."),
            score=score, color=color_for(score, settings), flags=flags, reasons=reasons,
        )

    value = market.value
    sell_items = selling_costs(value, settings)
    cost_items = [*buy_items, *sell_items]
    other_costs = sum(i.amount for i in cost_items)
    total_costs = round(repair_total + other_costs, 2)

    profit = round(value - price - total_costs, 2)
    investment = price + repair_total + buy_total
    roi = round(profit / investment * 100, 1) if investment > 0 else None
    required = round(required_profit(investment, rule), 2)
    # Opłata kupującego rośnie z ceną: liczymy maksymalny „wydatek na zakup" X = B·(1+f),
    # traktując część stałą jak pozostałe koszty, a potem B = X / (1+f).
    fee_rate, fee_fixed = buyer_fee_rate(offer, settings)
    price_dependent_fee = fee_item.amount - fee_fixed if fee_item else 0.0
    fixed_costs = total_costs - price_dependent_fee
    max_outlay = max_buy_price(value - fixed_costs, repair_total + acquisition.amount + fee_fixed, rule)
    max_buy = floor10(max_outlay / (1 + fee_rate))

    ratio = settings.suspicious_price_ratio_damaged if offer.parsed.condition.market_class == "damaged" \
        else settings.suspicious_price_ratio_working
    if price < value * ratio:
        flags.append(RedFlag.SUSPICIOUSLY_CHEAP)

    verdict, negotiation = recommend(price, max_buy, offer.parsed.negotiable, settings)
    if mode_mismatch:
        verdict = Verdict.SKIP
        negotiation = Negotiation(False, None, None, "Tryb szybkiego resellu: telefon wymaga naprawy.")
        reasons.append("Telefon ma usterki wymagające naprawy — nie pasuje do trybu „Szybki resell”.")

    if RedFlag.PRICE_UNREALISTIC in flags and verdict is Verdict.BUY:
        # nierealnie niska cena: zamiast „okazji” — najpierw sprawdzić ogłoszenie
        verdict = Verdict.NEGOTIATE
        negotiation = Negotiation(False, None, negotiation.max_price,
                                  "Cena nierealnie niska — przed zakupem sprawdź ogłoszenie (czy to na pewno cały, "
                                  "sprawny telefon, a nie akcesorium, część lub oszustwo).")
        reasons.append("Cena nierealnie niska względem rynku — wymaga sprawdzenia, nie traktuj jako pewnej okazji.")

    hard = [f for f in flags if f.severity.value == "hard"]
    if hard and settings.hard_flags_force_skip and verdict is not Verdict.SKIP:
        verdict = Verdict.SKIP
        negotiation = Negotiation(False, None, None, "Twarda czerwona flaga — pominięto.")

    if not mode_mismatch:
        reasons.insert(0, _summary(verdict, profit, roi, required, price, max_buy))
    if repair_items:
        reasons.append(f"Naprawa: {', '.join(d.label for d in offer.parsed.defects) or 'nieokreślona'} "
                       f"— koszt ok. {repair_total:.0f} zł.")
    if market.confidence in ("niska", "brak"):
        reasons.append(f"Niska pewność wyceny rynkowej ({market.method}).")
    for f in flags:
        reasons.append(f"⚑ {f.label}")

    score = compute_score(
        verdict, price=price, max_buy=max_buy, profit=profit, required=required,
        margin=negotiation_margin(offer.parsed.negotiable, settings), confidence=market.confidence,
        flags=flags, settings=settings, mode_mismatch=mode_mismatch,
    )
    if RedFlag.PRICE_UNREALISTIC in flags:
        score = min(score, settings.score_green - 1)  # najwyżej „przeciętna”, nigdy zielona
    return Valuation(
        mode=mode, market=market, repair_items=repair_items, repair_cost=repair_total,
        cost_items=cost_items, total_costs=total_costs, expected_profit=profit, roi_pct=roi,
        required_profit=required, max_buy_price=max_buy, verdict=verdict, negotiation=negotiation,
        score=score, color=color_for(score, settings), flags=flags, reasons=reasons,
    )


def _summary(verdict: Verdict, profit: float, roi: float | None, required: float, price: float, max_buy: float) -> str:
    roi_txt = f" ({roi:.0f}%)" if roi is not None else ""
    if verdict is Verdict.BUY:
        return f"Przewidywany zysk {profit:.0f} zł{roi_txt} przy wymaganym {required:.0f} zł."
    if verdict is Verdict.NEGOTIATE:
        return (f"Przy cenie {price:.0f} zł zysk {profit:.0f} zł{roi_txt} jest poniżej wymaganego "
                f"{required:.0f} zł — opłaca się po zbiciu ceny do {max_buy:.0f} zł.")
    return f"Zysk {profit:.0f} zł{roi_txt} przy wymaganym {required:.0f} zł — maksymalnie {max_buy:.0f} zł."

