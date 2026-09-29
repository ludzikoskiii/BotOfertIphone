"""Zadanie 1: transakcje, realny zysk, „Kupiłem”, samodoskonalenie wyceny (poprawki model + usterki)."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from phonebot.core.inventory import InventoryConfig, Lot
from phonebot.core.models import Defect, Mode
from phonebot.core.parts import PartsCatalog, default_parts
from phonebot.core.settings import Settings
from phonebot.core.transactions import (
    Corrections,
    CostEntry,
    LearningConfig,
    Snapshot,
    Transaction,
    UsedPart,
    corrections,
    describe_key,
    from_offer,
    insights,
    key_for,
    summarize,
    tx_count,
)
from phonebot.core.valuation import evaluate
from phonebot.storage.db import open_database
from phonebot.storage.repositories import InventoryRepository, OfferRepository, PartsRepository, TransactionRepository

from .conftest import make_offer, make_raw
from .test_stage3 import MARKET

D = datetime(2026, 9, 1, 12, tzinfo=UTC)
CFG = LearningConfig()
SCREEN = ("iPhone 12 128GB zbity ekran", 900, "zbity ekran, reszta sprawna")


def sold_tx(repair_actual=236.0, repair_expected=200.0, resale=1500.0, sell=1500.0, minutes=45, expected_min=45,
            days=10, status="sold", model="iPhone 12", defects=(Defect.SCREEN,)) -> Transaction:
    snap = Snapshot("KUPUJ", resale_value=resale, repair_cost=repair_expected, profit=300, repair_minutes=expected_min,
                    handling_minutes=80, sell_days=14)
    return Transaction(model=model, defects=list(defects), status=status, bought_at=D, buy_price=900,
                       costs=[CostEntry("repair", "szkło + klej", repair_actual), CostEntry("buy", "wysyłka", 15)],
                       repair_minutes=minutes, handling_minutes=80, listed_at=D + timedelta(days=2),
                       sold_at=D + timedelta(days=2 + days) if status == "sold" else None,
                       sell_price=sell if status == "sold" else None, snapshot=snap)


# ------------------------------------------------------------ wyliczenia ---

def test_transaction_money_time_and_days():
    tx = sold_tx(repair_actual=100, sell=1400, minutes=40)
    tx.parts = [UsedPart(Defect.SCREEN, 1, 180.0)]
    assert tx.parts_cost == 180 and tx.other_costs == 115 and tx.repair_cost == 280
    assert tx.total_cost == 900 + 180 + 115 and tx.profit == 1400 - 1195
    assert tx.minutes == 120 and tx.profit_per_hour == pytest.approx(205 / 2)
    assert tx.sell_days == 10
    tx.listed_at = None
    assert tx.sell_days == 12  # bez daty wystawienia — od zakupu
    unsold = sold_tx(status="listed")
    assert unsold.profit is None and unsold.profit_per_hour is None and unsold.sell_days is None
    s = summarize([tx, unsold])
    assert s.count == 2 and s.sold == 1 and s.profit == 205 and s.frozen == unsold.total_cost


def test_describe_key_and_plural():
    assert describe_key(key_for("iPhone 12", [Defect.SCREEN])) == "iPhone 12 ze zbitym ekranem"
    assert describe_key(key_for("iPhone 11", [])) == "iPhone 11 bez usterek"
    assert describe_key(key_for("iPhone 13", [Defect.SCREEN, Defect.BATTERY])) == \
        "iPhone 13 ze słabą baterią i ze zbitym ekranem"
    assert [tx_count(n) for n in (1, 2, 5, 12, 22)] == ["1 transakcja", "2 transakcje", "5 transakcji",
                                                        "12 transakcji", "22 transakcje"]


# --------------------------------------------------------------- poprawki ---

def test_corrections_need_min_transactions_and_grow_gradually():
    two = corrections([sold_tx(), sold_tx()], CFG)[key_for("iPhone 12", [Defect.SCREEN])]
    assert not two.active and two.factors["repair_cost"].weight == 0
    three = corrections([sold_tx()] * 3, CFG)[key_for("iPhone 12", [Defect.SCREEN])]
    six = corrections([sold_tx()] * 6, CFG)[key_for("iPhone 12", [Defect.SCREEN])]
    f3, f6 = three.factor("repair_cost"), six.factor("repair_cost")
    assert f3.ratio == pytest.approx(1.18) and f3.weight == 0.5 and f3.applied == pytest.approx(1.09)
    assert f6.weight == pytest.approx(0.667, abs=0.001) and f6.applied > f3.applied  # więcej transakcji — większa waga


def test_one_unusual_transaction_does_not_break_valuation():
    txs = [sold_tx(), sold_tx(), sold_tx(), sold_tx(repair_actual=2000)]  # jedna pomyłka / pechowa naprawa
    f = corrections(txs, CFG)[key_for("iPhone 12", [Defect.SCREEN])].factor("repair_cost")
    assert f.ratio == pytest.approx(1.18)  # mediana — odstająca wartość nie przestawia poprawki
    wild = corrections([sold_tx(repair_actual=2000)] * 3, CFG)[key_for("iPhone 12", [Defect.SCREEN])]
    assert wild.factor("repair_cost").ratio == 1.5  # przycięte do +50%


def test_insights_in_plain_language():
    txs = [sold_tx() for _ in range(5)] + [sold_tx(model="iPhone 13", repair_actual=200)]
    texts = [i.text for i in insights(corrections(txs, CFG), CFG)]
    assert ("iPhone 12 ze zbitym ekranem: naprawa kosztuje Cię średnio o 18% więcej, niż zakładam "
            "(ok. 236 zł zamiast 200 zł) (5 transakcji). Uwzględniam to w wycenie (waga 62%).") in texts
    assert any(t.startswith("iPhone 12 ze zbitym ekranem: sprzedaż trwa średnio 10 dni (zakładam 14)")
               for t in texts)
    assert any("iPhone 13 ze zbitym ekranem: koszt naprawy zgadza się z wyceną (1 transakcja). Za mało danych"
               in t for t in texts)
    off = LearningConfig(enabled=False)
    assert any("Poprawki są wyłączone — nie uwzględniam." in i.text for i in insights(corrections(txs, off), off))


def test_valuation_applies_corrections_and_can_be_disabled():
    offer = make_offer(*SCREEN)
    txs = [sold_tx(sell=1650, resale=1500, minutes=63, expected_min=45) for _ in range(3)]
    parts = PartsCatalog(default_parts())
    parts.corrections = Corrections(corrections(txs, CFG), CFG)
    base = evaluate(offer, MARKET, PartsCatalog(default_parts()), Settings(), Mode.REPAIR)
    val = evaluate(offer, MARKET, parts, Settings(), Mode.REPAIR)
    item = next(i for i in val.repair_items if i.label.startswith("Poprawka z Twoich transakcji"))
    assert item.amount == pytest.approx(base.repair_cost * 0.09, abs=0.02)  # +18% × waga 50%
    assert val.resale_value == pytest.approx(MARKET.value * 1.05)  # +10% × 50%
    assert next(i for i in val.time_items if i.repair).minutes == 54  # 45 min × (1 + 40% × 50%)
    assert val.baseline == {"repair_cost": base.repair_cost, "repair_minutes": 45, "resale": MARKET.value}
    assert len(val.corrections) == 4 and val.sell_days == pytest.approx(14 * (1 + 0.5 * (10 / 14 - 1)), abs=0.1)
    s = Settings()
    s.learning.enabled = False
    off = evaluate(offer, MARKET, parts, s, Mode.REPAIR)
    assert off.repair_cost == base.repair_cost and off.expected_profit == base.expected_profit and not off.corrections
    other = evaluate(make_offer("iPhone 13 128GB zbity ekran", 900, "zbity ekran"), MARKET, parts, Settings(),
                     Mode.REPAIR)
    assert not other.corrections  # inny model — bez poprawek


# ------------------------------------------------------------ baza i „Kupiłem” ---

@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "t.sqlite3")
    PartsRepository(conn).seed_defaults_if_empty()
    yield conn
    conn.close()


def _offer(db, title=SCREEN[0], price=SCREEN[1], desc=SCREEN[2]):
    from phonebot.core.normalizer import parse_offer

    raw = make_raw(title, price, desc)
    oid = OfferRepository(db).upsert(raw, parse_offer(raw)).offer_id
    return OfferRepository(db).get(oid)


def test_from_offer_snapshot_and_buy_costs(db):
    from phonebot.services.evaluator import Evaluator

    offer = _offer(db)
    val = Evaluator(db, Settings()).evaluate(offer)
    tx = from_offer(offer, val, CFG, D)
    assert tx.model == "iPhone 12" and tx.defects == [Defect.SCREEN] and tx.buy_price == 900
    assert tx.offer_id == offer.id and tx.status == "bought"
    assert [c.label for c in tx.costs] == ["Wysyłka do Ciebie"]  # koszty sprzedaży — dopiero po sprzedaży
    snap = tx.snapshot
    assert snap.verdict == val.verdict.value and snap.profit == val.expected_profit
    assert snap.repair_cost == val.repair_cost and snap.repair_minutes == 45 and snap.handling_minutes == 80
    assert snap.resale_value == val.market.value and snap.sell_days == 14


def test_repository_roundtrip_parts_fifo_atomic_and_delete(db):
    inv = InventoryRepository(db)
    inv.save(Lot(None, Defect.SCREEN, ["iPhone 12"], "replacement", 1, 200.0, D - timedelta(days=9)))
    inv.save(Lot(None, Defect.SCREEN, ["iPhone 12"], "original", 1, 300.0, D))
    repo = TransactionRepository(db)
    tx = sold_tx()
    tx_id = repo.save(tx)
    assert repo.set_parts(tx_id, "iPhone 12", [(Defect.SCREEN, 1)], InventoryConfig(), when=D) == []
    got = repo.get(tx_id)
    assert got.parts == [UsedPart(Defect.SCREEN, 1, 200.0)]  # najstarsza sztuka
    assert got.snapshot.repair_cost == 200 and got.costs[0].kind == "repair" and got.sold_at == tx.sold_at
    # za mało na stanie → nic się nie zmienia (ani zwrot, ani zdjęcie)
    assert repo.set_parts(tx_id, "iPhone 12", [(Defect.SCREEN, 3)], InventoryConfig()) == [Defect.SCREEN]
    assert repo.get(tx_id).parts == [UsedPart(Defect.SCREEN, 1, 200.0)] and sum(lt.qty for lt in inv.lots()) == 1
    assert repo.set_parts(tx_id, "iPhone 12", [(Defect.SCREEN, 2)], InventoryConfig()) == []
    assert sorted(p.unit_price for p in repo.get(tx_id).parts) == [200.0, 300.0]
    assert sum(lt.qty for lt in inv.lots()) == 0
    other = repo.save(sold_tx())
    repo.delete(tx_id, return_parts=True)
    assert repo.get(tx_id) is None and sum(lt.qty for lt in inv.lots()) == 2  # części wróciły
    repo.set_parts(other, "iPhone 12", [(Defect.SCREEN, 1)], InventoryConfig())
    repo.delete(other, return_parts=False)
    assert sum(lt.qty for lt in inv.lots()) == 1 and len(inv.usage()) == 1  # zużyta, historia zostaje


def test_evaluator_learns_from_transactions_and_shows_preview(db):
    from phonebot.services.evaluator import Evaluator

    repo = TransactionRepository(db)
    for _ in range(3):
        repo.save(sold_tx(repair_actual=400, repair_expected=200))  # naprawa 2× droższa niż zakładano
    offer = _offer(db)
    s = Settings(manual_market_values={"iPhone 12|128": 1600.0})
    val = Evaluator(db, s).evaluate(offer)
    assert val.corrections and val.alternative is not None
    assert val.repair_cost > val.alternative.repair_cost and val.expected_profit < val.alternative.expected_profit
    s.learning.enabled = False
    off = Evaluator(db, s).evaluate(_offer(db))
    assert not off.corrections and off.alternative.repair_cost == val.repair_cost  # podgląd „z poprawkami”


# --------------------------------------------------------------------- UI ---

def test_kupilem_dialog_tab_and_details(db, tmp_path):
    from PySide6.QtWidgets import QApplication

    from phonebot.ui.details_html import build_details_html
    from phonebot.ui.main_window import MainWindow
    from phonebot.ui.table_model import BOUGHT_MARK, Col
    from phonebot.ui.transactions_tab import TransactionDialog

    QApplication.instance() or QApplication([])
    InventoryRepository(db).save(Lot(None, Defect.SCREEN, ["iPhone 12"], "replacement", 2, 180.0, D))
    offer = _offer(db)
    win = MainWindow(db, tmp_path / "t.sqlite3", thumbs_dir=tmp_path)
    win.settings.manual_market_values = {"iPhone 12|128": 1600.0}
    win.reload()
    assert [win.main_tabs.tabText(i) for i in range(win.main_tabs.count())][:3] == \
        ["Oferty", "Magazyn części", "Transakcje"]
    row = win.model.row_of(offer.id)
    o, val = win.model.row_at(row)
    assert win.details.bought_btn.text() in ("🛒 Kupiłem", "")  # tekst ustawiany przy wyborze oferty

    opened = []

    def fake_open(tx, *, dialog=None):  # „Kupiłem” → okno z danymi oferty; zapis bez pokazywania okna
        dlg = TransactionDialog(tx, db, win.settings)
        opened.append(dlg)
        assert dlg.parts.rowCount() == 1 and dlg.parts.item(0, 0).text() == Defect.SCREEN.label  # część ze stanu
        dlg.buy_price.setValue(850)  # wynegocjowana cena
        return type(win.transactions_tab).open_dialog(win.transactions_tab, tx, dialog=dlg)

    win.transactions_tab.open_dialog = fake_open
    assert win.record_purchase(offer.id)
    del win.transactions_tab.open_dialog  # dalej prawdziwa metoda
    tx = TransactionRepository(db).for_offer(offer.id)
    assert tx.buy_price == 850 and tx.parts == [UsedPart(Defect.SCREEN, 1, 180.0)]
    assert sum(lt.qty for lt in InventoryRepository(db).lots()) == 1  # część zdjęta ze stanu
    assert win.inventory_tab.summary.text().startswith("Na stanie: <b>1</b>")
    tab = win.transactions_tab
    assert tab.table.rowCount() == 1 and "Transakcji: <b>1</b>" in tab.summary.text()
    assert tab.table.item(0, 9).text().startswith("≈")  # szacowany zysk do czasu sprzedaży

    # sprzedaż: wpisanie ceny i daty → status „sprzedany”, realny zysk
    dlg = TransactionDialog(tx, db, win.settings)
    dlg.sell_price.setValue(1600)
    dlg.sold_at.setDate(dlg.bought_at.date().addDays(12))
    dlg.repair_minutes.setValue(50)
    assert tab.open_dialog(tx, dialog=dlg)
    tx = TransactionRepository(db).for_offer(offer.id)
    assert tx.status == "sold" and tx.profit == pytest.approx(1600 - 850 - 180 - tx.other_costs)
    assert tx.parts == [UsedPart(Defect.SCREEN, 1, 180.0)]  # bez zmian w częściach — bez ponownego zdejmowania
    assert "zł/h" in tab.table.item(0, 11).text() and "iPhone 12 ze zbitym ekranem" in tab.insights.toPlainText()

    # oferta oznaczona jako kupiona (tabela, szczegóły, przycisk)
    o, val = win.model.row_at(win.model.row_of(offer.id))
    assert o.transaction_id == tx.id and BOUGHT_MARK in win.model.data(win.model.index(0, Col.FLAGS))
    assert "🛒 Kupiona" in build_details_html(o, val, win.settings)
    win.details.set_offer(o, val)
    assert win.details.bought_btn.text() == "🛒 Transakcja"

    # przełącznik poprawek zapisuje ustawienia
    tab.learning.setChecked(False)
    assert win.settings.learning.enabled is False
    win._quitting = True
    win.close()


def test_details_show_corrections_and_preview():
    from phonebot.ui.details_html import build_details_html

    offer = make_offer(*SCREEN)
    parts = PartsCatalog(default_parts())
    parts.corrections = Corrections(corrections([sold_tx(repair_actual=300) for _ in range(4)], CFG), CFG)
    val = evaluate(offer, MARKET, parts, Settings(), Mode.REPAIR)
    val.alternative = evaluate(offer, MARKET, parts, Settings(), Mode.REPAIR, apply_corrections=False)
    html = build_details_html(offer, val, Settings())
    assert "<h3>Poprawki z Twoich transakcji</h3>" in html and "Koszt naprawy: +" in html
    assert "<b>z poprawkami</b>" in html and "<b>bez poprawek</b>" in html
    assert "średnio +50% w 4 transakcjach, waga 57%" in html


def test_transactions_table_sorts_text_and_numbers(db):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    from phonebot.ui.transactions_tab import TransactionsTab

    QApplication.instance() or QApplication([])
    repo = TransactionRepository(db)
    for model, price in (("iPhone 13", 1200.0), ("iPhone 11", 450.0), ("iPhone 12", 900.0)):
        tx = sold_tx(model=model)
        tx.buy_price = price
        repo.save(tx)
    s = Settings()
    tab = TransactionsTab(db, lambda: s, lambda _s: None)
    tab.table.sortItems(1, Qt.SortOrder.AscendingOrder)  # tekst (model) — bez rekurencji w __lt__
    assert [tab.table.item(r, 1).text() for r in range(3)] == ["iPhone 11", "iPhone 12", "iPhone 13"]
    tab.table.sortItems(6, Qt.SortOrder.DescendingOrder)  # kwota: 1 200 zł > 900 zł (liczbowo, nie tekstowo)
    assert [tab.table.item(r, 6).text() for r in range(3)] == ["1 200 zł", "900 zł", "450 zł"]


def test_small_difference_is_not_applied():
    txs = [sold_tx(resale=1500, sell=1485) for _ in range(4)]  # −1% — w granicach tolerancji (3%)
    by_key = corrections(txs, CFG)
    corr = by_key[key_for("iPhone 12", [Defect.SCREEN])]
    assert corr.factors["resale"].active and corr.factor("resale") is None
    assert any("cena sprzedaży zgadza się z wyceną (4 transakcje). Bez poprawki." in i.text
               for i in insights(by_key, CFG))
    parts = PartsCatalog(default_parts())
    parts.corrections = Corrections(by_key, CFG)
    val = evaluate(make_offer(*SCREEN), MARKET, parts, Settings(), Mode.REPAIR)
    assert val.resale_value == MARKET.value and not any(n.startswith("Cena sprzedaży") for n in val.corrections)
