"""Sonda: jak portale oznaczają sprzedane / zarezerwowane ogłoszenia (do rozpoznawania nieaktualnych ofert).

    python scripts/probe_sold.py wynik.txt URL [URL ...]

Dla każdego adresu: strona ogłoszenia (cała, bez obcinania) — kod HTTP, rozmiar, gdzie na stronie (bajt) pojawiają
się znaczniki sprzedaży/rezerwacji i ich otoczenie; dla Vinted dodatkowo dane przedmiotu z API (z anonimową sesją,
jak aplikacja) i przegląd pól w wynikach wyszukiwania. Kilka zapytań, odstęp 5 s, bez obchodzenia blokad.
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    from phonebot.net.http import DEFAULT_HEADERS
except Exception:  # noqa: BLE001
    DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                     "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                       "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.6"}

OUT: list[str] = []
MARKERS = [
    r"sprzedan\w*", r"zarezerwowan\w*", r"rezerwacj\w*", r"zakończon\w*", r"nieaktualn\w*", r"niedostępn\w*",
    r"\bsold\b", r"\breserved\b", r"is_closed", r"is_reserved", r"is_hidden", r"item_closing_action",
    r"closing_action", r"is_processing", r"transaction_permitted", r"can_buy", r"instant_buy", r"\"status\"",
    r"SoldOut", r"OutOfStock", r"InStock", r"availability", r"og:availability", r"item-status", r"sold_",
]
_RX = re.compile("|".join(f"({m})" for m in MARKERS), re.I)


def say(*parts) -> None:
    line = " ".join(str(p) for p in parts)
    print(line, flush=True)
    OUT.append(line)


def get(c: httpx.Client, url: str, **kw) -> httpx.Response | None:
    time.sleep(5)
    try:
        r = c.get(url, **kw)
    except httpx.HTTPError as e:
        say(f"  {url} -> BŁĄD SIECI {e.__class__.__name__}: {e}")
        return None
    say(f"  {url} -> HTTP {r.status_code}, {len(r.content)} B, typ={r.headers.get('content-type', '')[:40]}")
    return r


def markers(text: str, limit: int = 60) -> None:
    """Znaczniki na stronie: pozycja (bajt), dopasowanie, otoczenie."""
    seen: dict[str, int] = {}
    shown = 0
    for m in _RX.finditer(text):
        word = m.group(0).lower()
        seen[word] = seen.get(word, 0) + 1
        if seen[word] > 3 or shown >= limit:
            continue
        shown += 1
        pos = len(text[:m.start()].encode("utf-8"))
        ctx = re.sub(r"\s+", " ", text[max(0, m.start() - 110):m.end() + 110])
        say(f"    @{pos:>8} B  [{m.group(0)}]  …{ctx}…")
    say("  liczba wystąpień:", json.dumps(seen, ensure_ascii=False))


def json_ld(text: str) -> None:
    for block in re.findall(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', text, re.S | re.I):
        say("  JSON-LD:", re.sub(r"\s+", " ", block)[:600])


def vinted_item_api(c: httpx.Client, item_id: str) -> None:
    token = c.cookies.get("access_token_web")
    if not token:
        say("  brak tokenu sesji Vinted — pomijam API przedmiotu")
        return
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}", "Referer": "https://www.vinted.pl/"}
    for url in (f"https://www.vinted.pl/api/v2/items/{item_id}", f"https://www.vinted.pl/api/v2/items/{item_id}/details",
                f"https://www.vinted.pl/api/v2/item_upload/items/{item_id}"):
        r = get(c, url, headers=headers)
        if r is None or r.status_code != 200:
            if r is not None:
                say("   ", re.sub(r"\s+", " ", r.text[:300]))
            continue
        try:
            data = r.json()
        except ValueError:
            say("    (nie JSON)")
            continue
        item = data.get("item", data) if isinstance(data, dict) else data
        if isinstance(item, dict):
            keys = sorted(item)
            say("    klucze:", ", ".join(keys)[:1500])
            for k in keys:
                if re.search(r"clos|reserv|sold|status|hidden|draft|process|buy|active|available", k, re.I):
                    say(f"    {k} = {json.dumps(item[k], ensure_ascii=False)[:200]}")
        return


# w danych strony cudzysłowy bywają poprzedzone ukośnikiem (\\"can_buy\\":false) — stąd \\\\?
FLAGS = re.compile(r'\\?"hates_you\\?":(?:true|false),\\?"can_buy\\?":(true|false),\\?"instant_buy\\?":(true|false),'
                   r'\\?"is_reserved\\?":(true|false)(?:,\\?"is_hidden\\?":(true|false))?')


def vinted_flags(c: httpx.Client, url: str) -> None:
    """Próba kontrolna: flagi przedmiotu na stronie dostępnego ogłoszenia (cała strona)."""
    r = get(c, url)
    if r is None:
        return
    found = FLAGS.findall(r.text)
    pos = [len(r.text[:m.start()].encode("utf-8")) for m in FLAGS.finditer(r.text)]
    say(f"    flagi (can_buy, instant_buy, is_reserved, is_hidden): {found} @ {pos} B z {len(r.content)} B")


def vinted_search_fields(c: httpx.Client) -> list[str]:
    token = c.cookies.get("access_token_web")
    if not token:
        return []
    headers = {"Accept": "application/json", "Authorization": f"Bearer {token}", "Referer": "https://www.vinted.pl/"}
    r = get(c, "https://api.vinted.pl/svc-catalogue/items", headers=headers,
            params={"search_text": "iphone 13", "per_page": 96, "page": 1, "order": "newest_first"})
    if r is None or r.status_code != 200:
        return []
    data = r.json()
    items = []

    def walk(x):
        if isinstance(x, dict):
            if "id" in x and ("title" in x or "price" in x):
                items.append(x)
            for v in x.values():
                walk(v)
        elif isinstance(x, list):
            for v in x:
                walk(v)
    walk(data)
    keys: dict[str, int] = {}
    for it in items:
        for k in it:
            keys[k] = keys.get(k, 0) + 1
    say(f"  wyniki wyszukiwania: {len(items)} przedmiotów; pola:", json.dumps(keys, ensure_ascii=False)[:2000])
    for k in keys:
        if re.search(r"clos|reserv|sold|status|hidden|process|buy|active|available", k, re.I):
            vals: dict[str, int] = {}
            for it in items:
                v = json.dumps(it.get(k), ensure_ascii=False)[:60]
                vals[v] = vals.get(v, 0) + 1
            say(f"    {k}: {json.dumps(vals, ensure_ascii=False)[:400]}")
    return [it["url"] for it in items if isinstance(it.get("url"), str)][:3]


def main() -> int:
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "probe_sold.txt")
    urls = [u for arg in sys.argv[2:] for u in re.split(r"[\s,]+", arg) if u.startswith("http")]
    with httpx.Client(headers=DEFAULT_HEADERS, timeout=30, follow_redirects=True) as c:
        if any("vinted." in u for u in urls):
            say("== Vinted: sesja anonimowa (jak aplikacja)")
            get(c, "https://www.vinted.pl/catalog")
            say("  token sesji:", "jest" if c.cookies.get("access_token_web") else "BRAK")
        for url in urls:
            say(f"\n== {url}")
            r = get(c, url)
            if r is not None:
                if r.url != httpx.URL(url):
                    say("  przekierowanie na:", r.url)
                text = r.text
                json_ld(text)
                markers(text)
            m = re.search(r"vinted\.[a-z.]+/items/(\d+)", url)
            if m:
                vinted_item_api(c, m.group(1))
        if any("vinted." in u for u in urls):
            say("\n== Vinted: pola w wynikach wyszukiwania (czy są tam rezerwacje / sprzedane)")
            live = vinted_search_fields(c)
            say("\n== Vinted: próba kontrolna — dostępne ogłoszenia z wyszukiwarki")
            for u in live:
                vinted_flags(c, u if u.startswith("http") else "https://www.vinted.pl" + u)
            for u in [u for u in urls if "vinted." in u]:
                say("  zgłoszone:", u)
                vinted_flags(c, u)
    out.write_text("\n".join(OUT), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
