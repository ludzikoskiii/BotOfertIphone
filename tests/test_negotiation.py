import pytest

from phonebot.core.models import RowColor, Verdict
from phonebot.core.negotiation import color_for, nice_floor, recommend
from phonebot.core.settings import Settings


@pytest.mark.parametrize("value, expected", [(487, 480), (1234, 1200), (2380, 2300), (0, 0), (-5, 0)])
def test_nice_floor(value, expected):
    assert nice_floor(value) == expected


def test_buy_below_max():
    verdict, neg = recommend(900, 1000, None, Settings())
    assert verdict is Verdict.BUY
    assert neg.opening_price == 850  # 5% taniej, zaokrąglone
    assert neg.max_price == 900


def test_buy_not_negotiable():
    verdict, neg = recommend(900, 1000, False, Settings())
    assert verdict is Verdict.BUY and neg.opening_price is None


def test_negotiate_within_margin():
    verdict, neg = recommend(1100, 1000, None, Settings())  # 10% powyżej, margines 15%
    assert verdict is Verdict.NEGOTIATE
    assert neg.max_price == 1000
    assert neg.opening_price == 850  # 1000 × 0.88 = 880 → 850
    assert "1000" in neg.note


def test_negotiable_offer_gets_bigger_margin():
    s = Settings()
    assert recommend(1200, 1000, None, s)[0] is Verdict.SKIP  # 20% > 15%
    assert recommend(1200, 1000, True, s)[0] is Verdict.NEGOTIATE  # 20% < 25%


def test_final_price_means_no_negotiation():
    assert recommend(1050, 1000, False, Settings())[0] is Verdict.SKIP


def test_zero_max_buy():
    assert recommend(100, 0, True, Settings())[0] is Verdict.SKIP


def test_colors():
    s = Settings()
    assert color_for(80, s) is RowColor.GREEN
    assert color_for(50, s) is RowColor.YELLOW
    assert color_for(10, s) is RowColor.RED
