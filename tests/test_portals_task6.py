"""Zadanie 6: Lento, Allegro (REST API), eBay (Browse API), ceny referencyjne, scalanie duplikatów."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import httpx
import pytest

from phonebot.core.currency import EU_COUNTRIES, import_costs, parse_nbp
from phonebot.core.dedup import merge_across_portals
from phonebot.core.models import MarketEstimate, Mode, RawOffer, RedFlag
from phonebot.core.normalizer import parse_offer
from phonebot.core.settings import Settings
from phonebot.net.http import HostRateLimiter, HttpClient
from phonebot.services.reference_prices import (
    ReferencePrice,
    ReferenceRepository,
    blend,
    lookup,
    parse_refurbed,
    refresh,
    slug,
)
from phonebot.services.scanner import default_adapters
from phonebot.sources import REGISTRY, SOURCE_NAMES
from phonebot.sources.base import SearchQuery, SourceBlocked
from phonebot.storage.db import open_database
from phonebot.storage.repositories import OfferRepository

from .conftest import make_offer

LENTO_ROW = """
<div class="tablelist-tr margin-top-1-sm hash" data-id="{id}"><div class="thumb-list-row">
<div class="data-list-item hidden-xs"><span>dzisiaj </span> 10:44</div><div class="thumb-list thumb-list-small">
<span class="thumb-list-link"><img src="https://st-lento.pl/adpics/thumbnail/09_2026/13/{id}.jpg" alt=""></span></div></div>
<div class="desc-list-row"><a href="https://{city}.lento.pl/{slug},{id}.html" class="title-list-item">{title}</a>
<p class="hidden-xs margin-bottom-5 text-14">{desc}</p><div class="visible-xs-block param-list-item">
<a href="https://{city}.lento.pl/x.html" class="mark-pointer licon-pin-f">{city_name}</a></div></div>
<div class="price-list-item"><div class="price-list-item-price">{price} zł</div></div></div>"""


def lento_page(rows, next_page=True):
    body = "".join(LENTO_ROW.format(**r) for r in rows)
    more = '<a href="https://www.lento.pl/...apple.html?page=2">2</a>' if next_page else ""
    return f"<html><body><div class='tablelist'>{body}</div>{more}</body></html>"


ROWS = [dict(id="16156228", city="gora-kalwaria", city_name="Góra Kalwaria", slug="iphone-7-32-gb-bateria-73",
             title="Iphone 7 32 GB bateria 73%", desc="Używany iPhone 7, bateria 73%.", price="100"),
        dict(id="16156300", city="nowy-targ", city_name="Nowy Targ", slug="iphone-13-128gb",
             title="iPhone 13 128GB niebieski", desc="Stan bardzo dobry", price="1 450")]


def run(adapter, query=None):
    return asyncio.run(adapter.search(query or SearchQuery(Mode.REPAIR, max_pages=2)))


def client(handler):
    return HttpClient(HostRateLimiter(0), transport=httpx.MockTransport(handler), wait=lambda s: 0)


# ------------------------------------------------------------------ Lento ---

def test_lento_category_pages():
    seen = []

    def handler(req):
        seen.append(str(req.url))
        if req.url.host == "www.lento.pl" and req.url.path.endswith("/apple.html"):
            page = req.url.params.get("page")
            return httpx.Response(200, text=lento_page(ROWS if not page else ROWS[:1], next_page=not page))
        return httpx.Response(404)

    offers = run(REGISTRY["lento"](client(handler), Settings()))
    assert [o.source_id for o in offers] == ["16156228", "16156300"]
    o = offers[1]
    assert o.price == 1450 and o.city == "Nowy Targ" and o.url.endswith(",16156300.html")
    assert o.photos[0].endswith("/original/09_2026/13/16156300.jpg")
    assert len(seen) == 2 and "szukaj" not in "".join(seen)  # tylko kategoria, bez wyszukiwarki (robots.txt)


def test_lento_changed_layout_is_reported():
    from phonebot.sources.base import SourceFormatChanged

    adapter = REGISTRY["lento"](client(lambda r: httpx.Response(200, text="<html>nowy wygląd</html>")), Settings())
    with pytest.raises(SourceFormatChanged):
        run(adapter)


# ----------------------------------------------------------------- Allegro ---

def allegro_handler(listing_status=200, calls=None):
    def handler(req):
        if calls is not None:
            calls.append(req)
        if req.url.host == "allegro.pl" and req.url.path == "/auth/oauth/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 43199})
        if req.url.path.endswith("/parameters"):
            return httpx.Response(200, json={"parameters": [{"id": "11323", "name": "Stan", "dictionary": [
                {"id": "11323_1", "value": "Nowy"}, {"id": "11323_2", "value": "Używany"},
                {"id": "11323_238066", "value": "Uszkodzony"}]}]})
        if req.url.path == "/offers/listing":
            if listing_status != 200:
                return httpx.Response(listing_status, json={"errors": [{"message": "Access denied"}]})
            item = {"id": "123456", "name": "iPhone 13 128GB bateria 88%", "sellingMode": {"price": {
                "amount": "1499.00", "currency": "PLN"}}, "delivery": {"lowestPrice": {"amount": "12.99",
                "currency": "PLN"}}, "seller": {"id": "9", "login": "jan", "company": False},
                "images": [{"url": "https://a.allegroimg.com/1.jpg"}]}
            return httpx.Response(200, json={"items": {"promoted": [], "regular": [item]},
                                             "searchMeta": {"availableCount": 1}})
        return httpx.Response(404)
    return handler


def test_allegro_rest_api():
    calls = []
    s = Settings(allegro_client_id="id", allegro_client_secret="secret")
    offers = run(REGISTRY["allegro"](client(allegro_handler(calls=calls)), s),
                 SearchQuery(Mode.REPAIR, phrases=["iphone"], max_pages=1))
    assert len(offers) == 1 and offers[0].url == "https://allegro.pl/oferta/123456"
    assert offers[0].price == 1499 and offers[0].params["shipping_cost"] == "12.99"
    listing = next(r for r in calls if r.url.path == "/offers/listing")
    assert listing.url.params.get_list("parameter.11323") == ["11323_2", "11323_238066"]  # używany + uszkodzony
    assert listing.url.params["category.id"] == "165" and listing.headers["Authorization"] == "Bearer tok"
    token = next(r for r in calls if r.url.path == "/auth/oauth/token")
    assert token.headers["Authorization"].startswith("Basic ") and "secret" not in str(token.url)


def test_allegro_access_denied_is_explained():
    s = Settings(allegro_client_id="id", allegro_client_secret="secret")
    with pytest.raises(SourceBlocked, match="nie udostępnia wyszukiwania"):
        run(REGISTRY["allegro"](client(allegro_handler(403)), s), SearchQuery(Mode.REPAIR, max_pages=1))


def test_keyed_portals_only_with_keys():
    http = client(lambda r: httpx.Response(404))
    keys = {a.key for a in default_adapters(http, Settings())}
    assert "lento" in keys and "allegro" not in keys and "ebay" not in keys
    s = Settings(allegro_client_id="a", allegro_client_secret="b", ebay_client_id="c", ebay_client_secret="d")
    s.enabled_sources.update(allegro=True, ebay=True)
    assert {"allegro", "ebay"} <= {a.key for a in default_adapters(http, s)}
    assert list(SOURCE_NAMES) == ["allegro_lokalnie", "allegro", "vinted", "sprzedajemy", "lento", "ebay"]


# -------------------------------------------------------------------- eBay ---

NBP = [{"table": "A", "effectiveDate": "2026-09-25", "rates": [
    {"code": "EUR", "mid": 4.30}, {"code": "USD", "mid": 3.80}, {"code": "GBP", "mid": 5.00}]}]


def ebay_item(item_id, title, price, currency, ship, country, city=None):
    return {"itemId": item_id, "title": title, "price": {"value": str(price), "currency": currency},
            "shippingOptions": [{"shippingCost": {"value": str(ship), "currency": currency}}],
            "itemLocation": {"country": country, **({"city": city} if city else {})},
            "image": {"imageUrl": f"https://i.ebayimg.com/{item_id}.jpg"}, "itemWebUrl": f"https://ebay.de/itm/{item_id}",
            "condition": "Gebraucht", "seller": {"username": "shop", "feedbackScore": 120, "feedbackPercentage": "99.1"}}


def ebay_handler(calls):
    def handler(req):
        calls.append(req)
        if req.url.host == "api.nbp.pl":
            return httpx.Response(200, json=NBP)
        if req.url.path == "/identity/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "etok"})
        if req.url.path.endswith("/item_summary/search"):
            market = req.headers["X-EBAY-C-MARKETPLACE-ID"]
            items = {"EBAY_DE": [ebay_item("de1", "Apple iPhone 13 128GB", 300, "EUR", 20, "DE", "Berlin")],
                     "EBAY_US": [ebay_item("us1", "iPhone 15 Pro 256GB US model", 600, "USD", 50, "US")]}[market]
            return httpx.Response(200, json={"itemSummaries": items, "total": len(items)})
        return httpx.Response(404)
    return handler


def test_ebay_browse_api_costs_and_flags():
    calls = []
    s = Settings(ebay_client_id="app", ebay_client_secret="cert", ebay_markets=["EBAY_DE", "EBAY_US"])
    offers = {o.source_id: o for o in run(REGISTRY["ebay"](client(ebay_handler(calls)), s),
                                          SearchQuery(Mode.RESELL, phrases=["iphone"], max_pages=1))}
    de, us = offers["de1"], offers["us1"]
    assert de.price == 1290 and de.params["shipping_cost"] == "86.00" and "import_cost" not in de.params  # z UE
    assert us.price == 2280 and us.params["shipping_cost"] == "190.00"
    assert float(us.params["import_cost"]) == pytest.approx((2280 + 190) * 0.23 + 30, abs=0.01)  # VAT + odprawa
    search = next(r for r in calls if r.url.path.endswith("/search"))
    assert "deliveryCountry:PL" in search.url.params["filter"] and search.url.params["category_ids"] == "9355"
    assert "country%3DPL" in search.headers["X-EBAY-C-ENDUSERCTX"]
    flags_us = parse_offer(us).flags
    assert RedFlag.REMOTE_PURCHASE in flags_us and RedFlag.ESIM_ONLY_US in flags_us
    assert RedFlag.ESIM_ONLY_US not in parse_offer(de).flags


def test_ebay_valuation_includes_shipping_import_and_esim():
    from phonebot.core.parts import PartsCatalog, default_parts
    from phonebot.core.valuation import evaluate

    raw = RawOffer("ebay", "us1", "https://ebay", "iPhone 15 Pro 256GB US model", 2280, shipping_available=True,
                   params={"shipping_cost": "190.00", "import_cost": "598.10", "item_country": "US",
                           "remote_purchase": "1"})
    offer = make_offer("x")
    offer.raw, offer.parsed = raw, parse_offer(raw)
    s = Settings()
    val = evaluate(offer, MarketEstimate(4000, 10, "t", "wysoka", 4000), PartsCatalog(default_parts()), s, Mode.RESELL)
    labels = {i.label: i.amount for i in val.cost_items}
    assert labels["Wysyłka do Ciebie (wg ogłoszenia)"] == 190
    assert any(k.startswith("Cło, VAT importowy") and v == 598.10 for k, v in labels.items())
    assert any("tylko eSIM" in r for r in val.reasons)
    assert val.expected_profit == pytest.approx(4000 * 0.85 - 2280 - val.total_costs, abs=0.01)


def test_currency_and_import_rules():
    rates = parse_nbp(NBP)
    assert rates.to_pln(100, "EUR") == 430 and rates.date == "2026-09-25" and rates.to_pln(5, "XYZ") is None
    assert import_costs(1000, 100, "DE").total == 0 and "PL" in EU_COUNTRIES
    c = import_costs(1000, 100, "GB", vat_pct=23, duty_pct=0, clearance_fee=30)
    assert c.vat == 253 and c.total == 283


# ------------------------------------------------------- ceny referencyjne ---

REFURBED = """<html><script type="application/ld+json">[{"@type":"ProductGroup","hasVariant":[
{"@type":"Product","size":"128 GB","color":"różowy","offers":{"price":1338.2,"priceCurrency":"PLN"}},
{"@type":"Product","size":"128 GB","color":"czarny","offers":{"price":1278.06,"priceCurrency":"PLN"}},
{"@type":"Product","size":"256 GB","color":"czarny","offers":{"price":1780.61,"priceCurrency":"PLN"}},
{"@type":"Product","size":"1 TB","color":"czarny","offers":{"price":2500,"priceCurrency":"PLN"}}]}]</script></html>"""


def test_refurbed_parsing_and_slug():
    assert parse_refurbed(REFURBED) == {128: 1278.06, 256: 1780.61, 1024: 2500}
    assert slug("iPhone 13 Pro Max") == "iphone-13-pro-max" and slug("iPhone SE (2020)") == "iphone-se-2020"


def test_blend_lookup_and_fallbacks():
    s = Settings()  # 80%, waga 50%
    ref = ReferencePrice("refurbed", "iPhone 13", 128, 1500, None, datetime(2026, 9, 25, tzinfo=UTC))
    m = blend(MarketEstimate(1400, 8, "mediana 8 ofert", "wysoka", 1400), ref, s)
    assert m.value == pytest.approx(0.5 * 1200 + 0.5 * 1400) and "Refurbed: 1 500 zł" in m.reference_note
    assert "25.09.2026" in m.reference_note
    only_ref = blend(MarketEstimate(None, 0, "za mało danych rynkowych", "brak"), ref, s)
    assert only_ref.value == 1200 and only_ref.confidence == "średnia"
    none = blend(MarketEstimate(1400, 8, "mediana", "wysoka"), None, s)  # brak ceny referencyjnej — bez zmian
    assert none.value == 1400 and none.reference_note is None
    s.reference_manual = {"iPhone 13|128": 1600}
    assert lookup("iPhone 13", 128, {("iPhone 13", 128): [ref]}, s).source == "manual"
    s.reference_manual = {}
    assert lookup("iPhone 13", 256, {("iPhone 13", 128): [ref]}, s) is None


def test_refresh_saves_prices_and_keeps_last_on_block(tmp_path):
    conn = open_database(tmp_path / "r.sqlite3")
    raw = RawOffer("vinted", "1", "https://x", "iPhone 12 128GB", 1000, photos=["x"])
    OfferRepository(conn).upsert(raw, parse_offer(raw))
    ok = client(lambda r: httpx.Response(200, text=REFURBED) if r.url.path == "/p/iphone-12/" else httpx.Response(404))
    assert asyncio.run(refresh(conn, Settings(), http=ok, force=True)) == {"iPhone 12": 3}
    stored = ReferenceRepository(conn).all()
    assert stored[("iPhone 12", 128)][0].price == 1278.06
    blocked = client(lambda r: httpx.Response(403, text="cloudflare"))
    assert asyncio.run(refresh(conn, Settings(), http=blocked, force=True)) == {}
    assert ReferenceRepository(conn).all()[("iPhone 12", 128)][0].price == 1278.06  # ostatnia zapisana cena
    # ocena oferty korzysta z ceny referencyjnej
    from phonebot.services.evaluator import Evaluator

    offer = OfferRepository(conn).list()[0]
    val = Evaluator(conn, Settings()).evaluate(offer)
    assert val.market.reference_note and "Refurbed" in val.market.reference_note
    conn.close()


# --------------------------------------------------------------- duplikaty ---

def test_same_phone_on_two_portals_shown_once():
    from phonebot.core.models import Verdict

    from .test_sorting import _val

    a = make_offer("iPhone 13 128GB", 1200, source="allegro_lokalnie", city="Nowy Targ", url="https://a")
    b = make_offer("iPhone 13 128GB", 1199, source="lento", city="Nowy Targ", url="https://b")
    c = make_offer("iPhone 13 128GB", 1200, source="vinted")  # bez miasta — nie łączymy
    d = make_offer("iPhone 13 128GB", 1200, source="allegro_lokalnie", city="Nowy Targ")  # ten sam portal
    for o in (a, b, c, d):
        from phonebot.storage.repositories import dedup_key

        o.dedup_key = dedup_key(o.parsed, o.raw)
    rows = merge_across_portals([(a, _val(Verdict.NEGOTIATE)), (b, _val(Verdict.BUY)), (c, _val()), (d, _val())])
    main = next(o for o, _ in rows if o.also_on)
    assert main is b and main.also_on == [("allegro_lokalnie", "https://a", 1200)]
    assert len(rows) == 3 and a not in [o for o, _ in rows] and d in [o for o, _ in rows]
    assert json.dumps(main.also_on)


def test_portals_settings_tab(tmp_path):
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    from phonebot.storage.repositories import SettingsRepository
    from phonebot.ui.settings_dialog import SettingsDialog

    dialog = SettingsDialog(Settings())
    dialog.findChild(widgets.QLineEdit, "allegro_client_id").setText("abc")
    dialog.findChild(widgets.QLineEdit, "allegro_client_secret").setText("xyz")
    dialog.ebay_market_checks["EBAY_GB"].setChecked(True)
    dialog.findChild(widgets.QSpinBox, "remote_purchase_penalty").setValue(15)
    dialog.reference_manual_table.add_row(["iPhone 13", "128", "1 550"])
    s = dialog.result_settings()
    dialog.deleteLater()
    app.processEvents()
    assert s.allegro_client_id == "abc" and s.ebay_markets == ["EBAY_DE", "EBAY_GB"]
    assert s.flag_penalties["remote_purchase"] == 15 and s.reference_manual == {"iPhone 13|128": 1550}
    conn = open_database(tmp_path / "s.sqlite3")
    SettingsRepository(conn).save(s)
    assert "xyz" not in SettingsRepository(conn).get_value("app")  # klucze API zaszyfrowane osobno
    assert SettingsRepository(conn).load().allegro_client_secret == "xyz"
    conn.close()
