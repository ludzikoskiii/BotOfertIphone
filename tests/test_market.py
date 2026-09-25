import pytest

from phonebot.core.market import estimate_market_value, remove_outliers
from phonebot.core.models import Condition, MarketObservation
from phonebot.core.settings import Settings


def obs(price, storage=128, condition=Condition.GOOD, offer_id=None):
    return MarketObservation(price, storage, condition, offer_id)


def test_remove_outliers():
    prices = [1000, 1050, 1100, 1080, 1020, 10, 5000]
    assert sorted(remove_outliers(prices)) == [1000, 1020, 1050, 1080, 1100]


def test_remove_outliers_small_sample_untouched():
    assert remove_outliers([1, 1000, 5000]) == [1, 1000, 5000]


def test_median_with_correction():
    s = Settings()
    data = [obs(p) for p in (1000, 1100, 1200, 1300, 1400, 5)]  # 5 zł = poniżej min_valid_price
    est = estimate_market_value("iPhone 13", 128, "used", data, s)
    assert est.raw_median == 1200
    assert est.value == pytest.approx(1200 * 0.9)
    assert est.sample_size == 5
    assert est.confidence == "średnia"


def test_confidence_levels():
    s = Settings()
    many = [obs(1000 + i) for i in range(10)]
    assert estimate_market_value("iPhone 13", 128, "used", many, s).confidence == "wysoka"
    few = [obs(1000), obs(1100)]
    assert estimate_market_value("iPhone 13", 128, "used", few, s).confidence == "niska"


def test_only_same_condition_class_counts():
    s = Settings()
    data = [obs(1200), obs(1250), obs(400, condition=Condition.DAMAGED), obs(300, condition=Condition.FOR_PARTS)]
    used = estimate_market_value("iPhone 13", 128, "used", data, s)
    assert used.raw_median == 1225
    damaged = estimate_market_value("iPhone 13", 128, "damaged", data, s)
    assert damaged.raw_median == 350


def test_like_new_and_good_are_comparable():
    s = Settings()
    data = [obs(1200, condition=Condition.LIKE_NEW), obs(1000, condition=Condition.GOOD)]
    assert estimate_market_value("iPhone 13", 128, "used", data, s).raw_median == 1100


def test_manual_value_has_priority():
    s = Settings(manual_market_values={"iPhone 13|128": 1500})
    est = estimate_market_value("iPhone 13", 128, "used", [obs(1000)] * 10, s)
    assert est.value == 1500
    assert est.confidence == "wysoka"


def test_fallback_other_storage_is_adjusted():
    s = Settings(storage_step_pct=10, asking_price_correction=1.0)
    data = [obs(1000, storage=128), obs(1000, storage=128), obs(1000, storage=128)]
    est = estimate_market_value("iPhone 13", 256, "used", data, s)
    assert est.value == pytest.approx(1100)
    assert est.confidence == "niska"


def test_new_falls_back_to_used_times_multiplier():
    s = Settings(asking_price_correction=1.0, new_condition_multiplier=1.2)
    data = [obs(1000), obs(1000)]
    est = estimate_market_value("iPhone 13", 128, "new", data, s)
    assert est.value == pytest.approx(1200)


def test_excludes_offer_itself():
    s = Settings(asking_price_correction=1.0)
    data = [obs(1000, offer_id=1), obs(1000, offer_id=2), obs(100, offer_id=3)]
    est = estimate_market_value("iPhone 13", 128, "used", data, s, exclude_offer_id=3)
    assert est.value == 1000


def test_no_data():
    est = estimate_market_value("iPhone 13", 128, "used", [], Settings())
    assert est.value is None and est.confidence == "brak"
    assert estimate_market_value(None, 128, "used", [obs(1)], Settings()).value is None
