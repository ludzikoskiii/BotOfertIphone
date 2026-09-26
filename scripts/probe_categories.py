"""Jednorazowe rozpoznanie portali: ID kategorii telefonów, kraj i sprzedawca w danych Vinted.

Uruchamiane w GitHub Actions (kontener deweloperski nie ma dostępu do portali).
Kilkanaście zapytań z odstępami — bez obciążania serwisów.
"""
from __future__ import annotations

import json
import re
import sys
import time
from collections import Counter
from typing import Any

import httpx

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
OUT: list[str] = []


def say(*parts: Any) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    OUT.append(line)


def pause() -> None:
    time.sleep(3)


def keys_union(items: list[dict], path: str | None = None) -> Counter:
    c: Counter = Counter()
    for it in items:
        obj = it.get(path) if path else it
        if isinstance(obj, dict):
            c.update(obj.keys())
    return c


def contexts(text: str, pattern: str, width: int = 160, limit: int = 8) -> list[str]:
    out = []
    for m in re.finditer(pattern, text, re.I):
        s = max(0, m.start() - width)
        out.append(text[s:m.end() + width].replace("\n", " "))
        if len(out) >= limit:
            break
    return out


def walk_catalogs(nodes: Any, depth: int = 0, parents: str = "") -> None:
    if isinstance(nodes, dict):
        nodes = nodes.get("catalogs") or nodes.get("dtos") or [nodes]
    for n in nodes or []:
        if not isinstance(n, dict):
            continue
        title = str(n.get("title") or n.get("name") or "")
        path = f"{parents} > {title}" if parents else title
        if re.search(r"telefon|smartfon|phone|elektronik|electronic|akcesori|etui", title, re.I):
            say(f"  CATALOG id={n.get('id')} code={n.get('code')} path={path}")
        walk_catalogs(n.get("catalogs") or [], depth + 1, path)


def probe_vinted(c: httpx.Client) -> None:
    say("\n===== VINTED =====")
    r = c.head("https://www.vinted.pl/catalog")
    token = c.cookies.get("access_token_web")
    say("session", r.status_code, "token" if token else "NO TOKEN")
    h = {"Accept": "application/json", "Authorization": f"Bearer {token}",
         "Referer": "https://www.vinted.pl/catalog", "Origin": "https://www.vinted.pl"}
    pause()
    r = c.get("https://api.vinted.pl/svc-catalogue/items",
              params={"search_text": "iphone", "per_page": 40, "order": "newest_first"}, headers=h)
    say("items", r.status_code)
    items = r.json().get("items", []) if r.status_code == 200 else []
    say("item keys:", dict(keys_union(items)))
    say("user keys:", dict(keys_union(items, "user")))
    say("item_box keys:", dict(keys_union(items, "item_box")))
    say("currencies:", Counter((it.get("price") or {}).get("currency_code") for it in items))
    for it in items[:3]:
        say("ITEM", json.dumps(it, ensure_ascii=False)[:1800])
    say("top-level keys:", list(r.json().keys()) if r.status_code == 200 else "-")
    if r.status_code == 200:
        say("search_tracking_params:", json.dumps(r.json().get("search_tracking_params"), ensure_ascii=False)[:600])
    logins = Counter((it.get("user") or {}).get("login") for it in items)
    say("sellers:", logins.most_common(8))

    pause()
    for url in ("https://www.vinted.pl/api/v2/catalogs", "https://api.vinted.pl/svc-catalogue/catalogs",
                "https://www.vinted.pl/api/v2/catalog/initializers"):
        r = c.get(url, headers=h)
        say("catalogs", url, r.status_code, r.headers.get("content-type"))
        if r.status_code == 200 and "json" in (r.headers.get("content-type") or ""):
            data = r.json()
            say("  keys:", list(data.keys())[:20] if isinstance(data, dict) else type(data))
            walk_catalogs(data.get("catalogs") if isinstance(data, dict) and "catalogs" in data else data)
            if isinstance(data, dict) and "dtos" in data:
                walk_catalogs(data["dtos"])
        pause()

    r = c.get("https://www.vinted.pl/catalog", params={"search_text": "iphone"})
    say("catalog html", r.status_code, len(r.text))
    for ctx in contexts(r.text, r"Telefony kom[oó]rkowe|Smartfony|Mobile phones", limit=6):
        say("  CTX", ctx)
    for ctx in contexts(r.text, r"catalog\[\]=\d+|catalog_ids?\W{1,4}\d+", 60, 10):
        say("  CAT", ctx)

    if items:
        iid = items[0]["id"]
        for url in (f"https://www.vinted.pl/api/v2/items/{iid}/details", f"https://www.vinted.pl/api/v2/items/{iid}",
                    f"https://api.vinted.pl/svc-catalogue/items/{iid}"):
            pause()
            r = c.get(url, headers=h)
            say("item detail", url, r.status_code)
            if r.status_code == 200 and "json" in (r.headers.get("content-type") or ""):
                txt = json.dumps(r.json(), ensure_ascii=False)
                for ctx in contexts(txt, r"country|catalog_id|city|language|iso_code", 80, 12):
                    say("  D", ctx)
        pause()
        r = c.get(f"https://www.vinted.pl/items/{iid}")
        say("item html", r.status_code, len(r.text))
        for ctx in contexts(r.text, r"country_(?:code|iso|title|id)|countryCode|catalog_id", 80, 12):
            say("  H", ctx)


def probe_vinted_filtered(c: httpx.Client, catalog_ids: list[str]) -> None:
    r = c.head("https://www.vinted.pl/catalog")
    token = c.cookies.get("access_token_web")
    h = {"Accept": "application/json", "Authorization": f"Bearer {token}",
         "Referer": "https://www.vinted.pl/catalog", "Origin": "https://www.vinted.pl"}
    for cid in catalog_ids:
        pause()
        r = c.get("https://api.vinted.pl/svc-catalogue/items",
                  params={"search_text": "iphone", "per_page": 20, "catalog_ids": cid}, headers=h)
        items = r.json().get("items", []) if r.status_code == 200 else []
        say(f"catalog_ids={cid}: HTTP {r.status_code}, {len(items)} items:",
            [it.get("title") for it in items[:8]])


def probe_allegro(c: httpx.Client) -> None:
    say("\n===== ALLEGRO LOKALNIE =====")
    r = c.get("https://allegrolokalnie.pl/oferty/q/iphone")
    say("search", r.status_code, len(r.text))
    links = Counter(re.findall(r'href="(/oferty/[a-z0-9-]+(?:/[a-z0-9-]+)*)', r.text))
    for link, n in links.most_common(40):
        if re.search(r"telefon|smartfon|gsm|iphone|apple", link):
            say("  LINK", link, n)
    for ctx in contexts(r.text, r'"category(?:Id|_id|Path)?"\s*:', 100, 10):
        say("  CTX", ctx)
    pause()
    r = c.get("https://allegrolokalnie.pl/oferty/telefony-i-akcesoria")
    say("category guess telefony-i-akcesoria", r.status_code, r.url)
    for link, n in Counter(re.findall(r'href="(/oferty/[a-z0-9-]+(?:/[a-z0-9-]+)*)', r.text)).most_common(60):
        if re.search(r"telefon|smartfon|gsm|iphone|apple", link):
            say("  LINK", link, n)


def probe_sprzedajemy(c: httpx.Client) -> None:
    say("\n===== SPRZEDAJEMY.PL =====")
    r = c.get("https://sprzedajemy.pl/wszystkie-ogloszenia", params={"inp_text": "iphone"})
    say("search", r.status_code, len(r.text))
    for link, n in Counter(re.findall(r'href="(https://sprzedajemy\.pl/[a-z0-9/-]*telefon[a-z0-9/-]*)', r.text)
                           ).most_common(20):
        say("  LINK", link, n)
    for ctx in contexts(r.text, r'name="inp_category[^"]*"|category_id|catId|"category"', 120, 8):
        say("  CTX", ctx)


def main() -> int:
    with httpx.Client(headers={"User-Agent": UA, "Accept-Language": "pl-PL,pl;q=0.9"}, follow_redirects=True,
                      timeout=30) as c:
        for fn in (probe_vinted, probe_allegro, probe_sprzedajemy):
            try:
                fn(c)
            except Exception as e:  # noqa: BLE001
                say("ERROR", fn.__name__, repr(e))
            pause()
        ids = re.findall(r"CATALOG id=(\d+)[^\n]*(?:telefon|smartfon|phone)", "\n".join(OUT), re.I)
        if ids:
            try:
                probe_vinted_filtered(c, list(dict.fromkeys(ids))[:4])
            except Exception as e:  # noqa: BLE001
                say("ERROR filtered", repr(e))
    with open(sys.argv[1] if len(sys.argv) > 1 else "probe.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(OUT))
    return 0


if __name__ == "__main__":
    sys.exit(main())
