import asyncio
import json
from pathlib import Path

import httpx
import pytest

from phonebot.core.models import Mode
from phonebot.core.settings import Settings
from phonebot.net.http import HostRateLimiter, HttpClient
from phonebot.sources.allegro_lokalnie import AllegroLokalnieAdapter
from phonebot.sources.base import SearchQuery, SourceBlocked, SourceError, SourceFormatChanged
from phonebot.sources.extract import offers_from_html, parse_price
from phonebot.sources.vinted import VintedAdapter, parse_item

FIX = Path(__file__).parent / "fixtures"
AL_HTML = (FIX / "allegro_lokalnie_page1.html").read_text(encoding="utf-8")
AL_LD = (FIX / "allegro_lokalnie_jsonld.html").read_text(encoding="utf-8")
VINTED = json.loads((FIX / "vinted_page1.json").read_text(encoding="utf-8"))


def client(handler):
    return HttpClient(HostRateLimiter(0), transport=httpx.MockTransport(handler), wait=lambda s: 0)


@pytest.mark.parametrize("value, expected", [
    (1500, 1500.0), ("1500.0", 1500.0), ("1 499,99 zł", 1499.99), ("1.500 zł", 1500.0),
    ({"amount": "820.00", "currency": "PLN"}, 820.0), ({"value": 12}, 12.0), ("brak", None), (None, None),
])
def test_parse_price(value, expected):
    assert parse_price(value)[0] == expected


def test_extract_from_next_data():
    offers = {o.id: o for o in offers_from_html(AL_HTML, "https://allegrolokalnie.pl")}
    assert set(offers) == {"a1b2c3", "d4e5f6", "g7h8"}  # filtr „Cena" bez ceny nie jest ofertą
    o = offers["a1b2c3"]
    assert o.price == 820 and o.city == "Nowy Targ" and o.condition == "Uszkodzony"
    assert o.url == "https://allegrolokalnie.pl/oferta/iphone-13-128gb-zbity-ekran-a1b2c3"
    assert o.photos == ["https://a.allegroimg.com/s400/1.jpg"]
    assert o.created_at.year == 2026


def test_extract_from_json_ld():
    (o,) = offers_from_html(AL_LD, "https://allegrolokalnie.pl")
    assert o.title == "iPhone 11 Pro 256GB" and o.price == 1150
    assert o.id == "iphone-11-pro-256gb-x9y8"
    assert o.condition == "UsedCondition"


def run(coro):
    return asyncio.run(coro)


def test_allegro_lokalnie_adapter():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, text=AL_HTML)

    async def go():
        async with client(handler) as http:
            return await AllegroLokalnieAdapter(http, Settings()).search(
                SearchQuery(Mode.REPAIR, phrases=["iphone"], max_pages=3, price_max=3000))

    offers = {o.source_id: o for o in run(go())}
    assert len(calls) == 2  # druga strona nie wniosła nowych ofert → koniec
    assert calls[0].path == "/oferty/q/iphone" and calls[0].params["price_to"] == "3000"
    o = offers["a1b2c3"]
    assert o.source == "allegro_lokalnie"
    assert o.params["condition"] == "damaged" and o.shipping_available is True
    assert offers["d4e5f6"].shipping_available is False


def test_allegro_lokalnie_changed_layout_is_reported():
    async def go():
        async with client(lambda r: httpx.Response(200, text="<html><body>nowy wygląd</body></html>")) as http:
            return await AllegroLokalnieAdapter(http, Settings()).search(SearchQuery(Mode.RESELL))

    with pytest.raises(SourceError, match="zmiana formatu"):
        run(go())


VINTED_NEW = json.loads((FIX / "vinted_svc_catalogue.json").read_text(encoding="utf-8"))


def test_vinted_parse_item_new_format():
    raw = parse_item(VINTED_NEW["items"][0])
    assert raw.source == "vinted" and raw.price == 1450 and raw.currency == "PLN"
    assert raw.url == "https://www.vinted.pl/items/10090626301-iphone-13-128-gb"  # link względny → pełny
    assert raw.params == {"condition": "used", "buyer_fee": "75.40"}  # opłata z pola service_fee
    assert raw.photos == ["https://images1.vinted.net/t/1/310x430.webp"]
    assert raw.shipping_available is True
    assert parse_item(VINTED_NEW["items"][2]).currency == "USD"


def test_vinted_parse_item_old_format_still_supported():
    raw = parse_item(VINTED["items"][0])
    assert raw.price == 1450 and raw.params == {"condition": "used", "buyer_fee": "75.00"}
    assert parse_item(VINTED["items"][1]).price == 700
    assert parse_item(VINTED["items"][2]).currency == "EUR"


def vinted_handler(calls, *, catalogue=None, legacy_status=404, api_status=200, token=True):
    def handler(request):
        calls.append(request)
        if request.url.host == "www.vinted.pl" and not request.url.path.startswith("/api"):
            headers = {"set-cookie": "access_token_web=tok123; Path=/; Domain=.vinted.pl"} if token else {}
            return httpx.Response(200, text="", headers=headers)
        if request.url.host == "api.vinted.pl":
            if api_status != 200:
                return httpx.Response(api_status, json={"code": api_status})
            return httpx.Response(200, json=catalogue if catalogue is not None else VINTED_NEW)
        return httpx.Response(legacy_status, text="<div>nie znaleziono</div>")
    return handler


def vinted_search(handler, **kw):
    async def go():
        async with client(handler) as http:
            adapter = VintedAdapter(http, Settings())
            offers = await adapter.search(SearchQuery(Mode.RESELL, phrases=["iphone"], **kw))
            return offers, adapter
    return run(go())


def test_vinted_adapter_uses_new_catalogue_api_with_bearer_token():
    calls = []
    offers, adapter = vinted_search(vinted_handler(calls), price_min=300)
    assert sorted(o.price for o in offers) == [700, 1450]  # oferta w USD pominięta
    assert adapter.stats == {"items_seen": 3, "foreign_currency": 1}
    assert calls[0].method == "HEAD" and calls[0].url.path == "/catalog"
    api = calls[1]
    assert str(api.url).startswith("https://api.vinted.pl/svc-catalogue/items")
    assert api.headers["authorization"] == "Bearer tok123"
    assert api.url.params["order"] == "newest_first" and api.url.params["price_from"] == "300"
    assert "price_to" not in api.url.params  # puste filtry pomijane (inaczej API odpowiada 400)


def test_vinted_falls_back_to_legacy_endpoint():
    calls = []
    legacy = {"items": VINTED["items"][:2], "pagination": {"total_pages": 1}}

    def handler(request):
        calls.append(request)
        if request.url.path == "/catalog":
            return httpx.Response(200, headers={"set-cookie": "access_token_web=t; Path=/"})
        if request.url.host == "api.vinted.pl":
            return httpx.Response(404)
        return httpx.Response(200, json=legacy)

    offers, _ = vinted_search(handler)
    assert len(offers) == 2


def test_vinted_both_endpoints_gone_means_format_changed():
    with pytest.raises(SourceFormatChanged, match="zmienił API"):
        vinted_search(vinted_handler([], api_status=404))


def test_vinted_missing_token_means_format_changed():
    with pytest.raises(SourceFormatChanged, match="access_token_web"):
        vinted_search(vinted_handler([], token=False))


def test_vinted_blocked():
    with pytest.raises(SourceBlocked):
        vinted_search(lambda r: httpx.Response(403, text="<html>datadome captcha</html>",
                                               headers={"content-type": "text/html"}))


def test_vinted_expired_token_is_refreshed():
    calls, state = [], {"api": 0}

    def handler(request):
        calls.append(request)
        if request.url.host == "www.vinted.pl":
            return httpx.Response(200, headers={"set-cookie": f"access_token_web=t{len(calls)}; Path=/"})
        state["api"] += 1
        return httpx.Response(401) if state["api"] == 1 else httpx.Response(200, json=VINTED_NEW)

    offers, _ = vinted_search(handler)
    assert len(offers) == 2 and state["api"] == 2


def test_vinted_unexpected_json_means_format_changed():
    with pytest.raises(SourceFormatChanged, match="listy przedmiotów"):
        vinted_search(vinted_handler([], catalogue={"results": "nowy format"}))


# ------------------------------------------------------------- Sprzedajemy.pl ---

from phonebot.sources.sprzedajemy import SprzedajemyAdapter, city_from_url, offer_id  # noqa: E402

SPRZEDAJEMY = (FIX / "sprzedajemy_page1.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("url, city", [
    ("https://sprzedajemy.pl/iphone-13-kielce-4-0010c9-nr69433240", "Kielce"),
    ("https://sprzedajemy.pl/iphone-13-128gb-zbity-ekran-nowy-targ-4-0010c9-nr69433241", "Nowy Targ"),
    ("https://sprzedajemy.pl/etui-brzesko-4-0010c9-nr71699292", "Brzesko"),
    ("https://sprzedajemy.pl/cos-innego", None),
])
def test_sprzedajemy_city_from_url(url, city):
    assert city_from_url(url) == city


def test_sprzedajemy_offer_id():
    assert offer_id("https://sprzedajemy.pl/iphone-13-kielce-4-0010c9-nr69433240", "x") == "69433240"
    assert offer_id(None, "fallback") == "fallback"


def test_sprzedajemy_adapter():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, text=SPRZEDAJEMY)

    async def go():
        async with client(handler) as http:
            return await SprzedajemyAdapter(http, Settings()).search(
                SearchQuery(Mode.RESELL, phrases=["iphone 13"], price_max=2500))

    offers = {o.source_id: o for o in run(go())}
    assert calls[0].url.params["inp_text"] == "iphone 13"
    assert set(offers) == {"69433241", "71699292", "69433240"}  # 3000 zł odfiltrowane przez price_max
    o = offers["69433241"]
    assert (o.source, o.price, o.city) == ("sprzedajemy", 700, "Nowy Targ")
    assert o.url.endswith("nr69433241") and o.photos == ["https://thumbs.img-sprzedajemy.pl/1.jpg"]


def test_sprzedajemy_layout_change_and_no_results():
    async def go(html):
        async with client(lambda r: httpx.Response(200, text=html)) as http:
            return await SprzedajemyAdapter(http, Settings()).search(SearchQuery(Mode.RESELL, phrases=["x"]))

    assert run(go("<html>Brak ogłoszeń spełniających kryteria</html>")) == []
    with pytest.raises(SourceFormatChanged):
        run(go("<html>zupełnie nowy wygląd</html>"))
