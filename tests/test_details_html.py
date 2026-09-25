from datetime import datetime, timezone

from phonebot.core.models import MarketEstimate, Mode, OfferStatus
from phonebot.core.parts import PartsCatalog, default_parts
from phonebot.core.settings import Settings
from phonebot.core.valuation import evaluate
from phonebot.ui.details_html import build_details_html, zl

from .conftest import make_offer


def render(title, price, description="", mode=Mode.REPAIR, **kw):
    s = Settings()
    offer = make_offer(title, price, description, **kw)
    val = evaluate(offer, MarketEstimate(2000, 12, "mediana 12 ofert (128 GB)", "wysoka", 2222), PartsCatalog(default_parts()),
                   s, mode)
    return offer, val, build_details_html(offer, val, s)


def test_zl():
    assert zl(1234.4) == "1 234 zł"
    assert zl(-50) == "−50 zł"
    assert zl(50, sign=True) == "+50 zł"
    assert zl(None) == "—"


def test_full_breakdown_for_repair_offer():
    offer, val, html = render("iPhone 13 128GB zbity ekran", 900, "Bateria 85%. Do negocjacji.")
    assert "KUPUJ" in html
    assert "Wyświetlacz / szyba" in html and "−300 zł" in html  # część z tabeli
    assert "Wysyłka części" in html and "Wysyłka do Ciebie" in html
    assert "mediana 12 ofert" in html and "mediana 2 222 zł × korekta" in html
    assert f"+{val.expected_profit:,.0f} zł".replace(",", " ") in html
    assert "Maksymalna cena zakupu" in html and "1 330 zł" in html
    assert "Kondycja baterii" in html and "85%" in html


def test_negotiation_section():
    _, val, html = render("iPhone 13 128GB zbity ekran", 1450, "Cena do negocjacji")
    assert "NEGOCJUJ" in html
    assert "Proponowana cena otwierająca" in html
    assert zl(val.negotiation.opening_price) in html and zl(val.negotiation.max_price) in html


def test_flags_listed_with_penalty():
    _, _, html = render("iPhone 13 128GB", 900, "Blokada iCloud", photos=[])
    assert "Blokada iCloud / aktywacji" in html and "poważna" in html and "−60 pkt" in html
    assert "Brak zdjęć" in html and "ostrzeżenie" in html


def test_escapes_html_from_listing():
    _, _, html = render("iPhone 13 <script>alert(1)</script>", 900, "<b>opis</b>")
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "&lt;b&gt;opis" in html


def test_price_history_and_status():
    s = Settings()
    offer = make_offer("iPhone 13 128GB", 1000)
    offer.status = OfferStatus.WATCHED
    val = evaluate(offer, MarketEstimate(2000, 12, "m", "wysoka", 2000), PartsCatalog(default_parts()), s, Mode.RESELL)
    history = [(datetime(2026, 9, 1, tzinfo=timezone.utc), 1100.0), (datetime(2026, 9, 5, tzinfo=timezone.utc), 1000.0)]
    html = build_details_html(offer, val, s, history)
    assert "Historia ceny" in html and "1 100 zł" in html
    assert "obserwowana" in html


def test_no_market_value():
    s = Settings()
    offer = make_offer("iPhone 13 128GB", 1000)
    val = evaluate(offer, MarketEstimate(None, 0, "za mało danych rynkowych", "brak"), PartsCatalog(default_parts()), s,
                   Mode.RESELL)
    html = build_details_html(offer, val, s)
    assert "Brak wyceny rynkowej" in html and "ODPUŚĆ" in html
