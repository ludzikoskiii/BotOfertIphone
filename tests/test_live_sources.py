"""Testy kanarkowe NA ŻYWO: czy każdy portal nadal zwraca oferty.

Domyślnie pomijane (wymagają internetu). Uruchom:  python -m pytest -m live
Codziennie uruchamia je workflow GitHub Actions „live-sources”.

- zmiana formatu / API, 0 ofert, błąd adaptera → test CZERWONY (adapter do naprawy),
- blokada portalu (403/captcha) → oznaczone jako znany problem (xfail) z opisem,
  bo zależy od sieci, z której łączy się serwer testowy — nie od kodu adaptera.
"""
import asyncio

import pytest

from phonebot.core.settings import Settings
from phonebot.diagnose import run_adapter
from phonebot.sources import REGISTRY


@pytest.mark.live
@pytest.mark.parametrize("key", sorted(REGISTRY))
def test_source_returns_offers(key):
    import os

    settings = Settings()
    if REGISTRY[key].requires_keys:  # Allegro / eBay — tylko gdy w CI są klucze (sekrety repozytorium)
        prefix = key.upper()
        setattr(settings, f"{key}_client_id", os.environ.get(f"{prefix}_CLIENT_ID", ""))
        setattr(settings, f"{key}_client_secret", os.environ.get(f"{prefix}_CLIENT_SECRET", ""))
        if not REGISTRY[key].configured(settings):
            pytest.skip(f"{key}: wymaga kluczy API ({prefix}_CLIENT_ID / {prefix}_CLIENT_SECRET)")
    res = asyncio.run(run_adapter(key, settings))
    last = res.trace[-1] if res.trace else {}
    details = (f"{key}: {res.stage}; błąd: {res.error}; ofert: {res.raw_offers}/{res.accepted}; "
               f"ostatnie zapytanie: {last.get('status')} {last.get('url')}")
    if not res.ok and res.stage.startswith("blokada portalu"):
        pytest.xfail(f"portal blokuje automatyczne pobieranie z tej sieci — {details}")
    assert res.ok, details


@pytest.mark.live
def test_refurbed_reference_prices(tmp_path):
    """Ceny referencyjne Refurbed: strona modelu nadal ma ceny w danych strukturalnych."""
    from phonebot.core.models import RawOffer
    from phonebot.core.normalizer import parse_offer
    from phonebot.services.reference_prices import ReferenceRepository, refresh
    from phonebot.storage.db import open_database
    from phonebot.storage.repositories import OfferRepository

    conn = open_database(tmp_path / "ref.sqlite3")
    raw = RawOffer("vinted", "1", "https://x", "iPhone 13 128GB", 1200, photos=["x"])
    OfferRepository(conn).upsert(raw, parse_offer(raw))
    found = asyncio.run(refresh(conn, Settings(), force=True))
    prices = ReferenceRepository(conn).all()
    conn.close()
    assert found.get("iPhone 13", 0) >= 1, f"Refurbed nie zwrócił cen: {found}"
    assert 300 < prices[("iPhone 13", 128)][0].price < 5000
