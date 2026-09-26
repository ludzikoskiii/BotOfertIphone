"""Jednorazowe rozpoznanie portali: czy filtr po ID kategorii działa, skąd wziąć kraj sprzedawcy Vinted.

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


def summary(label: str, titles: list[str]) -> None:
    acc = sum(1 for t in titles if ACCESSORY.search(t))
    say(f"{label}: {len(titles)} ofert, z czego wyglądających na akcesoria: {acc}")
    for t in titles[:10]:
        say("    ", t)


def contexts(text: str, pattern: str, width: int = 160, limit: int = 8) -> list[str]:
    out = []
    for m in re.finditer(pattern, text, re.I):
        s = max(0, m.start() - width)
        out.append(text[s:m.end() + width].replace("\n", " "))
        if len(out) >= limit:
            break
    return out


def probe_vinted(c: httpx.Client) -> None:
    say("\n===== VINTED =====")
    c.head("https://www.vinted.pl/catalog")
    token = c.cookies.get("access_token_web")
    h = {"Accept": "application/json", "Authorization": f"Bearer {token}",
         "Referer": "https://www.vinted.pl/catalog", "Origin": "https://www.vinted.pl"}
    url = "https://api.vinted.pl/svc-catalogue/items"
    base = {"search_text": "iphone", "per_page": 40, "order": "newest_first"}
    user_id = None
    for label, extra in (("bez kategorii", {}), ("catalog_ids=3661", {"catalog_ids": 3661}),
                         ("catalog_ids[]=3661", {"catalog_ids[]": 3661}), ("catalog[]=3661", {"catalog[]": 3661}),
                         ("catalog_ids=3662 (akcesoria)", {"catalog_ids": 3662})):
        pause()
        r = c.get(url, params={**base, **extra}, headers=h)
        items = r.json().get("items", []) if r.status_code == 200 else []
        say(f"[{label}] HTTP {r.status_code}")
        summary("  " + label, [it.get("title", "") for it in items])
        if items and user_id is None:
            user_id = (items[0].get("user") or {}).get("id")
    if user_id:
        for u in (f"https://www.vinted.pl/api/v2/users/{user_id}", f"https://api.vinted.pl/svc-users/users/{user_id}"):
            pause()
            r = c.get(u, headers=h)
            say("user", u, r.status_code, r.headers.get("content-type"))
            if r.status_code == 200 and "json" in (r.headers.get("content-type") or ""):
                txt = json.dumps(r.json(), ensure_ascii=False)
                for ctx in contexts(txt, r"country|city|locale|language", 60, 12):
                    say("  U", ctx)


def probe_allegro(c: httpx.Client) -> None:
    say("\n===== ALLEGRO LOKALNIE =====")
    r = c.get("https://allegrolokalnie.pl/oferty/elektronika/telefony-i-akcesoria-4")
    say("kategoria telefony-i-akcesoria-4", r.status_code, len(r.text))
    links = Counter(re.findall(r'href="(/oferty/elektronika/telefony-i-akcesoria-4/[a-z0-9-]+)', r.text))
    for link, n in links.most_common(30):
        say("  SUB", link, n)
    subs = [link for link in links if re.search(r"smartfon|telefony-komorkowe", link)]
    pause()
    r = c.get("https://allegrolokalnie.pl/oferty/q/iphone")
    summary("  bez kategorii /oferty/q/iphone", [o.title for o in offers_from_html(r.text, "https://allegrolokalnie.pl")])
    for sub in subs[:2]:
        for path in (f"{sub}/q/iphone", f"{sub}?q=iphone"):
            pause()
            r = c.get("https://allegrolokalnie.pl" + path)
            say(f"[{path}] HTTP {r.status_code} -> {r.url}")
            if r.status_code == 200:
                summary("  " + path, [o.title for o in offers_from_html(r.text, "https://allegrolokalnie.pl")])
                for ctx in contexts(r.text, r'"category(?:Id|_id)?"\s*:\s*"?\d+', 40, 4):
                    say("  CAT", ctx)
    if subs:
        pause()
        r = c.get("https://allegrolokalnie.pl" + subs[0])
        deeper = Counter(re.findall(r'href="(' + re.escape(subs[0]) + r'/[a-z0-9-]+)', r.text))
        for link, n in deeper.most_common(20):
            say("  SUB2", link, n)


def probe_sprzedajemy(c: httpx.Client) -> None:
    say("\n===== SPRZEDAJEMY.PL =====")
    r = c.get("https://sprzedajemy.pl/elektronika/telefony-i-akcesoria/telefony-komorkowe/apple-iphone")
    say("kategoria apple-iphone", r.status_code, len(r.text))
    catid = re.search(r'"catid":"([^"]+)"', r.text)
    say("  catid:", catid.group(1) if catid else None)
    for ctx in contexts(r.text, r"inp_category_id=\d+", 40, 4):
        say("  ATOM", ctx)
    ids = re.findall(r"inp_category_id=(\d+)", r.text)
    pause()
    r = c.get("https://sprzedajemy.pl/wszystkie-ogloszenia", params={"inp_text": "iphone"})
    summary("  bez kategorii", [o.title for o in offers_from_html(r.text, "https://sprzedajemy.pl")])
    for cid in list(dict.fromkeys(ids))[:2]:
        pause()
        r = c.get("https://sprzedajemy.pl/wszystkie-ogloszenia", params={"inp_text": "iphone", "inp_category_id": cid})
        say(f"[inp_category_id={cid}] HTTP {r.status_code} -> {r.url}")
        if r.status_code == 200:
            summary(f"  inp_category_id={cid}", [o.title for o in offers_from_html(r.text, "https://sprzedajemy.pl")])
            c2 = re.search(r'"catid":"([^"]+)"', r.text)
            say("  catid na stronie wyników:", c2.group(1) if c2 else None)


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
