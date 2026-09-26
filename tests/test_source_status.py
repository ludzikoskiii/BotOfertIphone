"""Kategorie błędów źródeł, pauza po blokadzie i status źródeł w GUI."""
import asyncio
import os
from datetime import timedelta

import httpx
import pytest

from phonebot.core.models import Mode
from phonebot.core.settings import Settings
from phonebot.net.http import HostRateLimiter, HttpClient
from phonebot.services.scanner import Scanner
from phonebot.sources.allegro_lokalnie import AllegroLokalnieAdapter
from phonebot.sources.base import SearchQuery, SourceBlocked, SourceFormatChanged, SourceNetworkError
from phonebot.storage.repositories import FetchRunRepository, utcnow

CLOUDFRONT_403 = ('<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN"><HTML><HEAD>'
                  "<TITLE>ERROR: The request could not be satisfied</TITLE></HEAD><BODY><H1>403 ERROR</H1>"
                  "Request blocked.</BODY></HTML>")


def client(handler):
    return HttpClient(HostRateLimiter(0), transport=httpx.MockTransport(handler), wait=lambda s: 0)


def search(adapter_cls, handler, phrases=("iphone", "iphone zbity")):  # noqa: D103
    calls = []

    def wrapped(request):
        calls.append(request)
        return handler(request)

    async def go():
        async with client(wrapped) as http:
            return await adapter_cls(http, Settings()).search(SearchQuery(Mode.REPAIR, phrases=list(phrases)))

    return asyncio.run(go()), calls


def test_403_is_reported_as_block_and_stops_immediately():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(403, text=CLOUDFRONT_403, headers={"content-type": "text/html", "server": "CloudFront"})

    with pytest.raises(SourceBlocked, match="blokuje automatyczne pobieranie"):
        search(AllegroLokalnieAdapter, handler)
    assert len(calls) == 1  # bez ponawiania i bez kolejnych fraz


def test_gone_endpoint_is_reported_as_format_change():
    with pytest.raises(SourceFormatChanged, match="zmienił API"):
        search(AllegroLokalnieAdapter, lambda r: httpx.Response(404, text="nie ma"))


def test_network_error_category():
    def boom(request):
        raise httpx.ConnectError("brak sieci")

    with pytest.raises(SourceNetworkError):
        search(AllegroLokalnieAdapter, boom)


def test_allegro_no_results_is_not_a_format_change():
    offers, _ = search(AllegroLokalnieAdapter,
                       lambda r: httpx.Response(200, text="<html><body>Brak wyników dla „xyz”</body></html>"),
                       phrases=["xyz"])
    assert offers == []
    with pytest.raises(SourceFormatChanged):
        search(AllegroLokalnieAdapter, lambda r: httpx.Response(200, text="<html>nowy wygląd</html>"))


def scanner(conn, handler, settings=None):
    limiter = HostRateLimiter(0)
    return Scanner(conn, settings or Settings(max_pages_per_query=1), limiter,
                   http_factory=lambda: HttpClient(limiter, transport=httpx.MockTransport(handler), wait=lambda s: 0),
                   adapter_factory=lambda http, s: [AllegroLokalnieAdapter(http, s)])


def test_scanner_records_kind_and_pauses_blocked_source(conn):
    calls = []

    def blocked(request):
        calls.append(request)
        return httpx.Response(403, text=CLOUDFRONT_403)

    report = asyncio.run(scanner(conn, blocked).run())
    assert report.sources[0].kind == "blocked"
    assert FetchRunRepository(conn).latest_by_source()["allegro_lokalnie"]["status"] == "blocked"
    # automatyczne odświeżenie w czasie pauzy nie odpytuje portalu
    again = asyncio.run(scanner(conn, blocked).run())
    assert len(calls) == 1
    assert again.sources[0].kind == "blocked" and "pauza do" in again.sources[0].error
    # ręczne „Odśwież” (force) pomija pauzę
    asyncio.run(scanner(conn, blocked).run(force=True))
    assert len(calls) == 2


def test_pause_expires(conn):
    runs = FetchRunRepository(conn)
    run_id = runs.start("allegro_lokalnie")
    runs.finish(run_id, found=0, new=0, error="403", status="blocked")
    old = (utcnow() - timedelta(hours=10)).isoformat()
    conn.execute("UPDATE fetch_runs SET finished_at = ? WHERE id = ?", (old, run_id))
    s = scanner(conn, lambda r: httpx.Response(403))
    assert s._cooldowns() == {}


def test_empty_source_is_flagged(conn):
    report = asyncio.run(scanner(conn, lambda r: httpx.Response(200, text="<html>Brak wyników</html>")).run())
    assert report.sources[0].kind == "empty"


# ------------------------------------------------------------------ GUI ---

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def app():
    QtWidgets = pytest.importorskip("PySide6.QtWidgets")
    return QtWidgets.QApplication.instance() or QtWidgets.QApplication([])


def test_status_bar_shows_each_source(app, tmp_path):
    from .sample_data import build_sample_db

    conn, _ = build_sample_db(tmp_path / "db.sqlite3")
    runs = FetchRunRepository(conn)
    rid = runs.start("allegro_lokalnie")
    runs.finish(rid, found=0, new=0, error="HTTP 403 — portal blokuje", status="blocked")
    from phonebot.ui.main_window import MainWindow

    win = MainWindow(conn, tmp_path / "db.sqlite3", thumbs_dir=tmp_path)
    bar = win.source_status
    assert bar.kinds["allegro_lokalnie"] == "blocked" and "zablokowane" in bar.text_of("allegro_lokalnie")
    assert bar.kinds["vinted"] == "ok" and "działa" in bar.text_of("vinted")
    assert bar.kinds["sprzedajemy"] == "ok"
    assert "olx" not in bar.kinds
    tooltip = win.findChild(type(win._status), "status_allegro_lokalnie").toolTip()
    assert "HTTP 403" in tooltip and "Zwiększ odstęp" in tooltip

    import copy

    s = copy.deepcopy(win.settings)
    s.enabled_sources["vinted"] = False
    win.apply_settings(s)
    assert bar.kinds["vinted"] == "disabled"
    win.close()
    conn.close()


def test_scan_result_updates_status(app, tmp_path):
    from phonebot.services.scanner import ScanReport, SourceReport

    from .sample_data import build_sample_db

    conn, _ = build_sample_db(tmp_path / "db.sqlite3")
    from phonebot.ui.main_window import MainWindow

    win = MainWindow(conn, tmp_path / "db.sqlite3", thumbs_dir=tmp_path)
    win._scan_finished(ScanReport(sources=[SourceReport("vinted", "Vinted", kind="changed", error="HTTP 404")]))
    assert win.source_status.kinds["vinted"] == "changed"
    assert "Vinted: ZMIANA FORMATU" in win._status.text()
    win.close()
    conn.close()
