import asyncio
from pathlib import Path

import httpx

from phonebot.core.models import Mode
from phonebot.core.settings import Settings
from phonebot.net.http import HostRateLimiter, HttpClient
from phonebot.services.scanner import Scanner
from phonebot.sources.allegro_lokalnie import AllegroLokalnieAdapter
from phonebot.sources.base import SourceAdapter, SourceError
from phonebot.storage.repositories import FetchRunRepository, OfferRepository

FIX = Path(__file__).parent / "fixtures"
PAGES = {
    "1": (FIX / "allegro_lokalnie_search_p1.html").read_text(encoding="utf-8"),
    "2": (FIX / "allegro_lokalnie_search_p2.html").read_text(encoding="utf-8"),
}


def portal_handler(request):
    return httpx.Response(200, text=PAGES[request.url.params.get("page", "1")])


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


def make_scanner(conn, adapters, settings=None, handler=portal_handler):
    settings = settings or Settings(mode=Mode.RESELL.value, max_pages_per_query=2)
    limiter = HostRateLimiter(0)
    return Scanner(
        conn, settings, limiter,
        http_factory=lambda: HttpClient(limiter, transport=httpx.MockTransport(handler), wait=lambda s: 0),
        adapter_factory=lambda http, s: [cls(http, s) if cls is AllegroLokalnieAdapter else cls() for cls in adapters],
    )


def test_scan_stores_filtered_offers(conn):
    report = asyncio.run(make_scanner(conn, [AllegroLokalnieAdapter]).run())
    src = report.sources[0]
    assert src.ok and src.found == 14
    assert src.skipped == 2  # etui + „kupię"
    assert src.saved == src.new == 12
    assert report.new_count == 12
    offers = OfferRepository(conn).list()
    assert {o.parsed.model for o in offers} == {"iPhone 13", "iPhone 12 Pro", "iPhone 14 Pro", "iPhone 11"}

    again = asyncio.run(make_scanner(conn, [AllegroLokalnieAdapter]).run())
    assert again.new_count == 0 and again.sources[0].saved == 12


def test_failing_source_does_not_block_others(conn):
    messages = []
    report = asyncio.run(make_scanner(conn, [BrokenAdapter, AllegroLokalnieAdapter]).run(messages.append))
    broken, allegro = report.sources
    assert "403" in broken.error and allegro.ok and allegro.saved == 12
    assert any("Zepsuty: błąd" in m for m in messages)
    runs = {r["source"]: r["status"] for r in FetchRunRepository(conn).last_runs()}
    assert runs == {"broken": "error", "allegro_lokalnie": "ok"}


def test_timeout_is_isolated(conn):
    s = Settings(mode=Mode.RESELL.value, max_pages_per_query=2, source_timeout_s=0.05)
    report = asyncio.run(make_scanner(conn, [SlowAdapter, AllegroLokalnieAdapter], s).run())
    assert "limit czasu" in report.sources[0].error and report.sources[0].kind == "timeout"
    assert report.sources[1].ok


def test_price_drop_detected(conn):
    asyncio.run(make_scanner(conn, [AllegroLokalnieAdapter]).run())
    cheaper = {k: v.replace('"amount": "850.00"', '"amount": "799.00"') for k, v in PAGES.items()}
    report = asyncio.run(make_scanner(
        conn, [AllegroLokalnieAdapter],
        handler=lambda r: httpx.Response(200, text=cheaper[r.url.params.get("page", "1")])).run())
    assert len(report.price_drop_ids) == 1


def test_disabled_sources():
    from phonebot.services.scanner import default_adapters
    from phonebot.sources import REGISTRY

    s = Settings(enabled_sources={k: False for k in REGISTRY})
    assert default_adapters(None, s) == []


def test_olx_is_not_a_source_anymore():
    from phonebot.sources import REGISTRY

    assert "olx" not in REGISTRY
