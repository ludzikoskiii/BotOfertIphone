"""Demo etapu 1: wycena przykładowych ofert na tymczasowej bazie.

Uruchom:  python -m phonebot.demo
"""
from __future__ import annotations

import random
import sys

from .core.catalog import format_storage
from .core.models import Mode, RawOffer
from .core.normalizer import parse_offer
from .core.settings import Settings
from .services.evaluator import Evaluator
from .storage.db import open_database
from .storage.repositories import OfferRepository, PartsRepository

SAMPLES = [
    ("iPhone 13 128GB zbity ekran", 850, "Zbity ekran, dotyk działa, Face ID działa. Bateria 88%. Cena do negocjacji."),
    ("iPhone 13 128GB", 1250, "Stan bardzo dobry, bateria 90%."),
    ("iPhone 13 128GB na części", 300, "Sprzedam."),
    ("iPhone 12 Pro 256GB - nie ładuje", 700, "Nie ładuje, pewnie port. Reszta sprawna. Bez blokad iCloud."),
    ("iPhone 12 Pro 256GB", 1500, "Blokada iCloud, nie znam hasła."),
    ("iPhone 14 Pro 128GB zbity tył", 1900, "Zbity tył, przód idealny. Cena ostateczna."),
    ("iPhone 11 64GB", 650, "Bateria 76%, poza tym sprawny."),
]


def _market_data(repo: OfferRepository) -> None:
    rng = random.Random(1)
    base = {("iPhone 13", 128): 1700, ("iPhone 12 Pro", 256): 1650, ("iPhone 14 Pro", 128): 2900,
            ("iPhone 11", 64): 850}
    for (model, gb), price in base.items():
        for i in range(8):
            p = round(price * rng.uniform(0.9, 1.12), -1)
            raw = RawOffer("rynek", f"{model}-{gb}-{i}", "https://example.com", f"{model} {gb}GB", p,
                           description="sprawny", city=f"Miasto {i}", photos=["x"])
            repo.upsert(raw, parse_offer(raw))


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    conn = open_database(":memory:")
    PartsRepository(conn).seed_defaults_if_empty()
    repo = OfferRepository(conn)
    _market_data(repo)
    ids = []
    for i, (title, price, desc) in enumerate(SAMPLES):
        raw = RawOffer("olx", f"demo{i}", "https://olx.pl/...", title, price, description=desc,
                       photos=["x"], shipping_available=True)
        ids.append(repo.upsert(raw, parse_offer(raw)).offer_id)

    settings = Settings()
    ev = Evaluator(conn, settings)
    for mode in (Mode.REPAIR, Mode.RESELL):
        print(f"\n=== Tryb: {mode.label} ===")
        print(f"{'Oferta':<34}{'Cena':>7}{'Wart.':>7}{'Zysk':>7}{'Max':>7}  {'Werdykt':<9}{'Ocena':>6}  Kolor")
        for oid in ids:
            offer = repo.get(oid)
            v = ev.evaluate(offer, mode)
            p = offer.parsed
            name = f"{p.model} {format_storage(p.storage_gb)}"
            value = f"{v.market.value:.0f}" if v.market.value else "-"
            profit = f"{v.expected_profit:.0f}" if v.expected_profit is not None else "-"
            mx = f"{v.max_buy_price:.0f}" if v.max_buy_price is not None else "-"
            print(f"{name:<34}{offer.price:>7.0f}{value:>7}{profit:>7}{mx:>7}  "
                  f"{v.verdict.value:<9}{v.score:>6}  {v.color.value}")
            print(f"    {v.reasons[0]}")
            if v.negotiation.opening_price:
                print(f"    Negocjacje: {v.negotiation.note}")
            for f in v.flags:
                print(f"    ⚑ {f.label}")


if __name__ == "__main__":
    main()
