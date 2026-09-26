"""Baza z przykładowymi ofertami z trzech portali (symulowany serwer, bez sieci)."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from phonebot.core.settings import Settings
from phonebot.net.http import HostRateLimiter, HttpClient
from phonebot.services.scanner import Scanner
from phonebot.storage.db import open_database
from phonebot.storage.repositories import PartsRepository, SettingsRepository

FIX = Path(__file__).parent / "fixtures"


def mock_portals(request: httpx.Request) -> httpx.Response:
    host = request.url.host
    if host == "www.olx.pl":
        name = "olx_page2.json" if "offset=40" in str(request.url) else "olx_page1.json"
        return httpx.Response(200, json=json.loads((FIX / name).read_text(encoding="utf-8")))
    if host == "allegrolokalnie.pl":
        return httpx.Response(200, text=(FIX / "allegro_lokalnie_page1.html").read_text(encoding="utf-8"))
    if host == "www.vinted.pl":  # sesja: strona wydaje token w ciasteczku
        return httpx.Response(200, text="", headers={"set-cookie": "access_token_web=tok; Path=/; Domain=.vinted.pl"})
    if host == "api.vinted.pl":  # nowy katalog (od września 2026)
        return httpx.Response(200, json=json.loads((FIX / "vinted_svc_catalogue.json").read_text(encoding="utf-8")))
    return httpx.Response(404)


def build_sample_db(db_path: Path, settings: Settings | None = None):
    conn = open_database(db_path)
    PartsRepository(conn).seed_defaults_if_empty()
    settings = settings or Settings(max_pages_per_query=2)
    SettingsRepository(conn).save(settings)
    limiter = HostRateLimiter(0)
    transport = httpx.MockTransport(mock_portals)
    scanner = Scanner(conn, settings, limiter,
                      http_factory=lambda: HttpClient(limiter, transport=transport, wait=lambda s: 0))
    report = asyncio.run(scanner.run())
    return conn, report
