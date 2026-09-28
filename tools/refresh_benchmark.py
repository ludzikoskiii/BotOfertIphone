"""Porównanie odświeżania: pełne pobieranie co 15 min (przed) vs przyrostowe co ~2 min (po).

    python tools/refresh_benchmark.py [--json wynik.json]

Prawdziwy skaner i adapter Vinted na makiecie portalu (bez sieci): rynek 2000 ogłoszeń posortowanych od
najnowszych, frazy z domyślnych ustawień (tryb naprawy: 4 frazy, do 3 stron), między odświeżeniami przybywa
kilka nowych ogłoszeń. Liczone są zapytania; czas przebiegu szacowany z limitu tempa programu (domyślnie
4 s między zapytaniami do jednego portalu — portale odpytywane równolegle, więc czas = zapytania jednego portalu × 4 s).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phonebot.core.settings import Settings  # noqa: E402
from phonebot.net.http import HostRateLimiter, HttpClient  # noqa: E402
from phonebot.services.scanner import Scanner  # noqa: E402
from phonebot.sources.vinted import PER_PAGE, VintedAdapter  # noqa: E402
from phonebot.storage.db import open_database  # noqa: E402

MODELS = ["iPhone 11", "iPhone 12", "iPhone 13", "iPhone 13 Pro", "iPhone 14", "iPhone 14 Pro", "iPhone 15"]


class Market:
    """Ogłoszenia od najnowszych; frazy „uszkodzony/zbity/na części” to podzbiory."""

    def __init__(self, n: int = 2000):
        self.next_id = 10_000_000
        self.items: list[dict] = []
        for _ in range(n):
            self.add()

    def add(self, count: int = 1) -> None:
        for _ in range(count):
            i = self.next_id
            self.next_id += 1
            damaged = i % 4 == 0
            title = f"{MODELS[i % len(MODELS)]} 128GB" + (" zbity ekran" if damaged else "")
            self.items.insert(0, {"id": i, "title": title, "price": {"amount": str(900 + i % 1500),
                                                                       "currency_code": "PLN"},
                                  "url": f"/items/{i}", "user": {"id": i % 500, "login": f"u{i % 500}"},
                                  "photo": {"url": f"https://img/{i}.jpg"}, "damaged": damaged})

    def search(self, phrase: str, page: int) -> dict:
        subset = self.items if phrase == "iphone" else [x for x in self.items if x["damaged"]]
        chunk = subset[(page - 1) * PER_PAGE: page * PER_PAGE]
        pages = max(1, -(-len(subset) // PER_PAGE))
        return {"items": [{k: v for k, v in x.items() if k != "damaged"} for x in chunk],
                "pagination": {"current_page": page, "total_pages": pages, "per_page": PER_PAGE}}


def run(conn, market: Market, *, incremental: bool) -> dict:
    counts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counts["n"] += 1
        if request.url.host == "www.vinted.pl" and request.url.path.startswith("/api/v2/users/"):
            uid = int(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"user": {"id": uid, "login": f"u{uid}", "country_code": "PL"}})
        if request.url.host == "www.vinted.pl":
            return httpx.Response(200, text="", headers={"set-cookie": "access_token_web=t; Path=/"})
        return httpx.Response(200, json=market.search(request.url.params["search_text"],
                                                      int(request.url.params.get("page", 1))))

    s = Settings()
    s.enabled_sources = {k: k == "vinted" for k in s.enabled_sources}
    s.source_min_price = {}
    limiter = HostRateLimiter(0)
    scanner = Scanner(conn, s, limiter,
                      http_factory=lambda: HttpClient(limiter, transport=httpx.MockTransport(handler),
                                                      wait=lambda x: 0),
                      adapter_factory=lambda http, st: [VintedAdapter(http, st)])
    t = time.perf_counter()
    report = asyncio.run(scanner.run(force=True, incremental=incremental))
    return {"requests": counts["n"], "new": report.new_count, "cpu_s": round(time.perf_counter() - t, 2),
            "est_s": counts["n"] * s.request_delay_s}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json")
    args = ap.parse_args()
    delay = Settings().request_delay_s
    with tempfile.TemporaryDirectory() as tmp:
        conn = open_database(Path(tmp) / "b.sqlite3")
        market = Market()
        first = run(conn, market, incremental=False)  # baza startowa (pierwsze uruchomienie)
        market.add(5)
        full = run(conn, market, incremental=False)
        market.add(5)
        quick = run(conn, market, incremental=True)
        market.add(150)  # dużo nowych naraz (np. po nocy) — szybkie odświeżanie sięga dalej
        quick_many = run(conn, market, incremental=True)
        conn.close()
    old_per_h, new_per_h = 60 / 15, 60 / 2
    out = {
        "delay_s": delay,
        "przed_pelne": full, "po_szybkie": quick, "po_szybkie_150_nowych": quick_many, "pierwsze": first,
        "zapytania_na_godzine_portal": {"przed": round(full["requests"] * old_per_h),
                                        "po": round(quick["requests"] * new_per_h + full["requests"] / 24)},
        "srednie_opoznienie_nowej_oferty_min": {"przed": round(15 / 2 + full["est_s"] / 60, 1),
                                                "po": round(2 / 2 + quick["est_s"] / 60, 1)},
    }
    print(f"Pełne pobranie (przed, co 15 min): {full['requests']} zapytań, ~{full['est_s']:.0f} s na portal, "
          f"nowych: {full['new']}")
    print(f"Szybkie odświeżanie (po, co ~2 min): {quick['requests']} zapytania, ~{quick['est_s']:.0f} s, "
          f"nowych: {quick['new']}")
    print(f"Szybkie przy 150 nowych naraz: {quick_many['requests']} zapytania, ~{quick_many['est_s']:.0f} s, "
          f"nowych: {quick_many['new']}")
    print("Zapytania na godzinę (1 portal):", out["zapytania_na_godzine_portal"])
    print("Średnie opóźnienie pojawienia się nowej oferty (min):", out["srednie_opoznienie_nowej_oferty_min"])
    if args.json:
        Path(args.json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
