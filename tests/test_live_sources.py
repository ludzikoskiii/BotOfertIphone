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
    res = asyncio.run(run_adapter(key, Settings()))
    last = res.trace[-1] if res.trace else {}
    details = (f"{key}: {res.stage}; błąd: {res.error}; ofert: {res.raw_offers}/{res.accepted}; "
               f"ostatnie zapytanie: {last.get('status')} {last.get('url')}")
    if not res.ok and res.stage.startswith("blokada portalu"):
        pytest.xfail(f"portal blokuje automatyczne pobieranie z tej sieci — {details}")
    assert res.ok, details
