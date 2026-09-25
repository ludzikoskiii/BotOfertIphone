import asyncio
import json
from pathlib import Path

import httpx
import pytest

from phonebot.core.models import Mode
from phonebot.core.settings import Settings
from phonebot.net.http import HostRateLimiter, HttpClient, ResponseCache
from phonebot.sources.base import SearchQuery, SourceError
from phonebot.sources.olx import OlxAdapter, html_to_text, parse_offer, parse_page

FIXTURES = Path(__file__).parent / "fixtures"
PAGE1 = json.loads((FIXTURES / "olx_page1.json").read_text(encoding="utf-8"))
PAGE2 = json.loads((FIXTURES / "olx_page2.json").read_text(encoding="utf-8"))


def no_wait(_state):
    return 0


def client(handler, cache=None):
    return HttpClient(HostRateLimiter(0), cache, transport=httpx.MockTransport(handler), wait=no_wait)


def test_parse_offer_fields():
    raw = parse_offer(PAGE1["data"][0])
    assert raw.source == "olx" and raw.source_id == "900000001"
    assert raw.title == "iPhone 13 128GB zbity ekran"
    assert raw.price == 850 and raw.negotiable is True
    assert raw.city == "Nowy Targ" and raw.region == "Małopolskie"
    assert (raw.lat, raw.lon) == (49.477, 20.032)
    assert raw.params == {"condition": "damaged", "model": "iPhone 13", "storage": "128 GB"}
    assert raw.shipping_available is True
    assert raw.photos[0].endswith("image;s=400x300")
    assert "Zbity ekran" in raw.description and "<" not in raw.description
    assert raw.created_at.year == 2026


def test_parse_offer_without_price_is_skipped():
    item = dict(PAGE1["data"][0])
    item["params"] = [p for p in item["params"] if p["key"] != "price"]
    assert parse_offer(item) is None


def test_parse_offer_foreign_currency_is_skipped():
    item = json.loads(json.dumps(PAGE1["data"][0]))
    item["params"][0]["value"]["currency"] = "EUR"
    assert parse_offer(item) is None


def test_parse_page_and_next_link():
    offers, nxt = parse_page(PAGE1)
    assert len(offers) == 10
    assert nxt.endswith("offset=40&limit=40&query=iphone")
    offers2, nxt2 = parse_page(PAGE2)
    assert len(offers2) == 4 and nxt2 is None
    assert offers2[-1].shipping_available is False


def test_parse_page_tolerates_broken_items():
    payload = {"data": [{"title": "bez id"}, PAGE1["data"][0]]}
    offers, _ = parse_page(payload)
    assert [o.source_id for o in offers] == ["900000001"]


def test_html_to_text():
    assert html_to_text("<p>Linia 1<br />Linia 2</p>") == "Linia 1\nLinia 2"
    assert html_to_text(None) == ""
    assert html_to_text("zwykły tekst") == "zwykły tekst"


def run(coro):
    return asyncio.run(coro)


def test_adapter_paginates_and_dedups():
    calls = []

    def handler(request: httpx.Request):
        calls.append(str(request.url))
        page = PAGE2 if "offset=40" in str(request.url) else PAGE1
        return httpx.Response(200, json=page)

    async def go():
        async with client(handler) as http:
            adapter = OlxAdapter(http, Settings())
            return await adapter.search(SearchQuery(Mode.REPAIR, phrases=["iphone", "iphone 13"], max_pages=3))

    offers = run(go())
    assert len(offers) == 14  # te same oferty z dwóch fraz liczone raz
    assert len(calls) == 4  # 2 frazy × 2 strony
    assert "sort_by=created_at%3Adesc" in calls[0]


def test_adapter_respects_max_pages_and_price_filter():
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(200, json=PAGE1)

    async def go():
        async with client(handler) as http:
            return await OlxAdapter(http, Settings()).search(
                SearchQuery(Mode.RESELL, phrases=["iphone"], max_pages=1, price_min=300, price_max=2500))

    run(go())
    assert len(calls) == 1
    assert calls[0].params["filter_float_price:from"] == "300"
    assert calls[0].params["filter_float_price:to"] == "2500"


def test_retries_on_server_error_then_succeeds():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        if attempts["n"] < 3:
            return httpx.Response(503)
        return httpx.Response(200, json=PAGE2)

    async def go():
        async with client(handler) as http:
            return await OlxAdapter(http, Settings()).search(SearchQuery(Mode.RESELL, max_pages=1))

    assert len(run(go())) == 4
    assert attempts["n"] == 3


def test_blocked_source_raises_source_error_without_retries():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(403)

    async def go():
        async with client(handler) as http:
            return await OlxAdapter(http, Settings()).search(
                SearchQuery(Mode.REPAIR, phrases=["iphone", "iphone zbity"]))

    with pytest.raises(SourceError, match="403"):
        run(go())
    assert attempts["n"] == 1  # bez ponawiania i bez kolejnych fraz


def test_network_error_retried_then_fails():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        raise httpx.ConnectError("brak sieci")

    async def go():
        async with client(handler) as http:
            return await OlxAdapter(http, Settings()).search(SearchQuery(Mode.RESELL, max_pages=1))

    with pytest.raises(SourceError, match="Błąd sieci"):
        run(go())
    assert attempts["n"] == 3


def test_cache_prevents_repeated_requests():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(200, json=PAGE2)

    cache = ResponseCache(ttl_s=60)

    async def go():
        async with client(handler, cache) as http:
            a = OlxAdapter(http, Settings())
            await a.search(SearchQuery(Mode.RESELL, max_pages=1))
            await a.search(SearchQuery(Mode.RESELL, max_pages=1))

    run(go())
    assert attempts["n"] == 1


def test_rate_limiter_spaces_requests_per_host():
    now = [100.0]
    limiter = HostRateLimiter(4.0, jitter=0, clock=lambda: now[0])
    assert limiter.reserve("olx.pl") == 0
    assert limiter.reserve("olx.pl") == 4.0
    assert limiter.reserve("olx.pl") == 8.0
    assert limiter.reserve("vinted.pl") == 0  # inny host — bez czekania
    now[0] = 120.0
    assert limiter.reserve("olx.pl") == 0


def test_response_cache_ttl():
    now = [0.0]
    cache = ResponseCache(ttl_s=10, clock=lambda: now[0])
    cache.put("u", "body")
    assert cache.get("u") == "body"
    now[0] = 11
    assert cache.get("u") is None


def test_unreachable_host_stops_after_first_phrase():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        raise httpx.ConnectError("brak sieci")

    async def go():
        async with client(handler) as http:
            return await OlxAdapter(http, Settings()).search(
                SearchQuery(Mode.REPAIR, phrases=["iphone", "iphone zbity", "iphone uszkodzony"]))

    with pytest.raises(SourceError):
        run(go())
    assert attempts["n"] == 3  # 3 próby pierwszej frazy, kolejne pominięte
