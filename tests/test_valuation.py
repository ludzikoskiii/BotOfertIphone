import pytest

from phonebot.core.models import MarketEstimate, Mode, RedFlag, RowColor, Verdict
from phonebot.core.parts import PartsCatalog, default_parts
from phonebot.core.settings import ProfitRule, SalesChannel, Settings
from phonebot.core.valuation import evaluate, max_buy_price, required_profit

from .conftest import make_offer

PARTS = PartsCatalog(default_parts())


def market(value, confidence="wysoka"):
    return MarketEstimate(value, 12, "test", confidence, value)


def test_required_profit_modes():
    assert required_profit(1000, ProfitRule(150, 20, "amount")) == 150
    assert required_profit(1000, ProfitRule(150, 20, "percent")) == 200
    assert required_profit(1000, ProfitRule(150, 20, "max")) == 200
    assert required_profit(500, ProfitRule(150, 20, "max")) == 150
    assert required_profit(1000, ProfitRule(150, 20, "min")) == 150


@pytest.mark.parametrize("mode", ["amount", "percent", "max", "min"])
@pytest.mark.parametrize("net, extra", [(2000, 300), (1500, 0), (800, 150), (5000, 700)])
def test_max_buy_price_is_the_break_even_of_min_profit(mode, net, extra):
    rule = ProfitRule(150, 20, mode)
    b = max_buy_price(net, extra, rule)
    # przy cenie B zysk jest dokładnie równy wymaganemu (lub większy),
    assert net - b >= required_profit(b + extra, rule) - 1e-6
    # a przy wyższej cenie już nie wystarcza
    assert net - (b + 1) < required_profit(b + 1 + extra, rule)


def test_max_buy_price_never_negative():
    assert max_buy_price(100, 0, ProfitRule(150, 20, "max")) == 0


def test_repair_offer_full_calculation():
    s = Settings()  # OLX 0%, pakowanie 5, wysyłka zakupu 15, wysyłka części 12
    offer = make_offer("iPhone 13 128GB zbity ekran", price=900)
    v = evaluate(offer, market(2000), PARTS, s, Mode.REPAIR)
    assert v.repair_cost == 300 + 12
    assert v.total_costs == 312 + 15 + 5
    assert v.expected_profit == 2000 - 900 - 332
    assert v.required_profit == pytest.approx(0.2 * (900 + 312 + 15))
    # B = min(1668 - 150, (1668 - 0.2*327)/1.2) = 1335.5 → 1330
    assert v.max_buy_price == 1330
    assert v.verdict is Verdict.BUY
    assert v.color is RowColor.GREEN


def test_negotiate_when_slightly_above_max():
    s = Settings()
    offer = make_offer("iPhone 13 128GB zbity ekran", price=1450, description="Cena do negocjacji")
    v = evaluate(offer, market(2000), PARTS, s, Mode.REPAIR)
    assert v.max_buy_price == 1330
    assert v.verdict is Verdict.NEGOTIATE
    assert v.negotiation.max_price == 1330
    assert v.negotiation.opening_price < v.negotiation.max_price < offer.price


def test_skip_when_far_above_max():
    offer = make_offer("iPhone 13 128GB zbity ekran", price=1900)
    v = evaluate(offer, market(2000), PARTS, Settings(), Mode.REPAIR)
    assert v.verdict is Verdict.SKIP
    assert v.color is RowColor.RED


def test_selling_channel_commission_is_included():
    channel = SalesChannel("Allegro", commission_pct=10, fixed_fee=2)
    s = Settings(sales_channels=[channel], active_sales_channel="Allegro")
    offer = make_offer("iPhone 13 128GB", price=1000)
    v = evaluate(offer, market(2000), PARTS, s, Mode.RESELL)
    labels = {c.label: c.amount for c in v.cost_items}
    assert labels["Prowizja Allegro (10%)"] == 200
    assert labels["Opłata stała Allegro"] == 2


def test_pickup_cost_by_distance():
    s = Settings()
    offer = make_offer("iPhone 13 128GB", price=1000, shipping_available=False)
    offer.distance_km = 30
    v = evaluate(offer, market(2000), PARTS, s, Mode.RESELL)
    assert v.cost_items[0].amount == 60
    offer.distance_km = None
    v = evaluate(offer, market(2000), PARTS, s, Mode.RESELL)
    assert v.cost_items[0].amount == s.pickup_flat_cost


def test_unknown_defect_adds_risk_and_flag():
    s = Settings()
    offer = make_offer("iPhone 13 128GB", price=500, description="Telefon nie włącza się")
    v = evaluate(offer, market(2000), PARTS, s, Mode.REPAIR)
    assert RedFlag.UNKNOWN_REPAIR_COST in v.flags
    assert v.repair_cost == s.unknown_defect_risk_cost + s.parts_shipping_cost


def test_user_edited_part_price_is_used():
    from phonebot.core.models import Defect
    from phonebot.core.parts import PartPrice

    parts = PartsCatalog([PartPrice("iPhone 13", Defect.SCREEN, 150.0), PartPrice("*", Defect.SCREEN, 999.0)])
    offer = make_offer("iPhone 13 128GB zbity ekran", price=900)
    v = evaluate(offer, market(2000), parts, Settings(parts_shipping_cost=0), Mode.REPAIR)
    assert v.repair_cost == 150
    offer = make_offer("iPhone 14 128GB zbity ekran", price=900)
    v = evaluate(offer, market(2000), parts, Settings(parts_shipping_cost=0), Mode.REPAIR)
    assert v.repair_cost == 999  # fallback na cenę domyślną „*"


def test_resell_mode_rejects_damaged_phone():
    offer = make_offer("iPhone 13 128GB zbity ekran", price=300)
    v = evaluate(offer, market(2000), PARTS, Settings(), Mode.RESELL)
    assert v.verdict is Verdict.SKIP
    assert any("Szybki resell" in r for r in v.reasons)


def test_resell_mode_accepts_weak_battery():
    offer = make_offer("iPhone 13 128GB", price=1000, description="bateria 78%")
    v = evaluate(offer, market(2000), PARTS, Settings(), Mode.RESELL)
    assert v.verdict is Verdict.BUY


def test_suspiciously_cheap_flag():
    offer = make_offer("iPhone 13 128GB", price=500)
    v = evaluate(offer, market(2000), PARTS, Settings(), Mode.RESELL)
    assert RedFlag.SUSPICIOUSLY_CHEAP in v.flags


def test_hard_flag_lowers_score_but_keeps_verdict_by_default():
    clean = make_offer("iPhone 13 128GB", price=1000)
    locked = make_offer("iPhone 13 128GB", price=1000, description="blokada icloud")
    s = Settings()
    v_clean = evaluate(clean, market(2000), PARTS, s, Mode.RESELL)
    v_locked = evaluate(locked, market(2000), PARTS, s, Mode.RESELL)
    assert v_locked.verdict is Verdict.BUY
    assert v_locked.has_hard_flag
    assert v_locked.score == v_clean.score - s.penalty(RedFlag.ICLOUD_LOCK)


def test_hard_flag_can_force_skip():
    locked = make_offer("iPhone 13 128GB", price=1000, description="blokada icloud")
    v = evaluate(locked, market(2000), PARTS, Settings(hard_flags_force_skip=True), Mode.RESELL)
    assert v.verdict is Verdict.SKIP


def test_no_market_data():
    offer = make_offer("iPhone 13 128GB", price=1000)
    v = evaluate(offer, MarketEstimate(None, 0, "za mało danych", "brak"), PARTS, Settings(), Mode.RESELL)
    assert v.verdict is Verdict.SKIP
    assert v.expected_profit is None and v.max_buy_price is None
    assert "Brak wyceny" in v.reasons[0]


def test_low_confidence_lowers_score():
    offer = make_offer("iPhone 13 128GB", price=1000)
    hi = evaluate(offer, market(2000, "wysoka"), PARTS, Settings(), Mode.RESELL)
    lo = evaluate(offer, market(2000, "niska"), PARTS, Settings(), Mode.RESELL)
    assert lo.score < hi.score
