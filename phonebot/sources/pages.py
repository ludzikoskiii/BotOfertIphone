"""Opis oferty ze strony ogłoszenia — gdy wyniki wyszukiwania go nie zawierają.

Potrzebne lokalnemu modelowi językowemu (etap 3), który czyta opisy ofert „DO WERYFIKACJI”. Pobierane
są tylko strony takich ofert, każda raz, z tym samym limitem zapytań na portal co wyszukiwanie.

Sprawdzone sondą (``scripts/probe_descriptions.py``, wrzesień 2026):

* Sprzedajemy.pl — JSON-LD strony oferty (pole ``description``), strona ok. 125 kB;
* Allegro Lokalnie — HTML: akapity ``<p class="desc-p">``, strona ok. 1,1 MB;
* Vinted — JSON-LD i ``og:description`` na samym początku strony (ok. 10–14 kB z 2 MB), więc program czyta
  tylko początek. API przedmiotu (``/api/v2/items/{id}/details``) jest chronione przed automatami (403)
  — nie jest używane.

Gdy portal odpowie blokadą (403/429 albo strona z zabezpieczeniem), pobieranie opisów z tego portalu
jest wstrzymywane do końca działania programu — bez prób obchodzenia zabezpieczeń.
"""
from __future__ import annotations

import html as html_lib
import json
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from selectolax.parser import HTMLParser

from ..net.http import DEFAULT_HEADERS, HostRateLimiter, looks_blocked
from .extract import walk

log = logging.getLogger(__name__)

SUPPORTED = ("allegro_lokalnie", "sprzedajemy", "vinted")
# ile bajtów strony czytać: opis Vinted jest na początku ogromnej strony (2 MB)
MAX_BYTES = {"vinted": 160_000}
DEFAULT_MAX_BYTES = 3_000_000
MAX_DESCRIPTION_CHARS = 4000
_LD_JSON = re.compile(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', re.S | re.I)
_PRICE_PREFIX = re.compile(r"^\s*[\d\s.,]+\s*zł\s*:\s*", re.I)  # Sprzedajemy: „400 zł: Sprzedam…”


class PageBlocked(Exception):
    """Portal blokuje automatyczne pobieranie strony oferty."""


@dataclass
class PageResult:
    description: str = ""
    error: str | None = None
    blocked: bool = False


def _clean(text: str) -> str:
    text = html_lib.unescape(text or "").replace("\r", "")
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n", text)
    return text.strip()[:MAX_DESCRIPTION_CHARS]


def _ld_descriptions(page: str) -> list[str]:
    """Opisy z bloków JSON-LD (także z niepełnej strony — bierze tylko bloki zamknięte w całości)."""
    out: list[str] = []
    for block in _LD_JSON.findall(page):
        try:
            data = json.loads(block)
        except json.JSONDecodeError:
            continue
        for d in walk(data):
            v = d.get("description")
            if isinstance(v, str) and len(v.strip()) >= 3:
                out.append(v)
    return out


def _meta(tree: HTMLParser, selector: str) -> str:
    node = tree.css_first(selector)
    return (node.attributes.get("content") or "") if node else ""


def extract_description(source: str, page: str, title: str = "") -> str:
    """Opis sprzedającego ze strony oferty danego portalu (pusty tekst, gdy go nie ma)."""
    tree = HTMLParser(page)
    if source == "allegro_lokalnie":
        parts = [p.text(separator="\n", strip=True) for p in tree.css("p.desc-p")]
        return _clean("\n".join(p for p in parts if p))
    candidates = _ld_descriptions(page)
    if candidates:
        return _clean(max(candidates, key=len))
    og = _meta(tree, 'meta[property="og:description"]') or _meta(tree, 'meta[name="description"]')
    if not og:
        return ""
    if source == "sprzedajemy":
        og = _PRICE_PREFIX.sub("", og)
    elif source == "vinted" and title and og.startswith(title + " - "):
        og = og[len(title) + 3:]  # Vinted: „tytuł - opis”
    return _clean(og)


class PageFetcher:
    """Pobiera strony ofert z limitem zapytań na host (wspólnym z wyszukiwaniem) i pamięcią blokad."""

    def __init__(self, limiter: HostRateLimiter | None = None, *, client: httpx.Client | None = None,
                 stop: Callable[[], bool] | None = None, timeout_s: float = 20.0, sleep=time.sleep):
        self.limiter = limiter or HostRateLimiter(4.0)
        self._own = client is None
        self.client = client or httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=timeout_s)
        self.stop = stop or (lambda: False)
        self._sleep = sleep
        self.blocked_sources: set[str] = set()

    def close(self) -> None:
        if self._own:
            self.client.close()

    def _wait_turn(self, host: str) -> bool:
        """Czeka na swoją kolej u hosta (w krótkich odcinkach, żeby zamknięcie programu nie czekało)."""
        delay = self.limiter.reserve(host)
        while delay > 0:
            if self.stop():
                return False
            step = min(delay, 0.25)
            self._sleep(step)
            delay -= step
        return not self.stop()

    def fetch(self, source: str, url: str, title: str = "") -> PageResult:
        if source not in SUPPORTED:
            return PageResult(error="portal nie jest obsługiwany")
        if source in self.blocked_sources:
            return PageResult(error="portal zablokował pobieranie opisów", blocked=True)
        host = urlsplit(url).hostname or ""
        if not host or not self._wait_turn(host):
            return PageResult(error="przerwano")
        limit = MAX_BYTES.get(source, DEFAULT_MAX_BYTES)
        try:
            with self.client.stream("GET", url) as r:
                chunks, size = [], 0
                for chunk in r.iter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= limit:
                        break  # reszty strony nie potrzebujemy (Vinted: opis jest na początku)
                body = b"".join(chunks)
                status, headers = r.status_code, r.headers
                encoding = r.encoding or "utf-8"
        except httpx.HTTPError as e:
            return PageResult(error=f"błąd połączenia: {e.__class__.__name__}")
        page = body.decode(encoding, errors="replace")
        blocked = status in (403, 429) or looks_blocked(httpx.Response(status, headers=headers, text=page[:20000]))
        if blocked:
            self.blocked_sources.add(source)
            log.warning("%s blokuje pobieranie stron ofert (HTTP %s) — wstrzymano do końca działania programu",
                        source, status)
            return PageResult(error=f"portal zablokował pobieranie (HTTP {status})", blocked=True)
        if status == 404 or status == 410:
            return PageResult(error="ogłoszenie już nie istnieje")
        if status != 200:
            return PageResult(error=f"HTTP {status}")
        description = extract_description(source, page, title)
        return PageResult(description=description, error=None if description else "brak opisu na stronie oferty")


def page_supported(source: str) -> bool:
    return source in SUPPORTED
