from __future__ import annotations

import itertools

import pytest

from phonebot.core.models import Offer, RawOffer
from phonebot.core.normalizer import parse_offer
from phonebot.core.settings import Settings
from phonebot.storage.db import open_database

_ids = itertools.count(1)


@pytest.fixture(autouse=True, scope="session")
def _isolated_data_dir(tmp_path_factory):
    """Testy nie mogą pisać do prawdziwego katalogu danych (modele AI, baza, miniatury)."""
    import os

    os.environ["PHONEBOT_HOME"] = str(tmp_path_factory.mktemp("phonebot_home"))
    yield


@pytest.fixture(autouse=True)
def _no_internet(request, monkeypatch):
    """Testy nie łączą się z internetem (poza oznaczonymi ``live``): połączenie z innym adresem niż ten komputer
    kończy się błędem sieci, tak samo na każdym komputerze i w GitHub Actions. Makiety (MockTransport) działają."""
    if request.node.get_closest_marker("live"):
        return
    import httpx

    real = httpx.HTTPTransport.handle_request

    def handle(self, req):
        if req.url.host not in ("127.0.0.1", "localhost", "::1"):
            raise httpx.ConnectError(f"testy bez internetu: {req.url.host}", request=req)
        return real(self, req)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", handle)


@pytest.fixture(autouse=True)
def _no_telegram_polling(monkeypatch):
    """Okno programu w testach nie odpytuje prawdziwego Telegrama: wątek komend bota nie startuje."""
    from phonebot.services.telegram_bot import BotThread

    monkeypatch.setattr(BotThread, "start", lambda self: setattr(self, "started", True))


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
