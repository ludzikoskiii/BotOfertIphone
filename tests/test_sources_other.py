import asyncio
import json
from pathlib import Path

import httpx
import pytest

from phonebot.core.models import Mode
from phonebot.core.settings import Settings
from phonebot.net.http import HostRateLimiter, HttpClient
from phonebot.sources.allegro_lokalnie import AllegroLokalnieAdapter
from phonebot.sources.base import SearchQuery, SourceError
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


def test_vinted_parse_item():
    raw = parse_item(VINTED["items"][0])
    assert raw.source == "vinted" and raw.price == 1450 and raw.shipping_available is True
    assert raw.params == {"condition": "used", "buyer_fee": "75.00"}
    assert raw.photos == ["https://images1.vinted.net/t/01.jpeg"]
    old_format = parse_item(VINTED["items"][1])
    assert old_format.price == 700
    assert parse_item(VINTED["items"][2]) is None  # EUR


def test_vinted_adapter_gets_session_cookie_first():
    calls = []

    def handler(request):
        calls.append(request)
        if request.url.path == "/":
            return httpx.Response(200, text="<html></html>", headers={"set-cookie": "access_token_web=abc; Path=/"})
        return httpx.Response(200, json=VINTED)

    async def go():
        async with client(handler) as http:
            return await VintedAdapter(http, Settings()).search(SearchQuery(Mode.RESELL, phrases=["iphone"]))

    offers = run(go())
    assert len(offers) == 2
    assert calls[0].url.path == "/" and calls[1].url.path == "/api/v2/catalog/items"
    assert "access_token_web=abc" in calls[1].headers.get("cookie", "")
    assert calls[1].url.params["order"] == "newest_first"


def test_vinted_blocked():
    async def go():
        async with client(lambda r: httpx.Response(403)) as http:
            return await VintedAdapter(http, Settings()).search(SearchQuery(Mode.RESELL))

    with pytest.raises(SourceError, match="sesji Vinted"):
        run(go())
