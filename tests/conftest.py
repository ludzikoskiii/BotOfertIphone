from __future__ import annotations

import itertools

import pytest

from phonebot.core.models import Offer, RawOffer
from phonebot.core.normalizer import parse_offer
from phonebot.core.settings import Settings
from phonebot.storage.db import open_database

_ids = itertools.count(1)


def make_raw(title: str, price: float = 1000.0, description: str = "", **kw) -> RawOffer:
    kw.setdefault("photos", ["https://example.com/1.jpg"])
    kw.setdefault("shipping_available", True)
    return RawOffer(
        source=kw.pop("source", "test"),
        source_id=kw.pop("source_id", str(next(_ids))),
        url=kw.pop("url", "https://example.com/oferta"),
        title=title,
        price=price,
        description=description,
        **kw,
    )


def make_offer(title: str, price: float = 1000.0, description: str = "", **kw) -> Offer:
    raw = make_raw(title, price, description, **kw)
    return Offer(raw=raw, parsed=parse_offer(raw))


@pytest.fixture
def settings() -> Settings:
    return Settings()


@pytest.fixture
def conn():
    c = open_database(":memory:")
    yield c
    c.close()
