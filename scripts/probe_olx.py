"""Diagnoza OLX.pl: na którym etapie pobieranie ofert przestaje działać.

    python scripts/probe_olx.py [wynik.txt]

Kilka zapytań (odstęp 5 s), ten sam User-Agent co aplikacja, robots.txt sprawdzany najpierw. Bez obchodzenia
zabezpieczeń: gdy OLX odpowiada blokadą, sonda to zapisuje i kończy dany etap. Można uruchomić w GitHub
Actions (adres centrum danych) i na własnym komputerze (łącze domowe) — wyniki bywają różne.
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
except Exception:  # noqa: BLE001 — sonda działa też bez zależności aplikacji
    DEFAULT_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                     "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
                       "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.6"}

BASE = "https://www.olx.pl"
API = f"{BASE}/api/v1/offers/"
SEARCH = f"{BASE}/elektronika/telefony/smartfony-telefony-komorkowe/iphone/q-iphone-13/"
DOCS = ["https://developer.olx.pl/", "https://developer.olx.pl/api/doc"]
OUT: list[str] = []
BLOCK_MARKERS = ("request blocked", "captcha", "datadome", "cf-chl", "px-captcha", "are you a robot",
                 "jesteś robotem", "access denied")


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
    h = r.headers
    say(f"  {url} -> HTTP {r.status_code}, {len(r.content)} B, typ={h.get('content-type', '')[:40]}, "
        f"server={h.get('server', '')}, x-cache={h.get('x-cache', '')}, retry-after={h.get('retry-after', '')}")
    low = r.text[:5000].lower()
    marks = [m for m in BLOCK_MARKERS if m in low]
    if marks:
        say("  ślady blokady w treści:", ", ".join(marks))
    if r.status_code >= 400:
        say("  początek odpowiedzi:", re.sub(r"\s+", " ", r.text[:300]))
    return r


def stage_robots(c: httpx.Client) -> None:
    say("\n[1] robots.txt")
    r = get(c, f"{BASE}/robots.txt")
    if r is not None and r.status_code == 200:
        rules = [ln.strip() for ln in r.text.splitlines()
                 if ln.lower().startswith(("user-agent", "disallow", "allow", "crawl-delay"))]
        say("  reguły:", " | ".join(rules[:40]))
        say("  /api/ zablokowane dla wszystkich:", any(x.lower() in ("disallow: /api/", "disallow: /api")
                                                      for x in rules))


def stage_api(c: httpx.Client) -> None:
    say("\n[2] JSON /api/v1/offers/ (poprzednie podejście adaptera)")
    r = get(c, API, params={"offset": 0, "limit": 5, "query": "iphone 13", "sort_by": "created_at:desc"},
            headers={"Accept": "application/json"})
    if r is None or r.status_code != 200:
        return
    try:
        data = r.json()
    except json.JSONDecodeError:
        say("  PARSOWANIE: odpowiedź nie jest JSON-em")
        return
    items = data.get("data") if isinstance(data, dict) else None
    say("  PARSOWANIE: klucze:", list(data)[:10] if isinstance(data, dict) else type(data).__name__,
        "| ofert w 'data':", len(items) if isinstance(items, list) else "brak")
    for it in (items or [])[:3]:
        price = next((p.get("value", {}).get("value") for p in it.get("params", []) if p.get("key") == "price"), None)
        say(f"    - {it.get('id')} | {str(it.get('title'))[:60]} | {price} zł")


def stage_html(c: httpx.Client) -> None:
    say("\n[3] Strona wyników (HTML)")
    r = get(c, SEARCH)
    if r is None or r.status_code != 200:
        return
    t = r.text
    say("  PARSOWANIE: data-cy=l-card:", t.count('data-cy="l-card"'), "| __PRERENDERED_STATE__:",
        "__PRERENDERED_STATE__" in t, "| JSON-LD:", t.count("application/ld+json"))


def stage_official(c: httpx.Client) -> None:
    say("\n[4] Oficjalne API (Partner API) — dokumentacja")
    for url in DOCS:
        r = get(c, url)
        if r is not None and r.status_code == 200:
            low = r.text.lower()
            say("  wzmianki:", {k: low.count(k) for k in ("search", "adverts", "partner", "oauth", "own")})


def main(out: str) -> int:
    with httpx.Client(headers=DEFAULT_HEADERS, timeout=20, follow_redirects=True) as c:
        say("Sonda OLX,", time.strftime("%Y-%m-%d %H:%M"), "| UA:", DEFAULT_HEADERS["User-Agent"][:60])
        try:
            ip = c.get("https://api.ipify.org?format=json", timeout=10).json().get("ip", "?")
            say("Publiczny adres IP sondy:", ip)
        except Exception:  # noqa: BLE001
            pass
        for stage in (stage_robots, stage_api, stage_html, stage_official):
            stage(c)
    Path(out).write_text("\n".join(OUT), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "probe_olx.txt"))
