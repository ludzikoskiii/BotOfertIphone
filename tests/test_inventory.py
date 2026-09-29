"""Zadanie 2: magazyn części — FIFO, zgodność modeli, wycena z magazynu, premia i znacznik, filtr, zużycie,
niski stan, zakładka w oknie."""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from phonebot.core.inventory import InventoryConfig, Lot, Stock, compatible_models, fifo_pick, low_stock
from phonebot.core.models import Defect, Mode
from phonebot.core.parts import PartsCatalog, default_parts
from phonebot.core.settings import Settings
from phonebot.core.valuation import evaluate
from phonebot.core.view_filter import ViewFilter, matches
from phonebot.storage.db import open_database
from phonebot.storage.repositories import InventoryRepository

from .conftest import make_offer
from .test_stage3 import MARKET

CFG = InventoryConfig()
D = datetime(2026, 9, 1, tzinfo=UTC)


def lot(part=Defect.SCREEN, models=("iPhone 12",), qty=1, price=200.0, days=0, lid=None, quality="replacement"):
    return Lot(lid, part, list(models), quality, qty, price, D + timedelta(days=days))


def test_compatibility_groups():
    assert compatible_models("iPhone 11", Defect.SCREEN, CFG.compat) >= {"iPhone XR", "iPhone 11"}
    assert compatible_models("iPhone 11", Defect.BATTERY, CFG.compat) == {"iPhone 11"}
    stock = Stock([lot(models=["iPhone XR"], price=150)], CFG)
    assert stock.oldest("iPhone 11", Defect.SCREEN).unit_price == 150  # ekran XR pasuje do 11
    assert stock.oldest("iPhone 11", Defect.BATTERY) is None


def test_fifo_oldest_first():
    lots = [lot(price=250, days=10, lid=2, qty=2), lot(price=200, days=0, lid=1, qty=1)]
    picks = fifo_pick(lots, "iPhone 12", Defect.SCREEN, 2, CFG.compat)
    assert [(lt.id, n) for lt, n in picks] == [(1, 1), (2, 1)]
    assert fifo_pick(lots, "iPhone 12", Defect.SCREEN, 5, CFG.compat) == []  # za mało — nic
    assert Stock(lots, CFG).oldest("iPhone 12", Defect.SCREEN).unit_price == 200


def test_valuation_uses_own_price_bonus_and_marker():
    offer = make_offer("iPhone 12 128GB zbity ekran", 900, "zbity ekran, reszta sprawna")
    s = Settings()
    table = PartsCatalog(default_parts())
    without = evaluate(offer, MARKET, table, s, Mode.REPAIR)
    with_stock = evaluate(offer, MARKET, PartsCatalog(default_parts(), stock=Stock([lot(price=150)], CFG)),
                          s, Mode.REPAIR)
    assert without.repair_cost > with_stock.repair_cost
    item = next(i for i in with_stock.repair_items if "magazynu" in i.label)
    assert item.amount == 150 and "Twoja cena" in item.label
    assert not any("Wysyłka części" in i.label for i in with_stock.repair_items)  # wszystko z magazynu
    assert with_stock.parts_in_stock == [Defect.SCREEN] and without.parts_in_stock == []
    assert with_stock.score >= without.score and any("Masz na stanie" in r for r in with_stock.reasons)
    assert matches(offer, with_stock, ViewFilter(only_with_parts=True))
    assert not matches(offer, without, ViewFilter(only_with_parts=True))
    from phonebot.ui.table_model import PARTS_MARK

    assert PARTS_MARK == "🧩 masz część"


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "i.sqlite3")
    yield conn
    conn.close()


def test_repository_consume_release_and_value(db):
    repo = InventoryRepository(db)
    repo.save(lot(price=250, days=10, qty=2))
    repo.save(lot(price=200, days=0, qty=1))
    assert repo.value() == 700
    used = repo.consume("iPhone 12 Pro", Defect.SCREEN, 2, CFG, transaction_id=7)  # 12 Pro = ten sam ekran
    assert [(n, p) for _, n, p in used] == [(1, 200.0), (1, 250.0)]
    assert sum(lt.qty for lt in repo.lots()) == 1
    assert repo.consume("iPhone 12", Defect.SCREEN, 5, CFG) == []  # za mało
    assert repo.release(7) == 2 and sum(lt.qty for lt in repo.lots()) == 3


def test_low_stock_warning_for_frequent_parts(db):
    repo = InventoryRepository(db)
    repo.save(lot(qty=2, price=200))
    now = datetime.now(UTC)
    repo.consume("iPhone 12", Defect.SCREEN, 1, CFG, when=now - timedelta(days=5))
    assert low_stock(repo.lots(), repo.usage(), CFG, now) == []  # zużyta raz — to jeszcze nie „często”
    repo.consume("iPhone 12", Defect.SCREEN, 1, CFG, when=now - timedelta(days=2))
    warn = low_stock(repo.lots(), repo.usage(), CFG, now)
    assert len(warn) == 1 and warn[0].qty == 0 and "brak na stanie" in warn[0].message(60)


def test_evaluator_reads_inventory_from_db(db, tmp_path):
    from phonebot.core.normalizer import parse_offer
    from phonebot.services.evaluator import Evaluator
    from phonebot.storage.repositories import OfferRepository, PartsRepository

    from .conftest import make_raw

    PartsRepository(db).seed_defaults_if_empty()
    raw = make_raw("iPhone 12 128GB zbity ekran", 900, "zbity ekran")
    oid = OfferRepository(db).upsert(raw, parse_offer(raw)).offer_id
    offer = OfferRepository(db).get(oid)
    before = Evaluator(db, Settings()).evaluate(offer)
    InventoryRepository(db).save(lot(price=120))
    after = Evaluator(db, Settings()).evaluate(OfferRepository(db).get(oid))
    assert after.parts_in_stock and after.repair_cost < before.repair_cost
    s = Settings()
    s.inventory.enabled = False
    assert not Evaluator(db, s).evaluate(OfferRepository(db).get(oid)).parts_in_stock


def test_inventory_tab_in_window(db, tmp_path):
    from PySide6.QtWidgets import QApplication

    from phonebot.ui.inventory_tab import LotDialog
    from phonebot.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    win = MainWindow(db, tmp_path / "i.sqlite3", thumbs_dir=tmp_path)
    tabs = [win.main_tabs.tabText(i) for i in range(win.main_tabs.count())]
    assert tabs[:2] == ["Oferty", "Magazyn części"]
    dlg = LotDialog(lot(models=["iPhone 13"], qty=3, price=180))
    reloads = []
    win.inventory_tab.changed.connect(lambda: reloads.append(1))
    win.inventory_tab.add_lot(dialog=dlg)
    assert win.inventory_tab.table.rowCount() == 1 and reloads
    assert "3</b> szt." in win.inventory_tab.summary.text() and "540 zł" in win.inventory_tab.summary.text()
    win._quitting = True
    win.close()
