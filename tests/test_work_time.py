"""Zadanie 3: czas pracy w wycenie — czasy napraw z tabeli części, czas obsługi, zysk na godzinę, próg,
koszt czasu osobno, kolumny i sortowanie, migracja tabeli części."""
from __future__ import annotations

import pytest

from phonebot.core.models import Defect, Mode, Verdict
from phonebot.core.parts import PartPrice, PartsCatalog, default_parts
from phonebot.core.settings import Settings
from phonebot.core.sorting import FIELDS, PRESETS, SortLevel, sort_rows
from phonebot.core.valuation import evaluate
from phonebot.core.work_time import WorkTimeConfig, estimate, format_minutes, per_hour, summary
from phonebot.storage.db import MIGRATIONS, connect, migrate, open_database
from phonebot.storage.repositories import PartsRepository

from .conftest import make_offer
from .test_stage3 import MARKET

CFG = WorkTimeConfig()
SCREEN = ("iPhone 12 128GB zbity ekran", "zbity ekran, reszta sprawna")


def labels(items):
    return {i.label: i.minutes for i in items}


def test_repair_minutes_from_parts_table_default_and_unknown():
    offer = make_offer(*SCREEN[:1], 900, SCREEN[1])
    table = PartsCatalog([PartPrice("iPhone 12", Defect.SCREEN, 280, minutes=70)])
    assert labels(estimate(offer, table, CFG, Mode.REPAIR))["Naprawa: Wyświetlacz / szyba"] == 70
    no_minutes = PartsCatalog([PartPrice("iPhone 12", Defect.SCREEN, 280)])  # puste = domyślny dla rodzaju
    assert labels(estimate(offer, no_minutes, CFG, Mode.REPAIR))["Naprawa: Wyświetlacz / szyba"] == 45
    face = make_offer("iPhone 12 128GB nie działa face id", 900, "face id nie działa")
    items = labels(estimate(face, PartsCatalog(default_parts()), CFG, Mode.REPAIR))
    assert items["Naprawa: Face ID (zakres nieznany)"] == CFG.unknown_repair_minutes


def test_handling_time_parcel_pickup_and_resell():
    offer = make_offer(*SCREEN[:1], 900, SCREEN[1])
    parts = PartsCatalog(default_parts())
    items = labels(estimate(offer, parts, CFG, Mode.REPAIR))
    assert items["Odbiór paczki"] == 10 and items["Sprawdzenie telefonu"] == 20
    assert items["Wystawienie ogłoszenia"] == 20 and items["Sprzedaż (rozmowy, pakowanie, nadanie)"] == 30
    assert sum(items.values()) == 45 + 80
    local = make_offer(*SCREEN[:1], 900, SCREEN[1], shipping_available=False)
    local.distance_km = 30
    assert labels(estimate(local, parts, CFG, Mode.REPAIR))["Dojazd i odbiór (30 km × 2)"] == 72 + 15
    local.distance_km = None
    assert labels(estimate(local, parts, CFG, Mode.REPAIR))["Odbiór osobisty (odległość nieznana)"] == 60
    resell = labels(estimate(make_offer("iPhone 12 128GB", 1500), parts, CFG, Mode.RESELL))
    assert not any(k.startswith("Naprawa") for k in resell)


def test_formatting_and_summary():
    assert format_minutes(45) == "45 min" and format_minutes(180) == "3 h" and format_minutes(125) == "2 h 5 min"
    assert per_hour(150, 180) == 50 and per_hour(None, 60) is None and per_hour(100, 0) is None
    assert summary(150, 180, 50) == "Zysk 150 zł, czas 3 h, czyli 50 zł/h."


def test_valuation_has_time_rate_and_separate_time_cost():
    offer = make_offer(*SCREEN[:1], 900, SCREEN[1])
    val = evaluate(offer, MARKET, PartsCatalog(default_parts()), Settings(), Mode.REPAIR)
    assert val.work_minutes == 125 and val.profit_per_hour == pytest.approx(val.expected_profit / (125 / 60), 0.01)
    assert val.time_cost == pytest.approx(125 / 60 * 50, 0.01)
    # koszt czasu nie jest odejmowany od zysku — ta sama wycena bez czasu pracy daje ten sam zysk
    s = Settings()
    s.work.enabled = False
    off = evaluate(offer, MARKET, PartsCatalog(default_parts()), s, Mode.REPAIR)
    assert off.expected_profit == val.expected_profit and off.work_minutes is None and off.profit_per_hour is None
    assert any(r.startswith(f"Zysk {val.expected_profit:.0f} zł, czas 2 h 5 min, czyli") for r in val.reasons)


def test_min_profit_per_hour_lowers_verdict():
    s = Settings()
    offer = make_offer(*SCREEN[:1], 1150, SCREEN[1])
    parts = PartsCatalog(default_parts())
    base = evaluate(offer, MARKET, parts, s, Mode.REPAIR)
    assert base.verdict is Verdict.BUY and not base.time_limited
    s.work.min_profit_per_hour = base.profit_per_hour + 20  # próg powyżej zysku na godzinę tej oferty
    low = evaluate(offer, MARKET, parts, s, Mode.REPAIR)
    assert low.verdict.rank < base.verdict.rank and low.max_buy_price < base.max_buy_price
    assert low.time_limited and low.required_profit == pytest.approx(s.work.min_profit_per_hour * 125 / 60, 0.01)
    line = next(r for r in low.reasons if r.startswith("Zysk "))
    assert f"poniżej progu {s.work.min_profit_per_hour:.0f} zł/h" in line and "werdykt obniżony z KUPUJ" in line
    # przy maksymalnej cenie zakupu zysk na godzinę sięga progu
    at_max = evaluate(make_offer(*SCREEN[:1], low.max_buy_price, SCREEN[1]), MARKET, parts, s, Mode.REPAIR)
    assert at_max.profit_per_hour >= s.work.min_profit_per_hour and at_max.verdict is Verdict.BUY
    s.work.min_profit_per_hour = 0  # bez progu — jak dawniej
    assert evaluate(offer, MARKET, parts, s, Mode.REPAIR).max_buy_price == base.max_buy_price


def test_repair_time_corrected_from_transactions():
    from phonebot.core.transactions import Correction, Corrections, Factor, LearningConfig, key_for

    offer = make_offer(*SCREEN[:1], 900, SCREEN[1])
    parts = PartsCatalog(default_parts())
    key = key_for("iPhone 12", [Defect.SCREEN])
    corr = Correction(key, 4, {"repair_time": Factor("repair_time", 2.0, 4, 0.5, 45, 90)})
    parts.corrections = Corrections({key: corr}, LearningConfig())
    val = evaluate(offer, MARKET, parts, Settings(), Mode.REPAIR)
    item = next(i for i in val.time_items if i.repair)
    assert item.minutes == 68 and "średnio +100% w 4 transakcjach, waga 50%" in item.label  # 45 × 1,5
    assert val.baseline["repair_minutes"] == 45 and any(n.startswith("Czas naprawy") for n in val.corrections)
    off = evaluate(offer, MARKET, parts, Settings(), Mode.REPAIR, apply_corrections=False)
    assert next(i for i in off.time_items if i.repair).minutes == 45 and not off.corrections


def test_sort_fields_and_preset():
    parts = PartsCatalog(default_parts())
    rows = [(o, evaluate(o, MARKET, parts, Settings(), Mode.REPAIR)) for o in (
        make_offer(*SCREEN[:1], 1100, SCREEN[1]), make_offer("iPhone 12 128GB", 1300),
        make_offer("iPhone 12 128GB zbita tylna szyba", 1000, "pęknięta tylna szyba"))]
    sort_rows(rows, (SortLevel("per_hour", "desc"),))
    rates = [v.profit_per_hour for _, v in rows]
    assert rates == sorted(rates, reverse=True)
    sort_rows(rows, (SortLevel("work_time", "asc"),))
    assert [v.work_minutes for _, v in rows] == sorted(v.work_minutes for _, v in rows)
    assert FIELDS["per_hour"].label == "Zysk na godzinę" and "Najlepszy zysk na godzinę" in PRESETS


def test_migration_fills_minutes_and_repository_roundtrip(tmp_path):
    path = tmp_path / "old.sqlite3"
    conn = connect(path)
    for idx, script in enumerate(MIGRATIONS[:13], start=1):  # baza sprzed zadania 3
        for stmt in (x.strip() for x in script.split(";")):
            if stmt:
                conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {idx}")
    conn.execute("INSERT INTO parts_prices (model, part, price) VALUES ('iPhone 12', 'screen', 280)")
    conn.execute("INSERT INTO parts_prices (model, part, price) VALUES ('iPhone 12', 'face_id', 400)")
    migrate(conn)
    rows = {r.part: r for r in PartsRepository(conn).all()}
    assert rows[Defect.SCREEN].minutes == 45 and rows[Defect.FACE_ID].minutes is None
    repo = PartsRepository(conn)
    repo.upsert(PartPrice("iPhone 12", Defect.SCREEN, 250, minutes=60))
    assert {r.part: r for r in repo.all()}[Defect.SCREEN].minutes == 60
    conn.close()
    fresh = open_database(tmp_path / "new.sqlite3")
    PartsRepository(fresh).seed_defaults_if_empty()
    assert all(r.minutes for r in PartsRepository(fresh).all())
    fresh.close()


def test_table_columns_details_and_parts_editor(tmp_path):
    from PySide6.QtWidgets import QApplication

    from phonebot.ui.details_html import build_details_html
    from phonebot.ui.images import ThumbnailCache
    from phonebot.ui.parts_editor import PartsEditor
    from phonebot.ui.table_model import Col, OffersTableModel

    QApplication.instance() or QApplication([])
    offer = make_offer(*SCREEN[:1], 900, SCREEN[1])
    val = evaluate(offer, MARKET, PartsCatalog(default_parts()), Settings(), Mode.REPAIR)
    model = OffersTableModel(ThumbnailCache(tmp_path))
    model.set_rows([(offer, val)])
    assert model.data(model.index(0, Col.WORK_TIME)) == "2 h 5 min"
    assert model.data(model.index(0, Col.PER_HOUR)) == f"{val.profit_per_hour:.0f} zł/h"
    assert "Odbiór paczki" in model.data(model.index(0, Col.WORK_TIME), 3)  # podpowiedź: rozbicie czasu
    html = build_details_html(offer, val, Settings())
    assert "<h3>Czas pracy</h3>" in html and "Koszt Twojego czasu (50 zł/h)" in html
    assert f"czyli {val.profit_per_hour:.0f} zł/h" in html and "Zysk po opłaceniu Twojego czasu" in html

    conn = open_database(tmp_path / "e.sqlite3")
    repo = PartsRepository(conn)
    repo.upsert(PartPrice("iPhone 12", Defect.SCREEN, 280, minutes=45))
    editor = PartsEditor(repo)
    assert editor.table.horizontalHeaderItem(3).text() == "Czas pracy (min)"
    editor.table.item(0, 3).setText("75")
    editor._save()
    assert repo.all()[0].minutes == 75
    editor = PartsEditor(repo)
    editor.table.item(0, 3).setText("abc")
    with pytest.raises(ValueError, match="czas pracy"):
        editor.rows()
    conn.close()
