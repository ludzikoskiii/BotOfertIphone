"""Diagnostyka źródeł: każdy adapter osobno + surowe próby adresów portali.

Uruchom:  python -m phonebot.diagnose [--out plik.txt] [--json plik.json]
albo:     PhoneBot.exe --diagnose   (raport w %LOCALAPPDATA%\\PhoneBot\\diagnostyka.txt)

Dla każdego portalu raport pokazuje, na którym etapie jest problem:
pobieranie (sieć / status HTTP), blokada (403, captcha, DataDome),
parsowanie (ile ofert odczytano) i filtrowanie (ile przeszło do bazy).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
import traceback
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from . import __version__
from .core.listing_filter import ListingFilter
from .core.models import Mode
from .core.normalizer import parse_offer
from .core.settings import Settings
from .net.http import HostRateLimiter, HttpClient, HttpError
from .sources import REGISTRY, SearchQuery

PHRASE = "iphone 13"


@dataclass
class AdapterResult:
    key: str
    ok: bool = False
    stage: str = ""  # na którym etapie problem
    error: str | None = None
    error_type: str | None = None
    raw_offers: int = 0
    accepted: int = 0
    foreign_currency: int = 0  # oferty odrzucone, bo cena nie w PLN (np. test z serwera poza Polską)
    samples: list[str] = field(default_factory=list)
    seconds: float = 0.0
    trace: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class Probe:
    name: str
    url: str
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    status: int | None = None
    content_type: str = ""
    bytes: int = 0
    blocked: bool = False
    set_cookies: list[str] = field(default_factory=list)
    json_summary: str = ""
    markers: list[str] = field(default_factory=list)
    snippet: str = ""
    error: str | None = None
    extracted: str = ""  # oferty odczytane uniwersalnym ekstraktorem (JSON / JSON-LD)
    html_sample: str = ""  # fragment HTML wokół pierwszej ceny (do pisania parsera)


def _html_sample(text: str) -> str:
    i = text.find(" zł")
    if i < 0:
        return ""
    return text[max(0, i - 1200): i + 300].replace("\n", " ")


def _extracted(text: str, url: str) -> str:
    from .sources.extract import embedded_json, offers_from_html

    offers = offers_from_html(text, url)
    types = []
    for block in embedded_json(text):
        items = block if isinstance(block, list) else [block]
        types += [str(b.get("@type")) for b in items if isinstance(b, dict) and "@type" in b]
    head = "; ".join(f"{o.price:.0f} {o.currency} | {o.title[:50]} | {o.url}" for o in offers[:3])
    return f"{len(offers)} ofert, typy JSON-LD: {types[:10]}; przykłady: {head}"


def _json_summary(text: str) -> str:
    try:
        data = json.loads(text)
    except ValueError:
        return ""
    if isinstance(data, dict):
        parts = [f"klucze: {sorted(data)[:25]}"]
        for key, value in data.items():
            if isinstance(value, list) and value and isinstance(value[0], dict):
                first = {k: v for k, v in value[0].items() if k not in ("photo", "photos", "thumbnails")}
                parts.append(f"{key}[{len(value)}] pierwszy element: "
                             + json.dumps(first, ensure_ascii=False)[:3000])
            elif isinstance(value, dict) and key in ("pagination", "links", "metadata"):
                parts.append(f"{key}: " + json.dumps(value, ensure_ascii=False)[:500])
        return " | ".join(parts)
    return f"typ: {type(data).__name__}"


async def run_adapter(key: str, settings: Settings, timeout: float = 90.0) -> AdapterResult:
    res = AdapterResult(key)
    start = time.monotonic()
    limiter = HostRateLimiter(2.0)
    async with HttpClient(limiter, attempts=2, wait=lambda s: 3) as http:
        http.trace = res.trace
        adapter = REGISTRY[key](http, settings)  # type: ignore[call-arg]
        query = SearchQuery(Mode.RESELL, phrases=[PHRASE], max_pages=1)
        try:
            offers = await asyncio.wait_for(adapter.search(query), timeout)
        except Exception as e:
            res.error = str(e) or e.__class__.__name__
            res.error_type = e.__class__.__name__
            res.stage = _stage_from_error(res.trace, e)
            res.seconds = round(time.monotonic() - start, 1)
            return res
    res.raw_offers = len(offers)
    res.foreign_currency = int(getattr(adapter, "stats", {}).get("foreign_currency", 0))
    listing_filter = ListingFilter(settings.listing_filter)
    reasons: Counter[str] = Counter()
    for o in offers:
        decision = listing_filter.check(o.title, model=parse_offer(o).model, category=o.params.get("category"))
        if not decision.accepted:
            reasons[f"{decision.stage}: {decision.reason[:70]}"] += 1
            continue
        res.accepted += 1
        if len(res.samples) < 5:
            res.samples.append(f"{o.price:.0f} zł | {o.title[:60]} | {o.city or '-'}")
    res.ok = res.accepted > 0
    if res.ok:
        res.stage = "OK"
    elif res.foreign_currency:
        # portal działa i zwraca oferty, tylko w walucie kraju serwera (np. USD z GitHuba w USA)
        res.ok = True
        res.stage = f"OK — {res.foreign_currency} ofert w obcej walucie (połączenie spoza Polski)"
    else:
        top = "; ".join(f"{n}× {r}" for r, n in reasons.most_common(3))
        res.stage = ("parsowanie: 0 ofert w odpowiedzi" if not offers
                     else f"filtrowanie: żadna oferta nie przeszła ({top})")
    res.seconds = round(time.monotonic() - start, 1)
    return res


_KIND_STAGE = {
    "blocked": "blokada portalu",
    "changed": "zmiana formatu / API portalu",
    "network": "pobieranie: brak połączenia",
}


def _stage_from_error(trace: list[dict[str, Any]], exc: Exception) -> str:
    kind = getattr(exc, "kind", None)
    base = _stage_from_trace(trace, exc)
    return f"{_KIND_STAGE[kind]} — {base}" if kind in _KIND_STAGE else base


def _stage_from_trace(trace: list[dict[str, Any]], exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "pobieranie: przekroczony czas"
    last = trace[-1] if trace else {}
    if last.get("error"):
        return f"pobieranie: błąd sieci ({last['error']})"
    if last.get("blocked"):
        return f"blokada portalu (HTTP {last.get('status')})"
    status = last.get("status")
    if status and status >= 400:
        return f"pobieranie: HTTP {status}"
    if isinstance(exc, HttpError) and "JSON" in str(exc):
        return "parsowanie: odpowiedź nie jest JSON-em"
    return f"parsowanie / logika adaptera ({exc.__class__.__name__})"


def default_probes() -> list[Probe]:
    return [
        Probe("Vinted strona (sesja)", "https://www.vinted.pl/catalog?search_text=iphone", method="HEAD"),
        Probe("Vinted stary API", "https://www.vinted.pl/api/v2/catalog/items?search_text=iphone&per_page=5",
              headers={"Accept": "application/json"}),
        Probe("Vinted nowy API", "https://api.vinted.pl/svc-catalogue/items?search_text=iphone%2013&per_page=3"
                                 "&order=newest_first&page=2", headers={"Accept": "application/json"}),
        Probe("Vinted nowy API z cenami", "https://api.vinted.pl/svc-catalogue/items?search_text=iphone%2013"
                                          "&per_page=3&order=newest_first&price_from=500&price_to=3000",
              headers={"Accept": "application/json", "Accept-Language": "pl-PL,pl;q=0.9"}),
        Probe("Allegro Lokalnie (kategoria 4)",
              "https://allegrolokalnie.pl/oferty/elektronika/telefony-i-akcesoria-4/q/iphone%2013"),
        Probe("Sprzedajemy.pl (kategoria 1390)", "https://sprzedajemy.pl/elektronika/telefony-i-akcesoria/"
                                                 "telefony-komorkowe/apple-iphone?inp_text=iphone+13"),
    ]


async def run_probes(probes: list[Probe]) -> list[Probe]:
    async with HttpClient(HostRateLimiter(2.0), attempts=1) as http:
        for p in probes:
            headers = dict(p.headers)
            token = http.cookies.get("access_token_web")
            if "api.vinted" in p.url and token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                r = await http.request(p.method, p.url, headers=headers)
            except HttpError as e:
                p.error = str(e)
                continue
            p.status = r.status_code
            p.content_type = r.headers.get("content-type", "")
            p.bytes = len(r.content)
            from .net.http import looks_blocked

            p.blocked = looks_blocked(r)
            p.set_cookies = sorted({c.split("=", 1)[0] for c in r.headers.get_list("set-cookie")})
            text = r.text
            p.json_summary = _json_summary(text)
            low = text.lower()
            p.markers = [m for m in ("__PRERENDERED_STATE__", "__NEXT_DATA__", "application/ld+json",
                                     "datadome", "captcha", "cf-chl") if m.lower() in low]
            p.snippet = text[:400].replace("\n", " ")
            if "html" in p.content_type:
                p.extracted = _extracted(text, p.url)
                if "sprzedajemy" in p.url:
                    p.html_sample = _html_sample(text)
    return probes


async def diagnose(settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or Settings()
    adapters = []
    for key, cls in REGISTRY.items():
        if not cls.configured(settings):  # Allegro / eBay bez kluczy — nie ma czego sprawdzać
            adapters.append(AdapterResult(key, stage="wymaga kluczy API (Ustawienia → Portale)"))
            continue
        try:
            adapters.append(await run_adapter(key, settings))
        except Exception as e:  # diagnostyka nigdy nie może się wysypać
            adapters.append(AdapterResult(key, error=f"{e}\n{traceback.format_exc()}", stage="wyjątek"))
    probes = await run_probes(default_probes())
    return {
        "version": __version__,
        "time": datetime.now().isoformat(timespec="seconds"),
        "adapters": [asdict(a) for a in adapters],
        "probes": [asdict(p) for p in probes],
    }


def format_report(data: dict[str, Any]) -> str:
    lines = [f"PhoneBot {data['version']} — diagnostyka źródeł, {data['time']}", "=" * 70, ""]
    for a in data["adapters"]:
        status = "DZIAŁA" if a["ok"] else "NIE DZIAŁA"
        lines.append(f"[{a['key']}] {status} — {a['stage']} ({a['seconds']} s)")
        lines.append(f"    ofert w odpowiedzi: {a['raw_offers']}, po filtrach: {a['accepted']}"
                     + (f", w obcej walucie: {a['foreign_currency']}" if a.get("foreign_currency") else ""))
        if a["error"]:
            lines.append(f"    błąd ({a['error_type']}): {a['error']}")
        for s in a["samples"]:
            lines.append(f"    • {s}")
        for t in a["trace"]:
            lines.append(f"    → {t.get('method')} {t.get('url')[:150]}")
            lines.append(f"      status={t.get('status')} typ={t.get('content_type')} bajtów={t.get('bytes')} "
                         f"blokada={t.get('blocked')} błąd={t.get('error')} serwer={t.get('server')}")
            if t.get("status", 200) >= 400 or t.get("blocked"):
                lines.append(f"      treść: {t.get('snippet')}")
        lines.append("")
    lines += ["Surowe próby adresów", "-" * 70]
    for p in data["probes"]:
        lines.append(f"{p['name']}: {p['method']} {p['url'][:150]}")
        lines.append(f"    status={p['status']} typ={p['content_type']} bajtów={p['bytes']} blokada={p['blocked']} "
                     f"cookies={p['set_cookies']} znaczniki={p['markers']} błąd={p['error']}")
        if p.get("extracted"):
            lines.append(f"    ekstraktor: {p['extracted'][:1200]}")
        if p.get("html_sample"):
            lines.append(f"    HTML: {p['html_sample']}")
        if p["json_summary"]:
            lines.append(f"    JSON: {p['json_summary'][:1800]}")
        elif p["status"] and (p["status"] >= 400 or not p["markers"]):
            lines.append(f"    treść: {p['snippet']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Diagnostyka źródeł PhoneBot")
    parser.add_argument("--out", help="zapisz raport tekstowy do pliku")
    parser.add_argument("--json", help="zapisz pełny wynik JSON do pliku")
    args = parser.parse_args(argv)
    data = asyncio.run(diagnose())
    report = format_report(data)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(report)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    if sys.stdout is not None:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())
