"""Rozpoznanie: czy strona pojedynczej oferty daje opis, gdy wyniki wyszukiwania go nie mają.

Potrzebne do etapu 3 (lokalny model językowy czyta opisy ofert „DO WERYFIKACJI”). Uruchamiane
w GitHub Actions (kontener deweloperski nie ma dostępu do portali). Na portal: jedno wyszukiwanie
i 3 strony ofert, z przerwą 4 s między zapytaniami — bez obciążania serwisów.
"""
from __future__ import annotations

import html as html_lib
import re
import sys
import time
from typing import Any

import httpx
from selectolax.parser import HTMLParser

from phonebot.net.http import DEFAULT_HEADERS, looks_blocked
from phonebot.sources.extract import embedded_json, offers_from_html, walk

OUT: list[str] = []
PAUSE_S = 4


def say(*parts: Any) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    OUT.append(line)


def pause() -> None:
    time.sleep(PAUSE_S)


def short(text: str, n: int = 160) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:n] + ("…" if len(text) > n else "")


def describe_page(label: str, r: httpx.Response) -> None:
    """Gdzie na stronie oferty jest opis: JSON-LD / osadzony JSON / meta / blok HTML."""
    say(f"  {label}: HTTP {r.status_code}, {len(r.content) // 1024} kB, blokada: {looks_blocked(r)}")
    if r.status_code != 200:
        return
    page = r.text
    found: list[tuple[str, str]] = []
    for block in embedded_json(page):
        for d in walk(block):
            for key in ("description", "desc", "body", "text"):
                v = d.get(key)
                if isinstance(v, str) and len(v) >= 20:
                    found.append((f"json:{key}", v))
    tree = HTMLParser(page)
    for sel in ('meta[property="og:description"]', 'meta[name="description"]'):
        node = tree.css_first(sel)
        if node and node.attributes.get("content"):
            found.append((sel, node.attributes["content"] or ""))
    for sel in ('[itemprop="description"]', '[data-testid*="description"]', '[class*="escription"]',
                '[class*="opis"]', '[id*="escription"]'):
        for node in tree.css(sel)[:2]:
            txt = node.text(strip=True)
            if len(txt) >= 20:
                found.append((f"html:{sel}", txt))
    if not found:
        say("    opisu nie znaleziono")
    seen = set()
    for where, txt in sorted(found, key=lambda x: -len(x[1])):
        key = short(txt, 60)
        if key in seen:
            continue
        seen.add(key)
        say(f"    [{where}] {len(txt)} zn.: {short(html_lib.unescape(txt))}")
        if len(seen) >= 4:
            break


def json_strings(node: Any, path: str = "") -> list[tuple[str, str]]:
    """Wszystkie napisy z osadzonego JSON z ich ścieżką (do znalezienia pola z opisem)."""
    out: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for k, v in node.items():
            out += json_strings(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node[:50]):
            out += json_strings(v, f"{path}[{i}]")
    elif isinstance(node, str):
        out.append((path, node))
    return out


def deep_dive(label: str, page: str, raw: bytes, title: str) -> None:
    """Gdzie dokładnie jest opis: długie napisy w JSON (ze ścieżką), długie teksty w HTML, pozycja w bajtach."""
    say(f"  >>> szczegóły strony ({label})")
    blocks = embedded_json(page)
    say(f"    bloki JSON: {len(blocks)}")
    longest = sorted((x for b in blocks for x in json_strings(b)), key=lambda x: -len(x[1]))
    shown = 0
    for path, txt in longest:
        if len(txt) < 60 or txt.startswith("http") or "{" in txt[:5]:
            continue
        say(f"    json{path[:90]} ({len(txt)} zn.): {short(html_lib.unescape(txt), 120)}")
        shown += 1
        if shown >= 8:
            break
    tree = HTMLParser(page)
    texts = []
    for node in tree.css("div, p, section, article, span"):
        own = node.text(deep=False, strip=True)
        if len(own) >= 60:
            cls = node.attributes.get("class") or ""
            texts.append((len(own), node.tag, cls[:60], node.attributes.get("data-testid") or "", own))
    for n, tag, cls, tid, own in sorted(texts, reverse=True)[:8]:
        say(f"    html <{tag} class='{cls}' data-testid='{tid}'> ({n} zn.): {short(own, 120)}")
    for needle in ("description", "opis", "Opis"):
        pos = raw.find(needle.encode())
        say(f"    pierwsze „{needle}” w bajcie: {pos} z {len(raw)}")


def probe_html_portal(c: httpx.Client, name: str, search_url: str, params: dict[str, Any], base: str,
                      deep: bool = False) -> None:
    say(f"\n===== {name} =====")
    r = c.get(search_url, params=params)
    offers = offers_from_html(r.text, base) if r.status_code == 200 else []
    with_desc = [o for o in offers if len(o.description or "") >= 20]
    say(f"wyszukiwanie: HTTP {r.status_code}, ofert {len(offers)}, z opisem w wynikach: {len(with_desc)}")
    for o in with_desc[:2]:
        say(f"  opis w wynikach: {short(o.description)}")
    for o in offers[:2 if deep else 3]:
        pause()
        say(f"- {short(o.title, 70)} → {o.url}")
        page = c.get(o.url)
        describe_page("strona oferty", page)
        if deep and page.status_code == 200:
            deep_dive(name, page.text, page.content, o.title)


def probe_vinted(c: httpx.Client, limit: int = 3) -> None:
    say("\n===== VINTED =====")
    c.get("https://www.vinted.pl/catalog")
    token = c.cookies.get("access_token_web")
    h = {"Accept": "application/json", "Authorization": f"Bearer {token}", "Referer": "https://www.vinted.pl/catalog",
         "Origin": "https://www.vinted.pl"}
    pause()
    r = c.get("https://api.vinted.pl/svc-catalogue/items",
              params={"search_text": "iphone 13", "per_page": 20, "order": "newest_first", "price_from": 150}, headers=h)
    items = r.json().get("items", []) if r.status_code == 200 else []
    say(f"katalog: HTTP {r.status_code}, przedmiotów {len(items)}")
    if items:
        say("  pola przedmiotu w katalogu:", sorted(items[0].keys()))
    for it in items[:limit]:
        iid = it["id"]
        say(f"- {short(str(it.get('title')), 70)} (id {iid}), opis w katalogu: {len(str(it.get('description') or ''))} zn.")
        for label, url, headers in ():  # API przedmiotu: 404 / 403 (ochrona antybotowa) — nie używamy
            pause()
            rr = c.get(url, headers=headers)
            desc = ""
            if rr.status_code == 200 and "json" in rr.headers.get("content-type", ""):
                for d in walk(rr.json()):
                    v = d.get("description")
                    if isinstance(v, str) and len(v) > len(desc):
                        desc = v
            say(f"  {label}: HTTP {rr.status_code}, blokada: {looks_blocked(rr)}, opis {len(desc)} zn.: {short(desc)}")
        pause()
        path = it.get("url") or it.get("path") or f"/items/{iid}"
        page = c.get(path if path.startswith("http") else "https://www.vinted.pl" + path)
        describe_page("strona HTML", page)
        if page.status_code == 200:
            raw = page.content
            for needle in (b'"description"', b'og:description', b'<script id="__NEXT_DATA__"', b"self.__next_f"):
                say(f"    pozycja {needle.decode()}: {raw.find(needle)} z {len(raw)} B")


def main() -> int:
    out_path = sys.argv[1] if len(sys.argv) > 1 else "probe_descriptions.txt"
    with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True, timeout=30) as c:
        probe_html_portal(c, "ALLEGRO LOKALNIE",
                          "https://allegrolokalnie.pl/oferty/elektronika/telefony-i-akcesoria-4/q/iphone%2013",
                          {"sort": "startingTime-desc"}, "https://allegrolokalnie.pl", deep=True)
        pause()
        probe_vinted(c, limit=1)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(OUT) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
