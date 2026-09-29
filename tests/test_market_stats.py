"""Zadanie 5: statystyki rynku — ceny dzienne, podaż, trend, czas aktywności, najlepsze pory, zapis w tle,
„za mało danych”, zakładka „Rynek” i trend w szczegółach oferty."""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

import pytest

from phonebot.core.market_stats import (
    ALL_STORAGE,
    TOO_LITTLE,
    MarketStatsConfig,
    OfferPoint,
    active_time,
    best_times,
    daily_stats,
    quantile,
    trend,
)
from phonebot.core.settings import Settings
from phonebot.services.market_stats import MarketStatsRepository, compute, load_points, stats_due
from phonebot.storage.db import open_database

from .sample_market import fill_market

CFG = MarketStatsConfig()
NOW = datetime(2026, 9, 29, 12, tzinfo=UTC)
TODAY = NOW.astimezone().date()


def pt(i, first_days_ago, last_days_ago, price, *, storage=128, cls="used", history=(), created=None, active=True,
       verdict=None, model="iPhone 13"):
    return OfferPoint(i, model, storage, cls, NOW - timedelta(days=first_days_ago), NOW - timedelta(days=last_days_ago),
                      price, [(NOW - timedelta(days=d), p) for d, p in history], created, active, verdict)


# ------------------------------------------------------------ obliczenia ---

def test_quantile_and_price_on_day():
    assert quantile([1, 2, 3, 4], 0.5) == 2.5 and quantile([], 0.5) is None and quantile([7], 0.9) == 7
    o = pt(1, 10, 0, 900, history=[(10, 1000), (4, 900)])
    assert o.price_on((NOW - timedelta(days=6)).date()) == 1000 and o.price_on(NOW.date()) == 900


def test_daily_stats_active_listings_storage_rollup_and_new_count():
    offers = [pt(1, 3, 0, 1000), pt(2, 2, 0, 1200), pt(3, 1, 1, 1100, storage=256), pt(4, 5, 4, 999)]
    days = [TODAY - timedelta(days=i) for i in range(6)]
    stats = {(s.storage_gb, s.day): s for s in daily_stats(offers, days)}
    d2 = stats[(128, TODAY - timedelta(days=2))]
    assert d2.n == 2 and d2.median == 1100 and d2.new_count == 1
    all1 = stats[(ALL_STORAGE, TODAY - timedelta(days=1))]  # „wszystkie pamięci” zawiera 256 GB
    assert all1.n == 3 and all1.median == 1100 and all1.new_count == 1
    assert (128, TODAY - timedelta(days=5)) in stats and stats[(128, TODAY - timedelta(days=5))].new_count == 1


def test_trend_down_flat_and_too_little():
    rnd = random.Random(1)
    falling = [(TODAY - timedelta(days=d), 1800 * (1 - 0.10 * (30 - d) / 30) * rnd.uniform(0.97, 1.03))
               for d in range(30) for _ in range(3)]
    t = trend(falling, CFG, TODAY)
    assert t.direction == "down" and -13 < t.pct < -7 and t.describe().startswith("↘ spada (−")
    noisy_flat = [(TODAY - timedelta(days=d), 1500 * rnd.uniform(0.8, 1.2)) for d in range(30) for _ in range(2)]
    flat = trend(noisy_flat, CFG, TODAY)
    assert flat.direction == "flat" and "bez wyraźnej zmiany" in flat.describe()
    few = trend(falling[:10], CFG, TODAY)
    assert few.direction == "unknown" and few.describe() == TOO_LITTLE
    with_junk = falling + [(TODAY, 50.0), (TODAY, 99999.0)]  # akcesorium / pomyłka — pomijane
    assert trend(with_junk, CFG, TODAY).direction == "down"


def test_active_time_needs_enough_ended_listings():
    ended = [pt(i, 12, 2, 1000, active=False) for i in range(4)]
    assert active_time(ended, CFG).median_days is None and active_time(ended, CFG).describe() == TOO_LITTLE
    ended.append(pt(9, 30, 10, 1000, active=False, created=NOW - timedelta(days=40)))  # wystawione przed śledzeniem
    at = active_time(ended + [pt(10, 3, 0, 1000)], CFG)  # aktywne się nie liczą
    assert at.count == 5 and at.median_days == 10


def test_best_times_counts_good_offers_and_cheap_days():
    offers = []
    for i in range(40):
        # wtorek 20:00 — okazje; data z portalu używana, bo ogłoszenie świeże
        when = datetime(2026, 9, 1, 20, tzinfo=UTC) + timedelta(weeks=i % 4)  # 1.09.2026 to wtorek
        offers.append(OfferPoint(i, "iPhone 13", 128, "used", when + timedelta(minutes=30), when + timedelta(days=3),
                                 900, [], when.replace(tzinfo=UTC), True, "KUPUJ" if i < 25 else "ODPUŚĆ"))
    bumped = OfferPoint(99, "iPhone 13", 128, "used", datetime(2026, 9, 6, 10, tzinfo=UTC),
                        datetime(2026, 9, 7, tzinfo=UTC), 900, [], datetime(2026, 1, 1, tzinfo=UTC), True, "KUPUJ")
    bt = best_times([*offers, bumped], {}, CFG)
    local_tue = offers[0].created_at.astimezone()
    assert bt.enough and bt.good_total == 26 and bt.best_weekday() == local_tue.weekday()
    assert bt.weekdays[datetime(2026, 9, 6, 10, tzinfo=UTC).astimezone().weekday()].good == 1  # „podbite” — dzień
    assert bt.describe()[0].startswith("Najwięcej okazji (KUPUJ / NEGOCJUJ) pojawia się we wtorek")
    assert not best_times(offers[:3], {}, CFG).enough


# ------------------------------------------------------------- zapis w tle ---

@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "m.sqlite3")
    yield conn
    conn.close()


def test_compute_stores_stats_freezes_old_days_and_is_due(db):
    fill_market(db, NOW)
    s = Settings()
    assert stats_due(db, s.market_stats, NOW)
    summary = compute(db, s, NOW)
    assert summary["days"] == 91 and summary["rows"] > 200
    repo = MarketStatsRepository(db)
    assert repo.models()[0] == "iPhone 13" and repo.storages("iPhone 12") == [64, 128]
    t, overall = repo.trend_for("iPhone 13", 128, "used")
    assert t.direction == "down" and not overall
    assert repo.active_time("iPhone 13").median_days is not None and repo.best_times().enough
    assert not stats_due(db, s.market_stats, NOW + timedelta(hours=1))
    assert stats_due(db, s.market_stats, NOW + timedelta(hours=7))
    old_day = TODAY - timedelta(days=60)
    before = repo.daily("iPhone 13", 128, "used", old_day)[0]
    db.execute("DELETE FROM offers")  # dawne oferty usunięte z bazy (np. po 97 dniach)
    again = compute(db, s, NOW + timedelta(hours=7))
    assert again["days"] == s.market_stats.refresh_days + 1
    kept = repo.daily("iPhone 13", 128, "used", old_day)[0]
    assert kept.day == before.day and kept.median == before.median  # starsze dni zostają zapisane
    assert not [r for r in repo.daily("iPhone 13", 128, "used", TODAY - timedelta(days=5)) if r.n]


def test_trend_falls_back_to_all_storages_and_too_little(db):
    fill_market(db, NOW)
    compute(db, Settings(), NOW)
    repo = MarketStatsRepository(db)
    t, overall = repo.trend_for("iPhone 11", 64, "damaged")
    assert t.direction == "unknown"  # mało ofert uszkodzonych 11 — „za mało danych”, nie zgadujemy
    assert repo.trend_for(None, None, "used") is None


def test_first_verdict_backfill_and_post_scan(db):
    from phonebot.core.normalizer import parse_offer
    from phonebot.services.post_scan import run_post_scan
    from phonebot.services.scanner import ScanReport
    from phonebot.storage.repositories import OfferRepository

    from .conftest import make_raw

    repo = OfferRepository(db)
    raw = make_raw("iPhone 13 128GB zbity ekran", 700, "zbity ekran")
    old = repo.upsert(raw, parse_offer(raw)).offer_id
    compute(db, Settings())
    assert db.execute("SELECT first_verdict FROM offers WHERE id = ?", (old,)).fetchone()[0]  # uzupełniony
    raw2 = make_raw("iPhone 12 128GB", 1500)
    new = repo.upsert(raw2, parse_offer(raw2)).offer_id
    run_post_scan(db, Settings(), ScanReport(new_offer_ids=[new]))
    assert db.execute("SELECT first_verdict FROM offers WHERE id = ?", (new,)).fetchone()[0]
    assert len(load_points(db, datetime.now(UTC) - timedelta(days=2))) == 2


# ------------------------------------------------------------------- UI ---

def test_market_tab_charts_tiles_ranges_and_too_little(db, tmp_path):
    from PySide6.QtCore import QPoint
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from phonebot.ui.charts import nice_ticks
    from phonebot.ui.market_tab import MarketTab

    QApplication.instance() or QApplication([])
    ticks = nice_ticks(1520, 1930)
    assert ticks[0] <= 1520 and ticks[-1] >= 1930 and 3 <= len(ticks) <= 7 and ticks[1] - ticks[0] in (100, 200)
    fill_market(db)
    compute(db, Settings())
    s = Settings()
    tab = MarketTab(db, lambda: s)
    tab.resize(1200, 1100)
    tab.select_model("iPhone 13")
    tab.storage.setCurrentIndex(tab.storage.findData(128))
    assert len(tab.price_chart.points) >= 25 and tab.price_chart.points[-1].low <= tab.price_chart.points[-1].median
    assert sum(tab.supply_chart.values) > 100 and len(tab.supply_chart.values) == 30
    assert tab.trend_tile.value.text() == "↘ spada" and "dni" in tab.active_tile.value.text()
    assert "we wtorek" in tab.best_text.text()
    tab.range_group.button(7).click()
    assert len(tab.supply_chart.values) == 7 and len(tab.price_chart.points) <= 7
    tab.range_group.button(90).click()
    assert len(tab.supply_chart.values) == 90
    tab.show()
    QTest.mouseMove(tab.price_chart, QPoint(400, 80))  # najechanie: bez błędów, zaznaczony punkt
    assert tab.price_chart._hover is not None
    assert not tab.price_chart.grab().isNull() and not tab.hour_chart.grab().isNull()
    # nowe iPhone 13 — brak ofert: „za mało danych” zamiast pustego / mylącego wykresu
    tab.cls.setCurrentIndex(tab.cls.findData("new"))
    assert tab.price_chart.points == [] and tab.price_chart.empty_text.startswith(TOO_LITTLE)
    assert tab.trend_tile.value.text() == TOO_LITTLE and tab.supply_chart.values == []
    assert not tab.supply_chart.grab().isNull()  # rysuje napis zamiast wykresu
    tab.close()


def test_trend_in_offer_details_and_window_tab(db, tmp_path):
    from PySide6.QtWidgets import QApplication

    from phonebot.core.normalizer import parse_offer
    from phonebot.services.evaluator import Evaluator
    from phonebot.storage.repositories import OfferRepository
    from phonebot.ui.details_html import build_details_html
    from phonebot.ui.main_window import MainWindow

    from .conftest import make_raw

    QApplication.instance() or QApplication([])
    fill_market(db)
    compute(db, Settings())
    raw = make_raw("iPhone 13 128GB zbity ekran", 900, "zbity ekran")
    offer = OfferRepository(db).get(OfferRepository(db).upsert(raw, parse_offer(raw)).offer_id)
    val = Evaluator(db, Settings()).evaluate(offer)
    assert val.trend_text.startswith("↘ spada") and val.active_days is not None
    html = build_details_html(offer, val, Settings())
    assert "trend ceny (30 dni): ↘ spada" in html and "aktywne średnio" in html
    win = MainWindow(db, tmp_path / "m.sqlite3", thumbs_dir=tmp_path)
    assert "Rynek" in [win.main_tabs.tabText(i) for i in range(win.main_tabs.count())]
    assert win.market_tab.model.count() == 3
    win._quitting = True
    win.close()
