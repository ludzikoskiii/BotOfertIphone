"""Ceny referencyjne — sklepy z odnowionymi iPhone'ami (tylko do wyceny odsprzedaży, nie oferty do kupna).

Sprawdzone we wrześniu 2026 (``scripts/probe_portals.py``):

* **Refurbed** (``refurbed.pl/p/iphone-13/``) — strona modelu ma dane strukturalne (JSON-LD ``ProductGroup``)
  z ceną każdego wariantu (kolor × pamięć). Program bierze najniższą cenę dla pamięci. Pobieranie raz dziennie,
  ≥ 10 s między zapytaniami (``Crawl-delay: 10`` z robots.txt), tylko najczęstsze modele z Twojej bazy;
* **Swappie** i **Back Market** — odpowiadają „403” (ochrona Cloudflare przed automatami). Program tego nie
  obchodzi; ceny z tych sklepów możesz wpisać ręcznie w Ustawieniach → Portale.

Wartość odsprzedaży = cena referencyjna × współczynnik (domyślnie 80%), mieszana z medianą ogłoszeń
według wagi z ustawień. Brak ceny referencyjnej nigdy nie blokuje wyceny — wtedy zostaje sama mediana,
a gdy sklep chwilowo nie odpowiada — ostatnia zapisana cena (z datą).
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from selectolax.parser import HTMLParser

from ..core.catalog import format_storage
from ..core.models import MarketEstimate
from ..core.settings import Settings
from ..net.http import HostRateLimiter, HttpClient, HttpError
from ..storage.repositories import SettingsRepository

log = logging.getLogger(__name__)

REFURBED_URL = "https://www.refurbed.pl/p/{slug}/"
CRAWL_DELAY_S = 10.0
REFRESH_KEY = "reference_refreshed_at"
REFRESH_EVERY = timedelta(hours=24)
_SIZE = re.compile(r"(\d+)\s*(GB|TB)", re.I)
SOURCE_NAMES = {"refurbed": "Refurbed", "manual": "wpisana ręcznie"}


@dataclass
class ReferencePrice:
    source: str
    model: str
    storage_gb: int
    price: float
    url: str | None
    fetched_at: datetime


def slug(model: str) -> str:
    """„iPhone 13 Pro Max” → „iphone-13-pro-max”; „iPhone SE (2020)” → „iphone-se-2020”."""
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def parse_refurbed(html: str) -> dict[int, float]:
    """Najniższa cena dla każdej pamięci z JSON-LD strony modelu."""
    out: dict[int, float] = {}
    for node in HTMLParser(html).css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except json.JSONDecodeError:
            continue
        for group in data if isinstance(data, list) else [data]:
            if not isinstance(group, dict) or group.get("@type") != "ProductGroup":
                continue
            for v in group.get("hasVariant") or []:
                m = _SIZE.search(str(v.get("size") or v.get("name") or ""))
                offer = v.get("offers") or {}
                if isinstance(offer, list):
                    offer = offer[0] if offer else {}
                try:
                    price = float(offer.get("price"))
                except (TypeError, ValueError):
                    continue
                if not m or str(offer.get("priceCurrency", "PLN")).upper() != "PLN":
                    continue
                gb = int(m.group(1)) * (1024 if m.group(2).upper() == "TB" else 1)
                out[gb] = min(price, out.get(gb, price))
    return out


class ReferenceRepository:
    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    def save(self, source: str, model: str, storage_gb: int, price: float, url: str | None,
             when: datetime | None = None) -> None:
        self.conn.execute(
            "INSERT INTO reference_prices (source, model, storage_gb, price, url, fetched_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source, model, storage_gb) DO UPDATE SET price = excluded.price, url = excluded.url, "
            "fetched_at = excluded.fetched_at",
            (source, model, storage_gb, price, url, (when or datetime.now(UTC)).isoformat()))

    def all(self) -> dict[tuple[str, int], list[ReferencePrice]]:
        out: dict[tuple[str, int], list[ReferencePrice]] = {}
        for r in self.conn.execute("SELECT * FROM reference_prices"):
            ref = ReferencePrice(r["source"], r["model"], int(r["storage_gb"]), float(r["price"]), r["url"],
                                 datetime.fromisoformat(r["fetched_at"]))
            out.setdefault((ref.model, ref.storage_gb), []).append(ref)
        return out


def lookup(model: str | None, storage_gb: int | None, stored: dict[tuple[str, int], list[ReferencePrice]],
           settings: Settings) -> ReferencePrice | None:
    """Cena referencyjna dla modelu i pamięci: ręczna ma pierwszeństwo, potem najnowsza pobrana."""
    if not model or not storage_gb:
        return None
    manual = settings.reference_manual.get(f"{model}|{storage_gb}")
    if manual:
        return ReferencePrice("manual", model, storage_gb, float(manual), None, datetime.now(UTC))
    refs = stored.get((model, storage_gb)) or []
    return max(refs, key=lambda r: r.fetched_at) if refs else None


def blend(market: MarketEstimate, ref: ReferencePrice | None, settings: Settings) -> MarketEstimate:
    """Wartość rynkowa z uwzględnieniem ceny referencyjnej (tylko klasa „używany sprawny”)."""
    if ref is None or not settings.reference_enabled or market.method == "wartość ręczna z ustawień":
        return market
    ref_value = round(ref.price * settings.reference_factor, 2)
    when = ref.fetched_at.astimezone().strftime("%d.%m.%Y")
    shop = SOURCE_NAMES.get(ref.source, ref.source)
    note = (f"{shop}: {ref.price:,.0f} zł ({format_storage(ref.storage_gb)}, {when}) × "
            f"{settings.reference_factor:.0%} = {ref_value:,.0f} zł").replace(",", " ")
    if market.value is None:
        return MarketEstimate(ref_value, market.sample_size, f"cena referencyjna ({shop}) × {settings.reference_factor:.0%}",
                              "średnia", market.raw_median, note)
    w = min(max(settings.reference_weight, 0.0), 1.0)
    value = round(w * ref_value + (1 - w) * market.value, 2)
    confidence = market.confidence if market.confidence in ("wysoka", "średnia") else "średnia"
    method = f"{market.method} · {w:.0%} z ceny referencyjnej"
    return MarketEstimate(value, market.sample_size, method, confidence, market.raw_median, note)


# ------------------------------------------------------------ pobieranie ---

def due(conn: sqlite3.Connection, now: datetime | None = None) -> bool:
    value = SettingsRepository(conn).get_value(REFRESH_KEY)
    if not value:
        return True
    return (now or datetime.now(UTC)) - datetime.fromisoformat(value) >= REFRESH_EVERY


def popular_models(conn: sqlite3.Connection, limit: int) -> list[str]:
    rows = conn.execute("SELECT model, COUNT(*) AS n FROM offers WHERE model IS NOT NULL AND is_active = 1 "
                        "GROUP BY model ORDER BY n DESC LIMIT ?", (limit,))
    return [r["model"] for r in rows]


async def refresh(conn: sqlite3.Connection, settings: Settings, *, http: HttpClient | None = None,
                  force: bool = False) -> dict[str, int]:
    """Raz dziennie: ceny Refurbed dla najczęstszych modeli. Zwraca {model: liczba pojemności}.

    Błąd albo blokada sklepu → przerwanie bez szkody (zostają ostatnie zapisane ceny)."""
    if not settings.reference_enabled or not (force or due(conn)):
        return {}
    own = http is None
    http = http or HttpClient(HostRateLimiter(CRAWL_DELAY_S))
    repo = ReferenceRepository(conn)
    found: dict[str, int] = {}
    try:
        for model in popular_models(conn, settings.reference_max_models):
            url = REFURBED_URL.format(slug=slug(model))
            try:
                html = await http.get_text(url, use_cache=False)
            except HttpError as e:
                if e.status in (403, 429) or e.blocked or e.network:
                    log.warning("Refurbed: %s — przerywam (zostają ostatnie zapisane ceny)", e)
                    break
                continue  # np. 404 — sklep nie ma tego modelu
            prices = parse_refurbed(html)
            for gb, price in prices.items():
                repo.save("refurbed", model, gb, price, url)
            found[model] = len(prices)
        SettingsRepository(conn).set_value(REFRESH_KEY, datetime.now(UTC).isoformat())
    finally:
        if own:
            await http.aclose()
    log.info("Ceny referencyjne: %s", found)
    return found


def refresh_in_background(db_path, settings: Settings, force: bool = False) -> dict[str, int]:
    """Dla wątku roboczego: własne połączenie z bazą."""
    import asyncio

    from ..storage.db import connect

    conn = connect(db_path)
    try:
        return asyncio.run(refresh(conn, settings, force=force))
    finally:
        conn.close()
