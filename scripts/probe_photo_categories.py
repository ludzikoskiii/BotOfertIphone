"""Rozpoznanie kategorii „zdjęcia / plakaty / obrazy / dekoracje / kolekcje” na portalach (oszustwo:
sprzedaż zdjęcia iPhone'a zamiast telefonu). Uruchamiane w GitHub Actions — kontener deweloperski nie ma
dostępu do portali. Kilkanaście zapytań z odstępem 4 s, bez obchodzenia blokad.

Wypisuje: drzewo kategorii Vinted pasujące do wzorca (ID + ścieżka), pola kategorii w wynikach wyszukiwania
Vinted, kategorie ofert z wyszukiwania bez filtra kategorii na Allegro Lokalnie i Sprzedajemy.pl.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from typing import Any

import httpx

from phonebot.sources.extract import offers_from_html

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")
OUT: list[str] = []
WANTED = re.compile(r"zdj[eę]c|foto|fotograf|obraz|plakat|poster|grafik|dekorac|kolekc|sztuk|reprodukc|wydruk|"
                    r"art\b|print|pocztówk|album|ramk", re.I)


def say(*parts: Any) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    OUT.append(line)


def pause() -> None:
    time.sleep(4)


def walk_catalogs(nodes: list, path: list[str], out: list[tuple[int, str]]) -> None:
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        title = str(n.get("title") or n.get("name") or "")
        p = [*path, title]
        if n.get("id") is not None:
            out.append((int(n["id"]), " > ".join(p)))
        walk_catalogs(n.get("catalogs") or n.get("children") or [], p, out)


def probe_vinted(c: httpx.Client) -> None:
    say("\n===== VINTED =====")
    c.head("https://www.vinted.pl/catalog")
    token = c.cookies.get("access_token_web")
    say("token:", bool(token))
    h = {"Accept": "application/json", "Authorization": f"Bearer {token}",
         "Referer": "https://www.vinted.pl/catalog", "Origin": "https://www.vinted.pl"}
    found: list[tuple[int, str]] = []
    for url in ("https://www.vinted.pl/api/v2/catalogs", "https://api.vinted.pl/svc-catalogue/catalogs",
                "https://www.vinted.pl/api/v2/catalog/initializers"):
        pause()
        r = c.get(url, headers=h)
        say(f"[{url}] HTTP {r.status_code}, {len(r.content)} B, {r.headers.get('content-type', '')[:30]}")
        if r.status_code != 200:
            continue
        try:
            data = r.json()
        except json.JSONDecodeError:
            continue
        roots = data.get("catalogs") or (data.get("dtos") or {}).get("catalogs") or []
        walk_catalogs(roots, [], found)
        if found:
            break
    say(f"kategorii w drzewie: {len(found)}")
    for cid, path in found:
        if WANTED.search(path):
            say(f"  KATEGORIA {cid}: {path}")
    phones = [(cid, p) for cid, p in found if re.search(r"telefon", p, re.I)]
    for cid, path in phones[:10]:
        say(f"  (telefony) {cid}: {path}")
    names = dict(found)
    for phrase in ("iphone zdjęcie", "iphone plakat", "iphone obraz"):
        pause()
        r = c.get("https://api.vinted.pl/svc-catalogue/items",
                  params={"search_text": phrase, "per_page": 40, "order": "newest_first"}, headers=h)
        items = r.json().get("items", []) if r.status_code == 200 else []
        say(f"[szukaj „{phrase}”] HTTP {r.status_code}, przedmiotów: {len(items)}")
        if items:
            say("  pola przedmiotu:", sorted(items[0].keys()))
        cats = Counter()
        for it in items:
            cid = it.get("catalog_id") or (it.get("catalog") or {}).get("id") if isinstance(it, dict) else None
            cats[cid] += 1
        for cid, n in cats.most_common(12):
            say(f"  catalog_id={cid} ({names.get(cid, '?')}): {n}")
        for it in items[:8]:
            price = (it.get("price") or {}).get("amount") if isinstance(it.get("price"), dict) else it.get("price")
            say(f"    - {str(it.get('title'))[:60]} | {price} | catalog_id={it.get('catalog_id')}")


def probe_html(c: httpx.Client, name: str, base: str, urls: list[tuple[str, dict]]) -> None:
    say(f"\n===== {name} =====")
    for path, params in urls:
        pause()
        r = c.get(base + path, params=params)
        say(f"[{path} {params}] HTTP {r.status_code} -> {str(r.url)[:120]}")
        if r.status_code != 200:
            continue
        offers = offers_from_html(r.text, base)
        cats = Counter(o.category or "—" for o in offers)
        say(f"  ofert: {len(offers)}; kategorie:")
        for cat, n in cats.most_common(15):
            say(f"    {n}× {cat}{'   <== pasuje' if WANTED.search(cat) else ''}")
        for o in offers[:8]:
            say(f"    - {o.title[:60]} | {o.price} | {o.category}")
        ids = Counter(re.findall(r'"(?:catid|categoryId|category_id)"\s*:\s*"?(\d+)', r.text))
        if ids:
            say("  ID kategorii w stronie:", dict(ids.most_common(10)))
        links = sorted(set(re.findall(r'href="(/[^"]*(?:kolekc|sztuk|obraz|plakat|foto|dekorac)[^"]*)"', r.text,
                                      re.I)))[:20]
        for link in links:
            say("    link kategorii:", link)


def main() -> int:
    with httpx.Client(headers={"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9"}, follow_redirects=True,
                      timeout=30) as c:
        steps = [
            lambda: probe_vinted(c),
            lambda: probe_html(c, "ALLEGRO LOKALNIE", "https://allegrolokalnie.pl", [
                ("/oferty/q/iphone%20zdj%C4%99cie", {}), ("/oferty/q/iphone%20plakat", {}),
                ("/oferty/q/iphone", {})]),
            lambda: probe_html(c, "SPRZEDAJEMY.PL", "https://sprzedajemy.pl", [
                ("/wszystkie-ogloszenia", {"inp_text[v]": "iphone zdjęcie"}),
                ("/wszystkie-ogloszenia", {"inp_text[v]": "iphone plakat"}),
                ("/wszystkie-ogloszenia", {"inp_text[v]": "iphone"})]),
        ]
        for step in steps:
            try:
                step()
            except Exception as e:  # noqa: BLE001
                say("ERROR", repr(e))
    with open(sys.argv[1] if len(sys.argv) > 1 else "probe_photo_categories.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
