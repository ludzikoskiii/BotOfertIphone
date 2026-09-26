"""Klient HTTP: ponawianie, blokady, cache i limit zapytań (niezależnie od portalu)."""
import asyncio

import httpx
import pytest

from phonebot.net.http import HostRateLimiter, HttpClient, HttpError, ResponseCache


def client(handler, cache=None, attempts=3):
    return HttpClient(HostRateLimiter(0), cache, attempts=attempts, transport=httpx.MockTransport(handler),
                      wait=lambda s: 0)


def get(handler, url="https://example.pl/x", **kw):
    async def go():
        async with client(handler, **kw) as http:
            return await http.get_text(url)
    return asyncio.run(go())


def test_retries_on_server_error_then_succeeds():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(503) if attempts["n"] < 3 else httpx.Response(200, text="ok")

    assert get(handler) == "ok" and attempts["n"] == 3


def test_blocked_response_is_not_retried():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(403, text="<html>Request blocked</html>", headers={"content-type": "text/html"})

    with pytest.raises(HttpError) as e:
        get(handler)
    assert e.value.blocked and attempts["n"] == 1


def test_rate_limit_429_is_retried():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(429) if attempts["n"] == 1 else httpx.Response(200, text="ok")

    assert get(handler) == "ok" and attempts["n"] == 2


def test_network_error_retried_then_fails():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        raise httpx.ConnectError("brak sieci")

    with pytest.raises(HttpError) as e:
        get(handler)
    assert e.value.network and attempts["n"] == 3


def test_cache_prevents_repeated_requests():
    attempts = {"n": 0}

    def handler(request):
        attempts["n"] += 1
        return httpx.Response(200, text="ok")

    cache = ResponseCache(ttl_s=60)

    async def go():
        async with client(handler, cache) as http:
            await http.get_text("https://example.pl/x")
            await http.get_text("https://example.pl/x")

    asyncio.run(go())
    assert attempts["n"] == 1


def test_trace_records_requests():
    async def go():
        async with client(lambda r: httpx.Response(404, text="nie ma")) as http:
            http.trace = []
            with pytest.raises(HttpError):
                await http.get_text("https://example.pl/x")
            return http.trace

    trace = asyncio.run(go())
    assert trace[0]["status"] == 404 and trace[0]["url"] == "https://example.pl/x"


def test_rate_limiter_spaces_requests_per_host():
    now = [100.0]
    limiter = HostRateLimiter(4.0, jitter=0, clock=lambda: now[0])
    assert limiter.reserve("allegrolokalnie.pl") == 0
    assert limiter.reserve("allegrolokalnie.pl") == 4.0
    assert limiter.reserve("allegrolokalnie.pl") == 8.0
    assert limiter.reserve("vinted.pl") == 0  # inny host — bez czekania
    now[0] = 120.0
    assert limiter.reserve("allegrolokalnie.pl") == 0


def test_response_cache_ttl():
    now = [0.0]
    cache = ResponseCache(ttl_s=10, clock=lambda: now[0])
    cache.put("u", "body")
    assert cache.get("u") == "body"
    now[0] = 11
    assert cache.get("u") is None
