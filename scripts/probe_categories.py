"""Jednorazowe rozpoznanie portali: jak filtrować wyniki po ID kategorii.

Uruchamiane w GitHub Actions (kontener deweloperski nie ma dostępu do portali).
Kilkanaście zapytań z odstępami — bez obciążania serwisów.
"""
from __future__ import annotations

import re
import sys
import time
from typing import Any

import httpx

from phonebot.sources.extract import offers_from_html

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
OUT: list[str] = []
ACCESSORY = re.compile(r"etui|case|obal|kryt|h[uü]lle|szk[lł]o|folia|cover|pokrowiec|glass|kabel|[lł]adowark", re.I)


def say(*parts: Any) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    OUT.append(line)


def pause() -> None:
    time.sleep(3)


def summary(label: str, titles: list[str], prices: list[float] | None = None) -> None:
    acc = sum(1 for t in titles if ACCESSORY.search(t))
    extra = f", ceny {min(prices):.0f}–{max(prices):.0f}" if prices else ""
    say(f"{label}: {len(titles)} ofert, akcesoriów wg słów: {acc}{extra}")
    for t in titles[:6]:
        say("    ", t)


def probe_vinted(c: httpx.Client) -> None:
    say("\n===== VINTED =====")
    c.head("https://www.vinted.pl/catalog")
    token = c.cookies.get("access_token_web")
    h = {"Accept": "application/json", "Authorization": f"Bearer {token}",
         "Referer": "https://www.vinted.pl/catalog", "Origin": "https://www.vinted.pl"}
    url = "https://api.vinted.pl/svc-catalogue/items"
    variants: list[tuple[str, dict[str, Any]]] = [
        ("search", {"search_text": "iphone"}),
        ("search+price_from=500", {"search_text": "iphone", "price_from": 500}),
        ("search+catalogIds", {"search_text": "iphone", "catalogIds": 3661}),
        ("search+catalog_id", {"search_text": "iphone", "catalog_id": 3661}),
        ("search+catalog", {"search_text": "iphone", "catalog": 3661}),
        ("tylko catalog_ids=3661", {"catalog_ids": 3661}),
        ("tylko catalog_ids=3662", {"catalog_ids": 3662}),
    ]
    for label, params in variants:
        pause()
        r = c.get(url, params={"per_page": 40, "order": "newest_first", **params}, headers=h)
        items = r.json().get("items", []) if r.status_code == 200 else []
        prices = [float((it.get("price") or {}).get("amount") or 0) for it in items]
        say(f"[{label}] HTTP {r.status_code}")
        summary("  " + label, [it.get("title", "") for it in items], prices)
    pause()
    r = c.get("https://www.vinted.pl/api/v2/catalog/items",
              params={"search_text": "iphone", "catalog_ids": 3661, "per_page": 20}, headers=h)
    say("stary endpoint z catalog_ids:", r.status_code)


def probe_allegro(c: httpx.Client) -> None:
    say("\n===== ALLEGRO LOKALNIE =====")
    for path in ("/oferty/elektronika/telefony-i-akcesoria-4/q/iphone", "/oferty/q/iphone?category=4",
                 "/oferty/elektronika/telefony-i-akcesoria-4?q=iphone"):
        pause()
        r = c.get("https://allegrolokalnie.pl" + path)
        say(f"[{path}] HTTP {r.status_code} -> {r.url}")
        if r.status_code == 200:
            offers = offers_from_html(r.text, "https://allegrolokalnie.pl")
            summary("  " + path, [o.title for o in offers], [o.price for o in offers if o.price])


def probe_sprzedajemy(c: httpx.Client) -> None:
    say("\n===== SPRZEDAJEMY.PL =====")
    base = "https://sprzedajemy.pl"
    for path, params in (
        ("/wszystkie-ogloszenia", {"inp_text[v]": "iphone", "inp_category_id": 1390}),
        ("/elektronika/telefony-i-akcesoria/telefony-komorkowe/apple-iphone", {"inp_text": "iphone"}),
        ("/elektronika/telefony-i-akcesoria/telefony-komorkowe/apple-iphone", {"inp_text[v]": "iphone"}),
        ("/elektronika/telefony-i-akcesoria/telefony-komorkowe/apple-iphone", {"inp_text[v]": "etui"}),
    ):
        pause()
        r = c.get(base + path, params=params)
        catid = re.search(r'"catid":"([^"]+)"', r.text)
        say(f"[{path} {params}] HTTP {r.status_code}, catid={catid.group(1) if catid else None}")
        if r.status_code == 200:
            offers = offers_from_html(r.text, base)
            summary("  wyniki", [o.title for o in offers], [o.price for o in offers if o.price])
    pause()
    r = c.get(base + "/lista-ofert.atom", params={"inp_category_id": 1390, "inp_text[v]": "iphone"})
    titles = re.findall(r"<title[^>]*>(.*?)</title>", r.text, re.S)[1:]
    say(f"[atom inp_category_id=1390] HTTP {r.status_code}, {len(r.text)} B, wpisów: {len(titles)}")
    for t in titles[:6]:
        say("    ", t.strip())


def main() -> int:
    with httpx.Client(headers={"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9"}, follow_redirects=True,
                      timeout=30) as c:
        for fn in (probe_vinted, probe_allegro, probe_sprzedajemy):
            try:
                fn(c)
            except Exception as e:  # noqa: BLE001
                say("ERROR", fn.__name__, repr(e))
            pause()
    with open(sys.argv[1] if len(sys.argv) > 1 else "probe.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
