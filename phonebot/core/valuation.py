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

from ..ml.combine import combine as combine_layers
from .models import (
    Condition,
    CostItem,
    MarketEstimate,
    Mode,
    Negotiation,
    Offer,
    RedFlag,
    TimeItem,
    Valuation,
    Verdict,
)
from .negotiation import color_for, compute_score, floor10, negotiation_margin, recommend
from .parts import PartsCatalog
from .sanity import verdict_cap
from .settings import ProfitRule, Settings
from .transactions import Correction, Factor
from .work_time import estimate as estimate_time
from .work_time import per_hour
from .work_time import summary as time_summary


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
    stock = getattr(parts, "stock", None)
    from_stock = 0
    for defect in offer.parsed.defects:
        lot = stock.oldest(model, defect) if stock is not None else None
        if lot is not None:  # masz część: koszt = Twoja cena zakupu (najstarsza sztuka pierwsza)
            n = stock.available(model, defect)
            items.append(CostItem(f"{defect.label} — z magazynu ({lot.label}, {n} szt., Twoja cena)",
                                  lot.unit_price))
            from_stock += 1
            continue
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
        if settings.parts_shipping_cost and from_stock < len(items):  # wszystko z magazynu — bez wysyłki części
            items.append(CostItem("Wysyłka części", settings.parts_shipping_cost))
        if settings.own_labor_cost:
            items.append(CostItem("Własna robocizna", settings.own_labor_cost))
    return items, unknown


def acquisition_cost(offer: Offer, settings: Settings) -> CostItem:
    exact = offer.raw.params.get("shipping_cost")  # dokładny koszt wysyłki z portalu (Allegro, eBay)
    if exact and offer.raw.shipping_available is not False:
        try:
            return CostItem("Wysyłka do Ciebie (wg ogłoszenia)", round(float(exact), 2))
        except ValueError:
            pass
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


def import_items(offer: Offer) -> list[CostItem]:
    """Zakup spoza UE (eBay): szacunek VAT importowego, cła i opłaty za odprawę."""
    amount = offer.raw.params.get("import_cost")
    try:
        value = round(float(amount), 2) if amount else 0.0
    except ValueError:
        value = 0.0
    if not value:
        return []
    detail = offer.raw.params.get("import_detail")
    return [CostItem("Cło, VAT importowy i odprawa (szac.)" + (f" – {detail}" if detail else ""), value)]


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


def evaluate(offer: Offer, market: MarketEstimate, parts: PartsCatalog, settings: Settings, mode: Mode, *,
             apply_corrections: bool | None = None) -> Valuation:
    """``apply_corrections``: poprawki z Twoich transakcji (domyślnie wg ustawień; podgląd — odwrotnie)."""
    price = offer.price
    flags = list(dict.fromkeys(offer.parsed.flags))
    reasons: list[str] = []
    corr = _correction(offer, parts, settings, apply_corrections)
    notes: list[str] = []  # zastosowane poprawki (szczegóły oferty)

    repair_items, unknown_cost = repair_costs(offer, parts, settings)
    if unknown_cost:
        flags.append(RedFlag.UNKNOWN_REPAIR_COST)
    repair_total = round(sum(i.amount for i in repair_items), 2)
    baseline = {"repair_cost": repair_total}
    f = corr.factor("repair_cost") if corr else None
    if f is not None and repair_items:
        extra = round(repair_total * (f.applied - 1), 2)
        if abs(extra) >= 1:
            note = _note(f)
            repair_items.append(CostItem(f"Poprawka z Twoich transakcji ({note})", extra))
            repair_total = round(repair_total + extra, 2)
            notes.append(f"Koszt naprawy: {extra:+.0f} zł ({note}).")
    acquisition = acquisition_cost(offer, settings)
    fee_item = buyer_fee_item(offer, settings)
    buy_items = [acquisition, *([fee_item] if fee_item else []), *import_items(offer)]
    buy_total = sum(i.amount for i in buy_items)
    rule = settings.profit_rule(mode)

    time_items, minutes, time_cost = work_time(offer, parts, settings, mode, corr, notes)
    baseline["repair_minutes"] = _base_repair_minutes(time_items, corr)
    sell_days = None
    f = corr.factor("sell_days") if corr else None
    if f is not None:
        sell_days = round(settings.learning.default_sell_days * f.applied, 1)
        notes.append(f"Czas sprzedaży: ok. {sell_days:.0f} dni ({_note(f)}).")

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
            parts_in_stock=parts_in_stock(offer, parts),
            time_items=time_items, work_minutes=minutes, time_cost=time_cost,
            corrections=notes, baseline=baseline, sell_days=sell_days,
        )

    value = market.value
    if RedFlag.ESIM_ONLY_US in flags and settings.esim_us_value_pct:
        # iPhone z USA (tylko eSIM) sprzedaje się w Polsce taniej
        value = round(value * (1 - settings.esim_us_value_pct / 100), 2)
        reasons.append(f"Model z USA (tylko eSIM): wartość odsprzedaży −{settings.esim_us_value_pct:g}%.")
    baseline["resale"] = value
    f = corr.factor("resale") if corr else None
    if f is not None and abs(f.applied - 1) >= 0.005:
        corrected = round(value * f.applied, 2)
        note = _note(f)
        notes.append(f"Cena sprzedaży: {value:.0f} → {corrected:.0f} zł ({note}).")
        value = corrected
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
    rule_max_buy = floor10(max_outlay / (1 + fee_rate))
    # minimalny zysk na godzinę: zysk ≥ próg × czas  →  wydatek na zakup ≤ V − koszty − próg × czas
    min_rate = settings.work.min_profit_per_hour if minutes else 0.0
    time_required = round(min_rate * minutes / 60, 2) if min_rate else 0.0
    if time_required:
        max_outlay = min(max_outlay, max(0.0, value - fixed_costs - time_required))
    max_buy = floor10(max_outlay / (1 + fee_rate))
    time_limited = time_required > required
    required = max(required, time_required)
    rate = per_hour(profit, minutes)

    damaged = offer.parsed.condition.market_class == "damaged"
    sanity = settings.sanity
    # test sensowności ceny: dużo poniżej rynku to zwykle akcesorium, część, atrapa albo oszustwo
    unrealistic_ratio = sanity.price_min_ratio_damaged if damaged else sanity.price_min_ratio_working
    if price < value * unrealistic_ratio and RedFlag.PRICE_UNREALISTIC not in flags:
        flags.append(RedFlag.PRICE_UNREALISTIC)
    ratio = settings.suspicious_price_ratio_damaged if damaged else settings.suspicious_price_ratio_working
    if price < value * ratio and RedFlag.PRICE_UNREALISTIC not in flags:
        flags.append(RedFlag.SUSPICIOUSLY_CHEAP)
    # test sensowności zysku
    if roi is not None and roi > sanity.profit_max_pct:
        flags.append(RedFlag.PROFIT_UNREALISTIC)
    # nieznana pamięć: wycena z mediany wszystkich wersji modelu — przybliżona
    if sanity.unknown_storage_verify and offer.parsed.storage_gb is None:
        flags.append(RedFlag.STORAGE_UNKNOWN)

    verdict, negotiation = recommend(price, max_buy, offer.parsed.negotiable, settings)
    time_lowered: tuple[Verdict, Verdict] | None = None  # (bez progu, z progiem)
    if max_buy < rule_max_buy:  # bez progu zysku na godzinę werdykt byłby lepszy?
        without_time, _ = recommend(price, rule_max_buy, offer.parsed.negotiable, settings)
        if without_time.rank > verdict.rank:
            time_lowered = (without_time, verdict)
    if mode_mismatch:
        verdict = Verdict.SKIP
        negotiation = Negotiation(False, None, None, "Tryb szybkiego resellu: telefon wymaga naprawy.")
        reasons.append("Telefon ma usterki wymagające naprawy — nie pasuje do trybu „Szybki resell”.")

    # lokalne AI (tytuł + zdjęcie): sprzeczność albo niska pewność → flaga (najwyżej DO WERYFIKACJI)
    flags.extend(f for f in combine_layers(offer.layers, settings.ml).flags if f not in flags)

    # każda flaga ogranicza najlepszy możliwy werdykt
    cap, limiting = verdict_cap(flags, sanity, hard_force_skip=settings.hard_flags_force_skip)
    capped_from: Verdict | None = None
    if verdict.rank > cap.rank:
        capped_from, verdict = verdict, cap
        negotiation = _capped_negotiation(cap, negotiation, limiting, price, max_buy)
    if not mode_mismatch:
        reasons.insert(0, _summary(capped_from or verdict, profit, roi, required, price, max_buy))
    if capped_from is not None:
        why = ", ".join(f.label for f in limiting)
        reasons.insert(1, f"Werdykt obniżony z {capped_from.value} na {verdict.value}: {why}.")
    if rate is not None:
        line = time_summary(profit, minutes, rate)
        if min_rate and rate < min_rate:
            line += f" To poniżej progu {min_rate:.0f} zł/h"
            line += (f" — werdykt obniżony z {time_lowered[0].value} na {time_lowered[1].value}."
                     if time_lowered is not None else ".")
        reasons.insert(0 if mode_mismatch else 1 + (capped_from is not None), line)
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
    in_stock = parts_in_stock(offer, parts)
    if in_stock and len(in_stock) == len(offer.parsed.defects) and settings.inventory.score_bonus:
        score = min(100, score + settings.inventory.score_bonus)  # masz wszystkie potrzebne części
        reasons.append(f"Masz na stanie: {', '.join(d.label for d in in_stock)} "
                       f"(+{settings.inventory.score_bonus} pkt oceny).")
    if verdict is Verdict.VERIFY or RedFlag.PRICE_UNREALISTIC in flags:
        score = min(score, settings.score_green - 1)  # najwyżej „przeciętna”, nigdy zielona
    return Valuation(
        mode=mode, market=market, repair_items=repair_items, repair_cost=repair_total,
        cost_items=cost_items, total_costs=total_costs, expected_profit=profit, roi_pct=roi,
        required_profit=required, max_buy_price=max_buy, verdict=verdict, negotiation=negotiation,
        score=score, color=color_for(score, settings), flags=flags, reasons=reasons, parts_in_stock=in_stock,
        time_items=time_items, work_minutes=minutes, profit_per_hour=rate, time_cost=time_cost,
        time_limited=time_limited, corrections=notes, baseline=baseline, sell_days=sell_days, resale_value=value,
    )


def work_time(offer: Offer, parts: PartsCatalog, settings: Settings, mode: Mode, corr: Correction | None = None,
              notes: list[str] | None = None) -> tuple[list[TimeItem], int | None, float | None]:
    """Czas pracy (naprawa + obsługa), łączne minuty i koszt czasu wg stawki godzinowej."""
    cfg = settings.work
    if not cfg.enabled:
        return [], None, None
    adjust = None
    f = corr.factor("repair_time") if corr else None
    if f is not None and abs(f.applied - 1) >= 0.005:
        note = _note(f)

        def adjust(_model, _defect, minutes: int, note=note, factor=f.applied):
            return round(minutes * factor), note
    items = estimate_time(offer, parts, cfg, mode, adjust)
    if adjust is not None and notes is not None and any(i.repair for i in items):
        notes.append(f"Czas naprawy: {note}.")
    minutes = sum(i.minutes for i in items)
    return items, minutes, round(minutes / 60 * cfg.hourly_rate, 2)


def _correction(offer: Offer, parts: PartsCatalog, settings: Settings, apply: bool | None) -> Correction | None:
    """Poprawka z Twoich transakcji dla modelu + usterek oferty (``PartsCatalog.corrections``)."""
    if not (settings.learning.enabled if apply is None else apply):
        return None
    source = getattr(parts, "corrections", None)
    return source.for_offer(offer.parsed.model, offer.parsed.defects) if source is not None else None


def _note(f: Factor) -> str:
    """„+11% — średnio +18% w 5 transakcjach, waga 63%”."""
    where = f"w {f.n} transakcji" if f.n == 1 else f"w {f.n} transakcjach"
    return f"{(f.applied - 1) * 100:+.0f}% — średnio {(f.ratio - 1) * 100:+.0f}% {where}, waga {f.weight * 100:.0f}%"


def _base_repair_minutes(items: list[TimeItem], corr: Correction | None) -> int | None:
    """Czas naprawy bez poprawki z transakcji (do nauki poprawek)."""
    repair = [i.minutes for i in items if i.repair]
    if not repair:
        return None
    total = sum(repair)
    f = corr.factor("repair_time") if corr else None
    return round(total / f.applied) if f is not None and f.applied else total


def parts_in_stock(offer: Offer, parts: PartsCatalog) -> list:
    """Usterki, do których masz część w magazynie."""
    stock = getattr(parts, "stock", None)
    if stock is None:
        return []
    return [d for d in offer.parsed.defects if stock.oldest(offer.parsed.model, d) is not None]


def _lower_first(text: str) -> str:
    """Mała litera na początku, ale skróty zostają („AI: …”, „IMEI …”)."""
    word = text.split(" ", 1)[0].rstrip(":")
    return text if len(word) > 1 and word.isupper() else text[:1].lower() + text[1:]


def _capped_negotiation(cap: Verdict, original: Negotiation, limiting: list[RedFlag], price: float,
                        max_buy: float) -> Negotiation:
    why = ", ".join(_lower_first(f.label) for f in limiting)
    if cap is Verdict.NEGOTIATE:
        return Negotiation(True, original.opening_price, original.max_price,
                           f"Cena mieści się w maksymalnej ({max_buy:.0f} zł), ale oferta ma ostrzeżenie ({why}). "
                           "Dopytaj sprzedającego i negocjuj, zanim kupisz.")
    if cap is Verdict.VERIFY:
        return Negotiation(False, None, original.max_price,
                           f"Do weryfikacji ({why}). Zanim zaproponujesz cenę, sprawdź zdjęcia, opis i sprzedającego — "
                           "czy to na pewno cały, sprawny telefon, a nie akcesorium, część lub oszustwo.")
    return Negotiation(False, None, None, f"Pominięto: {why}.")


def _summary(verdict: Verdict, profit: float, roi: float | None, required: float, price: float, max_buy: float) -> str:
    roi_txt = f" ({roi:.0f}%)" if roi is not None else ""
    if verdict is Verdict.BUY:
        return f"Przewidywany zysk {profit:.0f} zł{roi_txt} przy wymaganym {required:.0f} zł."
    if verdict is Verdict.NEGOTIATE:
        return (f"Przy cenie {price:.0f} zł zysk {profit:.0f} zł{roi_txt} jest poniżej wymaganego "
                f"{required:.0f} zł — opłaca się po zbiciu ceny do {max_buy:.0f} zł.")
    return f"Zysk {profit:.0f} zł{roi_txt} przy wymaganym {required:.0f} zł — maksymalnie {max_buy:.0f} zł."

