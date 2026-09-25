from phonebot.core.models import MarketEstimate, Mode
from phonebot.core.parts import PartsCatalog, default_parts
from phonebot.core.settings import Settings
from phonebot.core.valuation import evaluate
from phonebot.core.view_filter import ViewFilter, matches

from .conftest import make_offer

PARTS = PartsCatalog(default_parts())


def pair(title="iPhone 13 128GB", price=1000, distance=None, value=2000, **kw):
    offer = make_offer(title, price, **kw)
    offer.distance_km = distance
    val = evaluate(offer, MarketEstimate(value, 12, "m", "wysoka", value), PARTS, Settings(), Mode.RESELL)
    return offer, val


def test_default_filter_accepts_everything():
    assert matches(*pair(), ViewFilter())
    assert not ViewFilter().is_active()


def test_models_price_condition_source():
    o, v = pair("iPhone 13 128GB", 1000, source="olx")
    assert matches(o, v, ViewFilter(models=["iPhone 13"]))
    assert not matches(o, v, ViewFilter(models=["iPhone 14"]))
    assert not matches(o, v, ViewFilter(price_min=1200))
    assert not matches(o, v, ViewFilter(price_max=900))
    assert matches(o, v, ViewFilter(price_min=900, price_max=1000))
    assert matches(o, v, ViewFilter(conditions=["good"]))
    assert not matches(o, v, ViewFilter(conditions=["damaged"]))
    assert not matches(o, v, ViewFilter(sources=["vinted"]))


def test_radius():
    near = pair(distance=20, shipping_available=False)
    far_pickup = pair(distance=300, shipping_available=False)
    far_shipping = pair(distance=300, shipping_available=True)
    unknown_shipping = pair(distance=None, shipping_available=True)
    f = ViewFilter(radius_km=50)
    assert matches(*near, f)
    assert not matches(*far_pickup, f)
    assert matches(*far_shipping, f) and matches(*unknown_shipping, f)
    strict = ViewFilter(radius_km=50, radius_keeps_shipping=False)
    assert not matches(*far_shipping, strict) and not matches(*unknown_shipping, strict)
    assert matches(*near, strict)


def test_min_profit_colors_shipping_text():
    o, v = pair(price=1000, value=2000)  # zysk ok. 980 zł
    assert matches(o, v, ViewFilter(min_profit_enabled=True, min_profit=500))
    assert not matches(o, v, ViewFilter(min_profit_enabled=True, min_profit=1500))
    assert matches(o, v, ViewFilter(colors=["green"]))
    assert not matches(o, v, ViewFilter(colors=["red"]))
    pickup = pair(shipping_available=False)
    assert not matches(*pickup, ViewFilter(shipping_only=True))
    assert matches(o, v, ViewFilter(text="iphone 13"))
    assert not matches(o, v, ViewFilter(text="pro max"))


def test_filter_persisted_in_settings():
    s = Settings(view_filter=ViewFilter(models=["iPhone 13"], radius_km=40, colors=["green"]))
    restored = Settings.from_json(s.to_json())
    assert restored.view_filter == s.view_filter
