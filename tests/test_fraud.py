"""Zadanie 7: wykrywanie oszustw — każdy sygnał, poziomy, skutki, zdjęcia, czarna lista, interfejs."""
from __future__ import annotations

import io
import os
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from PIL import Image, ImageDraw

from phonebot.core.fraud import (
    FraudConfig,
    FraudContext,
    SellerStats,
    assess,
    desc_hash,
    owner_key,
    phone_numbers,
)
from phonebot.core.models import RawOffer, Verdict
from phonebot.core.normalizer import parse_offer
from phonebot.core.selection import SelectionCriteria, auto_match
from phonebot.core.settings import Settings
from phonebot.services.fraud_service import apply_risk
from phonebot.services.photo_hash import analyze, dhash, hash_pending, looks_like_stock
from phonebot.storage.db import open_database
from phonebot.storage.repositories import BlacklistRepository, OfferRepository, RejectedRepository

from .conftest import make_offer
from .test_sorting import _val

CFG = FraudConfig()
MARKET = 2000.0


def keys(offer, market=MARKET, ctx=None, cfg=CFG):
    return {s.key for s in assess(offer, market, ctx or FraudContext(), cfg).signals}


def offer(desc="", price=1800.0, **kw):
    kw.setdefault("source", "vinted")
    kw.setdefault("params", {"seller_id": "7", "seller": "jan"})
    return make_offer("iPhone 13 128GB", price, desc, **kw)


# ------------------------------------------------------------- sprzedający ---

def test_seller_signals():
    o = offer()
    ctx = FraudContext(sellers={("vinted", "7"): SellerStats(created_at=datetime.now(UTC) - timedelta(days=5),
                                                             reviews=0)},
                       expensive_by_seller={("vinted", "7"): 4})
    assert {"new_account", "no_reviews", "many_expensive_new"} <= keys(o, ctx=ctx)
    ctx.sellers[("vinted", "7")] = SellerStats(created_at=datetime.now(UTC) - timedelta(days=900), reviews=40,
                                               positive_pct=70)
    got = keys(o, ctx=ctx)
    assert "negative_reviews" in got and "new_account" not in got and "many_expensive_new" not in got
    ebay = offer(source="ebay", params={"seller_id": "s", "seller_feedback": "0", "seller_positive_pct": "0"})
    assert "no_reviews" in keys(ebay)
    assert not keys(offer())  # brak danych o sprzedającym — sygnały nie działają (nie zgadujemy)


# ------------------------------------------------------------------ tekst ---

@pytest.mark.parametrize("desc, expected", [
    ("Kontakt tylko na WhatsApp", {"contact_outside"}),
    ("pisz na jan.kowalski(at)gmail.com", {"contact_outside"}),
    ("Dzwoń 600 123 456", {"contact_outside"}),
    ("Call +44 7911 123456", {"contact_outside", "foreign_phone"}),
    ("Płatność BLIK na telefon przed wysyłką", {"prepayment"}),
    ("Wyślę link do płatności", {"prepayment"}),
    ("Bez przedpłaty, odbiór osobisty", set()),
])
def test_text_signals(desc, expected):
    assert keys(offer(desc)) == expected


def test_shipping_only_and_sealed_only_when_cheap():
    assert "shipping_only_cheap" not in keys(offer("Tylko wysyłka", 1800))
    assert {"shipping_only_cheap", "very_cheap", "combo"} <= keys(offer("Tylko wysyłka", 900))
    assert "sealed_gift_cheap" not in keys(offer("Nowy, zafoliowany", 1900))
    assert "sealed_gift_cheap" in keys(offer("Nowy, zafoliowany, nietrafiony prezent", 1400))


def test_copied_description():
    text = "Sprzedam iPhone 13 w idealnym stanie, bateria 100%, komplet, pudełko, ładowarka, faktura, gwarancja."
    mine = offer(text)
    ctx = FraudContext(descriptions={desc_hash(text): {owner_key(mine)}})
    assert "copied_description" not in keys(mine, ctx=ctx)  # tylko ten sam sprzedający
    ctx.descriptions[desc_hash(text)].add("allegro_lokalnie:@krakow")
    assert "copied_description" in keys(mine, ctx=ctx)
    assert desc_hash("stan dobry") is None  # krótkie opisy się powtarzają — nie liczymy


# ---------------------------------------------------------------- zdjęcia ---

def test_photo_signals():
    o = offer()
    key = (o.raw.source, o.raw.source_id)
    ctx = FraudContext(photos={key: (0xF0F0F0F0F0F0F0F0, False), ("lento", "9"): (0xF0F0F0F0F0F0F0F1, False)},
                       photo_owner={key: owner_key(o), ("lento", "9"): "lento:@gdansk"})
    assert "duplicate_photo" in keys(o, ctx=ctx)
    ctx.photo_owner[("lento", "9")] = owner_key(o)  # to samo zdjęcie tego samego sprzedającego — w porządku
    assert "duplicate_photo" not in keys(o, ctx=ctx)
    ctx.photos[key] = (0x1, True)
    assert "stock_photo" in keys(o, ctx=ctx)
    assert "no_real_photos" in keys(offer(photos=[]))


def _photo(stock: bool, shift: int = 0, size=(320, 320)) -> bytes:
    img = Image.new("RGB", size, "white" if stock else (90, 70, 50))
    d = ImageDraw.Draw(img)
    if stock:
        d.rounded_rectangle((110, 40, 210, 280), radius=20, fill="black")
    else:
        for i in range(0, 320, 16):
            d.line((i + shift, 0, 320 - i, 320), fill=(200, 180 - i // 3, 60), width=5)
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue()


def test_dhash_and_stock_detection():
    a, stock_a = analyze(_photo(False))
    big = Image.open(io.BytesIO(_photo(False))).resize((640, 640))  # to samo zdjęcie w innym rozmiarze
    buf = io.BytesIO()
    big.save(buf, "JPEG", quality=70)
    b, _ = analyze(buf.getvalue())
    c, _ = analyze(_photo(False, shift=40))
    assert (a ^ b).bit_count() <= 4 and (a ^ c).bit_count() > 8
    assert looks_like_stock(Image.open(io.BytesIO(_photo(True)))) and not stock_a
    assert isinstance(dhash(Image.new("L", (10, 10))), int)


def test_hash_pending_end_to_end(tmp_path):
    from phonebot.services.evaluator import Evaluator

    conn = open_database(tmp_path / "f.sqlite3")
    repo = OfferRepository(conn)
    for sid, city, seller in (("1", "Nowy Targ", "ala"), ("2", "Gdańsk", "ola")):
        raw = RawOffer("allegro_lokalnie", sid, "https://x", "iPhone 13 128GB", 1500, city=city,
                       photos=["https://img.test/same.jpg"], params={"seller": seller})
        repo.upsert(raw, parse_offer(raw))
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, content=_photo(False))))
    assert hash_pending(conn, Settings(), client=client, sleep=lambda s: None) == 2
    assert hash_pending(conn, Settings(), client=client, sleep=lambda s: None) == 0  # policzone raz, po ID
    val = Evaluator(conn, Settings()).evaluate(repo.list()[0])
    assert "duplicate_photo" in {s.key for s in val.risk.signals}
    conn.close()


# ---------------------------------------------------------- poziomy, skutki ---

def test_levels_weights_and_effects():
    cheap_scam = offer("Tylko wysyłka, płatność BLIK z góry, pisz na WhatsApp +44 7911 123456", 800)
    risk = assess(cheap_scam, MARKET, FraudContext(), CFG)
    assert risk.level == "high" and risk.score >= CFG.high_threshold
    val = _val(Verdict.BUY, 900)
    apply_risk(val, risk, Settings())
    assert val.verdict is Verdict.SKIP and val.reasons[0].startswith("MOŻLIWE OSZUSTWO")
    assert not auto_match(cheap_scam, val, SelectionCriteria(verdicts=[]))  # nie do „Wybrane” / Telegrama
    medium = assess(offer("Kontakt WhatsApp, płatność BLIK"), MARKET, FraudContext(), CFG)
    assert medium.level == "medium"
    val = _val(Verdict.BUY, 500)
    apply_risk(val, medium, Settings())
    assert val.verdict is Verdict.VERIFY
    low_cfg = FraudConfig(weights={"prepayment": 0, "contact_outside": 0})
    assert assess(offer("Kontakt WhatsApp, płatność BLIK"), MARKET, FraudContext(), low_cfg).level == "low"
    assert assess(cheap_scam, MARKET, FraudContext(), FraudConfig(enabled=False)).score == 0


def test_phone_numbers():
    assert set(phone_numbers("tel 600-123-456, +48 512 345 678, +44 7911 123456, 128 256")) == {
        "447911123456", "48512345678", "48600123456"}


# ----------------------------------------------------------- czarna lista ---

def test_blacklist_across_portals_and_restore(tmp_path):
    from phonebot.services.offer_guard import OfferGuard

    conn = open_database(tmp_path / "b.sqlite3")
    repo = OfferRepository(conn)
    offers = [RawOffer("vinted", "v1", "https://v", "iPhone 13 128GB", 1500, params={"seller": "Szybki_Handel"},
                       photos=["x"]),
              RawOffer("allegro", "a1", "https://a", "iPhone 12 64GB", 900, params={"seller": "szybki_handel"},
                       photos=["x"]),
              RawOffer("lento", "l1", "https://l", "iPhone 11", 600, description="dzwoń 600 123 456", photos=["x"]),
              RawOffer("lento", "l2", "https://l", "iPhone 14", 2600, description="Inny sprzedający", photos=["x"])]
    for raw in offers:
        repo.upsert(raw, parse_offer(raw))
    bl = BlacklistRepository(conn)
    bl.add(RawOffer("vinted", "v1", "https://v", "iPhone 13", 1500,
                    description="kontakt 600 123 456", params={"seller": "Szybki_Handel"}))
    moved = OfferGuard(conn, Settings()).refilter_stored(force=True)
    assert moved == 3
    left = {o.raw.source_id for o in repo.list()}
    assert left == {"l2"}
    reasons = {r.source_id: r.reason for r in RejectedRepository(conn).list()}
    assert "login" in reasons["a1"] and "numer telefonu" in reasons["l1"]
    for entry in bl.list():
        bl.remove(entry.id)
    guard = OfferGuard(conn, Settings())
    guard.refilter_stored()
    assert guard.restored == 3 and {o.raw.source_id for o in repo.list()} == {"v1", "a1", "l1", "l2"}
    conn.close()


def test_vinted_profile_feedback():
    from phonebot.sources.vinted import _feedback

    assert _feedback({"feedback_count": 20, "positive_feedback_count": 15, "negative_feedback_count": 5,
                      "created_at": "2026-09-20T10:00:00+02:00"}) == {
        "reviews": 20, "positive_pct": 75.0, "negative": 5, "created_at": "2026-09-20T10:00:00+02:00"}
    assert _feedback({}) == {}


# ------------------------------------------------------------------- okno ---

def test_window_risk_column_filter_details_and_block(tmp_path):
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    from phonebot.core.view_filter import ViewFilter
    from phonebot.ui.main_window import MainWindow
    from phonebot.ui.table_model import Col

    from .conftest import make_raw
    from .sample_data import build_sample_db

    conn, _ = build_sample_db(tmp_path / "w.sqlite3")
    raw = make_raw("iPhone 13 128GB", 700, "Tylko wysyłka. Płatność BLIK z góry, kontakt WhatsApp +44 7911 123456",
                   source="sprzedajemy", source_id="scam1", params={"seller": "okazja24"})
    OfferRepository(conn).upsert(raw, parse_offer(raw))
    win = MainWindow(conn, tmp_path / "w.sqlite3", thumbs_dir=tmp_path)
    try:
        scam = next((o, v) for o, v in win.model.rows() if o.raw.source_id == "scam1")
        assert scam[1].risk.level == "high"
        row = win.proxy.mapFromSource(win.model.index(win.model.row_of(scam[0].id), 0)).row()
        assert win.proxy.index(row, Col.VERDICT).data() == "MOŻLIWE OSZUSTWO"
        assert win.proxy.index(row, Col.RISK).data().startswith("⛔ WYSOKIE")
        assert "Przedpłata" in win.proxy.index(row, Col.RISK).data(3)  # podpowiedź z powodami
        win.filters.set_filter(ViewFilter(risk_levels=["high"]))
        assert win.proxy.rowCount() == 1
        win.filters.set_filter(ViewFilter())
        win.details.set_offer(*scam)
        text = win.details.browser.toPlainText()
        assert "MOŻLIWE OSZUSTWO" in text and "Jak kupić bezpiecznie" in text
        win.list_tabs.setCurrentIndex(1)
        assert scam[0].id not in [win._row_at(win.proxy.index(r, 0))[0].id for r in range(win.proxy.rowCount())]
        assert win.block_seller(scam[0].id) == 1
        assert all(o.raw.source_id != "scam1" for o, _ in win.model.rows())
        dialog = win.open_settings()
        assert dialog.blacklist_table.rowCount() == 1 and "okazja24" in dialog.blacklist_table.item(0, 1).text()
        dialog.blacklist_table.selectRow(0)
        dialog._blacklist_remove()
        dialog.accept()
        app.processEvents()
        assert any(o.raw.source_id == "scam1" for o, _ in win.model.rows())  # usunięty z listy — wraca
    finally:
        win._quitting = True
        win.close()
        conn.close()
        app.processEvents()


def test_marks_in_telegram_and_mobile():
    from phonebot.services.telegram_queue import format_offer
    from phonebot.web.pages import card, details_page

    o = offer("Kontakt WhatsApp, płatność BLIK", 1800)
    o.id = 5
    val = _val(Verdict.BUY, 500)
    apply_risk(val, assess(o, MARKET, FraudContext(), CFG), Settings())
    assert "Ryzyko oszustwa: średnie" in format_offer(o, val, Settings())
    assert "ryzyko średnie" in card(o, val)
    scam = offer("Tylko wysyłka, BLIK z góry, WhatsApp +44 7911 123456", 800)
    scam.id = 6
    sval = _val(Verdict.BUY, 900)
    apply_risk(sval, assess(scam, MARKET, FraudContext(), CFG), Settings())
    assert "MOŻLIWE OSZUSTWO" in card(scam, sval)
    page = details_page(scam, sval, Settings(), csrf="x")
    assert "MOŻLIWE OSZUSTWO" in page and "Jak kupić bezpiecznie" in page
