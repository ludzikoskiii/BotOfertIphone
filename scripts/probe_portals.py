"""Rozpoznanie nowych źródeł (zadanie 6): Lento, Allegro REST API, eBay Browse API, kursy NBP,
ceny referencyjne Swappie / Back Market / Refurbed.

Uruchamiane w GitHub Actions (kontener deweloperski nie ma dostępu do tych serwisów). Kilkanaście zapytań
z odstępami 3 s, robots.txt sprawdzany przed stronami — bez obciążania serwisów i bez obchodzenia blokad.
"""
from __future__ import annotations

import json
import re
import sys
import time
from typing import Any
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
OUT: list[str] = []


def say(*parts: Any) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    OUT.append(line)


def get(c: httpx.Client, url: str, **kw) -> httpx.Response | None:
    time.sleep(3)
    try:
        r = c.get(url, **kw)
    except httpx.HTTPError as e:
        say(f"  {url} -> BŁĄD {e.__class__.__name__}: {e}")
        return None
    say(f"  {url} -> HTTP {r.status_code}, {len(r.content)} B, {r.headers.get('content-type', '')[:40]}"
        + (f", server={r.headers.get('server')}" if r.headers.get("server") else ""))
    return r


def robots(c: httpx.Client, base: str) -> None:
    r = get(c, urljoin(base, "/robots.txt"))
    if r is not None and r.status_code == 200:
        lines = [ln for ln in r.text.splitlines() if ln.lower().startswith(("disallow", "user-agent", "crawl-delay"))]
        say("  robots.txt (pierwsze reguły):", " | ".join(lines[:25]))


def page_facts(r: httpx.Response, base: str, want: str = "iphone") -> list[str]:
    tree = HTMLParser(r.text)
    title = tree.css_first("title")
    say("  <title>:", title.text(strip=True)[:120] if title else "—")
    for node in tree.css('script[type="application/ld+json"]')[:4]:
        say("  JSON-LD:", re.sub(r"\s+", " ", node.text())[:600])
    nd = tree.css_first("script#__NEXT_DATA__")
    if nd:
        try:
            data = json.loads(nd.text())
            say("  __NEXT_DATA__ klucze:", list(data.get("props", {}).get("pageProps", {}).keys())[:30])
        except json.JSONDecodeError:
            say("  __NEXT_DATA__ (niepoprawny JSON)")
    for m in re.finditer(r"(\d[\d\s ]{1,6}(?:,\d{2})?)\s?(?:zł|PLN)", r.text[:400000]):
        say("  cena w tekście:", r.text[max(0, m.start() - 120):m.end() + 20].replace("\n", " ")[:220])
        break
    links = []
    for a in tree.css("a[href]"):
        href = a.attributes.get("href") or ""
        if want in href.lower():
            links.append(urljoin(base, href))
    links = list(dict.fromkeys(links))
    say(f"  linki z „{want}” ({len(links)}):", links[:12])
    return links


def probe_lento(c: httpx.Client) -> None:
    say("\n===== LENTO =====")
    base = "https://www.lento.pl"
    robots(c, base)
    home = get(c, base + "/")
    if home is None or home.status_code != 200:
        return
    links = page_facts(home, base, "telefon")
    for url in [base + "/szukaj.html?q=iphone", base + "/elektronika/telefony-komorkowe.html?q=iphone",
                *[u for u in links if "telefon" in u][:1]]:
        r = get(c, url)
        if r is not None and r.status_code == 200:
            tree = HTMLParser(r.text)
            say("  <title>:", (tree.css_first("title").text(strip=True) if tree.css_first("title") else "—")[:120])
            items = [a for a in tree.css("a[href]") if re.search(r"iphone", a.text() or "", re.I)]
            say(f"  ogłoszeń z „iphone” w tekście linku: {len(items)}")
            for a in items[:5]:
                say("    ", a.attributes.get("href"), "|", a.text(strip=True)[:80])
                parent = a.parent
                for _ in range(4):
                    if parent is not None and parent.parent is not None:
                        parent = parent.parent
                if parent is not None:
                    say("     HTML:", re.sub(r"\s+", " ", parent.html or "")[:900])
            break


def probe_apis(c: httpx.Client) -> None:
    say("\n===== ALLEGRO REST API (bez klucza — oczekiwane 401, czyli serwis dostępny) =====")
    get(c, "https://api.allegro.pl/offers/listing?phrase=iphone",
        headers={"Accept": "application/vnd.allegro.public.v1+json"})
    say("\n===== eBay Browse API (bez klucza — oczekiwane 401) =====")
    get(c, "https://api.ebay.com/buy/browse/v1/item_summary/search?q=iphone")
    say("\n===== NBP (kursy walut) =====")
    r = get(c, "https://api.nbp.pl/api/exchangerates/tables/A/?format=json")
    if r is not None and r.status_code == 200:
        rates = {x["code"]: x["mid"] for x in r.json()[0]["rates"]}
        say("  kursy:", {k: rates.get(k) for k in ("EUR", "USD", "GBP", "CHF", "CZK", "SEK")})


def probe_reference(c: httpx.Client) -> None:
    for name, base, start in [("SWAPPIE", "https://swappie.com", "/pl-pl/"),
                              ("BACK MARKET", "https://www.backmarket.pl", "/pl-pl"),
                              ("REFURBED", "https://www.refurbed.pl", "/")]:
        say(f"\n===== {name} =====")
        robots(c, base)
        home = get(c, base + start)
        if home is None or home.status_code != 200:
            continue
        links = page_facts(home, base)
        target = next((u for u in links if "13" in u), links[0] if links else None)
        if target:
            r = get(c, target)
            if r is not None and r.status_code == 200:
                page_facts(r, base, "iphone-13")


def probe_round2(c: httpx.Client) -> None:
    """Druga runda: struktura listy Lento, warianty cen Refurbed, Back Market."""
    say("\n===== LENTO — podkategorie i lista =====")
    r = get(c, "https://www.lento.pl/elektronika/telefony-i-akcesoria.html")
    if r is not None and r.status_code == 200:
        tree = HTMLParser(r.text)
        cats = [(a.attributes.get("href"), a.text(strip=True)) for a in tree.css(".list-category a")]
        say("  podkategorie:", cats[:20])
        phones = next((h for h, t in cats if h and "telefony-komorkowe" in h), None) or \
            next((h for h, t in cats if h and "smartfon" in h.lower()), None)
        for url in [u for u in [phones, (phones or "").replace(".html", "/apple.html") if phones else None,
                                (phones + "?co=iphone") if phones else None] if u]:
            r2 = get(c, url)
            if r2 is None or r2.status_code != 200:
                continue
            t2 = HTMLParser(r2.text)
            say("  <title>:", (t2.css_first("title").text(strip=True) if t2.css_first("title") else "—")[:120])
            titles = t2.css(".gridlist-title, .title-list-item, h2 a, .ogl-title")
            say(f"  elementy tytułów: {len(titles)}; klasy pierwszych:",
                [(n.tag, n.attributes.get("class")) for n in titles[:3]])
            first = next((n for n in t2.css("a[href]") if re.search(r",\d{6,}\.html", n.attributes.get("href") or "")),
                         None)
            if first is not None:
                box = first
                for _ in range(5):
                    if box.parent is not None:
                        box = box.parent
                say("  pierwsza oferta HTML:", re.sub(r"\s+", " ", box.html or "")[:2500])
                nxt = [a.attributes.get("href") for a in t2.css("a[href]") if "page=" in (a.attributes.get("href") or "")
                       or re.search(r"-\d+\.html$", a.attributes.get("href") or "")][:5]
                say("  paginacja?:", nxt)
                item = get(c, first.attributes.get("href"))
                if item is not None and item.status_code == 200:
                    page_facts(item, "https://www.lento.pl")
                    ti = HTMLParser(item.text)
                    for sel in (".desc", "#description", ".opis", "[itemprop=description]", ".ogl-desc"):
                        n = ti.css_first(sel)
                        if n is not None:
                            say(f"  opis ({sel}):", n.text(strip=True)[:300])
                            break
            break

    say("\n===== REFURBED — warianty iPhone 13 =====")
    time.sleep(8)  # robots.txt: crawl-delay 10 dla botów
    r = get(c, "https://www.refurbed.pl/p/iphone-13/")
    if r is not None and r.status_code == 200:
        tree = HTMLParser(r.text)
        say("  <title>:", tree.css_first("title").text(strip=True)[:150] if tree.css_first("title") else "—")
        for node in tree.css('script[type="application/ld+json"]'):
            try:
                data = json.loads(node.text())
            except json.JSONDecodeError:
                continue
            groups = data if isinstance(data, list) else [data]
            for g in groups:
                if isinstance(g, dict) and g.get("@type") == "ProductGroup":
                    variants = g.get("hasVariant") or []
                    say(f"  ProductGroup: {len(variants)} wariantów; klucze wariantu:",
                        list(variants[0].keys()) if variants else [])
                    for v in variants[:6]:
                        say("    ", json.dumps(v, ensure_ascii=False)[:500])
        for m in re.finditer(r"(Stan|stan)[^<]{0,40}(Bardzo dobry|Dobry|Idealny|Świetny|Doskona\w+|Premium)", r.text):
            say("  stan w tekście:", m.group(0)[:80])
            break

    say("\n===== BACK MARKET =====")
    for url in ("https://www.backmarket.pl/", "https://www.backmarket.pl/pl-pl/l/iphone-13/"):
        r = get(c, url)
        if r is not None and r.status_code == 200:
            links = page_facts(r, "https://www.backmarket.pl", "iphone-13")
            if links:
                r2 = get(c, links[0])
                if r2 is not None and r2.status_code == 200:
                    page_facts(r2, "https://www.backmarket.pl", "iphone")
            break

    say("\n===== SWAPPIE (strona produktu) =====")
    get(c, "https://swappie.com/pl-pl/iphone/iphone-13/")


def main(out: str) -> int:
    with httpx.Client(headers={"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9"}, timeout=20,
                      follow_redirects=True) as c:
        for step in (probe_round2,):
            try:
                step(c)
            except Exception as e:  # noqa: BLE001 — sonda ma zebrać jak najwięcej informacji
                say(f"  !! {step.__name__}: {e.__class__.__name__}: {e}")
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "probe_portals.txt"))
