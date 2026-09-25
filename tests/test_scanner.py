import asyncio
import json
from pathlib import Path

import httpx

from phonebot.core.models import Mode
from phonebot.core.settings import Settings
from phonebot.net.http import HostRateLimiter, HttpClient
from phonebot.services.scanner import Scanner
from phonebot.sources.base import SourceAdapter, SourceError
from phonebot.sources.olx import OlxAdapter
from phonebot.storage.repositories import FetchRunRepository, OfferRepository

FIXTURES = Path(__file__).parent / "fixtures"
PAGES = {
    False: json.loads((FIXTURES / "olx_page1.json").read_text(encoding="utf-8")),
    True: json.loads((FIXTURES / "olx_page2.json").read_text(encoding="utf-8")),
}


def olx_handler(request):
    return httpx.Response(200, json=PAGES["offset=40" in str(request.url)])


class BrokenAdapter(SourceAdapter):
    key = "broken"
    display_name = "Zepsuty"

    async def search(self, query):
        raise SourceError("HTTP 403 z broken.pl")


class SlowAdapter(SourceAdapter):
    key = "slow"
    display_name = "Wolny"

    async def search(self, query):
        await asyncio.sleep(10)
        return []


def make_scanner(conn, adapters, settings=None):
    settings = settings or Settings(mode=Mode.RESELL.value, max_pages_per_query=2)
    limiter = HostRateLimiter(0)
    return Scanner(
        conn, settings, limiter,
        http_factory=lambda: HttpClient(limiter, transport=httpx.MockTransport(olx_handler), wait=lambda s: 0),
        adapter_factory=lambda http, s: [cls(http, s) if cls is OlxAdapter else cls() for cls in adapters],
    )


def test_scan_stores_filtered_offers(conn):
    report = asyncio.run(make_scanner(conn, [OlxAdapter]).run())
    olx = report.sources[0]
    assert olx.ok and olx.found == 14
    assert olx.skipped == 2  # etui + „kupię"
    assert olx.saved == olx.new == 12
    assert report.new_count == 12
    offers = OfferRepository(conn).list()
    assert {o.parsed.model for o in offers} == {"iPhone 13", "iPhone 12 Pro", "iPhone 14 Pro", "iPhone 11"}

    again = asyncio.run(make_scanner(conn, [OlxAdapter]).run())
    assert again.new_count == 0 and again.sources[0].saved == 12


def test_failing_source_does_not_block_others(conn):
    messages = []
    report = asyncio.run(make_scanner(conn, [BrokenAdapter, OlxAdapter]).run(messages.append))
    broken, olx = report.sources
    assert "403" in broken.error and olx.ok and olx.saved == 12
    assert any("Zepsuty: błąd" in m for m in messages)
    runs = {r["source"]: r["status"] for r in FetchRunRepository(conn).last_runs()}
    assert runs == {"broken": "error", "olx": "ok"}


def test_timeout_is_isolated(conn):
    s = Settings(mode=Mode.RESELL.value, max_pages_per_query=2, source_timeout_s=0.05)
    report = asyncio.run(make_scanner(conn, [SlowAdapter, OlxAdapter], s).run())
    assert "limit czasu" in report.sources[0].error
    assert report.sources[1].ok


def test_price_drop_detected(conn):
    asyncio.run(make_scanner(conn, [OlxAdapter]).run())
    PAGES[False]["data"][0]["params"][0]["value"]["value"] = 799
    try:
        report = asyncio.run(make_scanner(conn, [OlxAdapter]).run())
    finally:
        PAGES[False]["data"][0]["params"][0]["value"]["value"] = 850
    assert len(report.price_drop_ids) == 1


def test_disabled_sources(conn):
    s = Settings(enabled_sources={"olx": False})
    from phonebot.services.scanner import default_adapters
    assert default_adapters(None, s) == []


