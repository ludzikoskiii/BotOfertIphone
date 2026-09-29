"""Zakładka „Rynek”: ceny modelu w czasie, podaż, czas aktywności ogłoszeń, najlepsze pory na zakupy, trend.

Dane czyta z tabel statystyk (liczonych w tle — ``services.market_stats``), więc przełączanie modelu
i zakresu jest natychmiastowe.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from ..core.catalog import format_storage
from ..core.market_stats import ALL_STORAGE, CLASSES, TOO_LITTLE, WEEKDAYS, WEEKDAYS_SHORT, usable_points
from ..services.market_stats import MarketStatsRepository
from .charts import BandPoint, BarChart, LineBandChart

RANGES = {"Tydzień": 7, "Miesiąc": 30, "3 miesiące": 90}


class StatTile(QFrame):
    """Kafelek z jedną liczbą (trend, mediana, czas aktywności, podaż)."""

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.setObjectName("stat_tile")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 8, 12, 8)
        self.title = QLabel(title)
        self.title.setObjectName("muted")
        self.value = QLabel("—")
        font = self.value.font()
        font.setPointSizeF(font.pointSizeF() * 1.6)
        font.setBold(True)
        self.value.setFont(font)
        self.detail = QLabel()
        self.detail.setObjectName("muted")
        self.detail.setWordWrap(True)
        lay.addWidget(self.title)
        lay.addWidget(self.value)
        lay.addWidget(self.detail)

    def set(self, value: str, detail: str = "") -> None:
        self.value.setText(value)
        self.detail.setText(detail)


class MarketTab(QWidget):
    recompute_requested = Signal()

    def __init__(self, conn: sqlite3.Connection, get_settings: Callable, parent=None):
        super().__init__(parent)
        self.repo = MarketStatsRepository(conn)
        self._get_settings = get_settings
        self.days = 30

        bar = QHBoxLayout()
        self.model = QComboBox()
        self.model.setMinimumWidth(170)
        self.storage = QComboBox()
        self.cls = QComboBox()
        for key, label in CLASSES.items():
            self.cls.addItem(label.capitalize(), key)
        for label, w in (("Model:", self.model), ("Pamięć:", self.storage), ("Stan:", self.cls)):
            bar.addWidget(QLabel(label))
            bar.addWidget(w)
        self.range_group = QButtonGroup(self)
        for i, (label, days) in enumerate(RANGES.items()):
            b = QPushButton(label)
            b.setCheckable(True)
            b.setChecked(days == self.days)
            b.setObjectName(f"range_{days}")
            self.range_group.addButton(b, days)
            bar.addWidget(b)
            if i == 0:
                bar.insertSpacing(bar.count() - 1, 12)
        self.range_group.idClicked.connect(self._set_range)
        bar.addStretch(1)
        self.status = QLabel()
        self.status.setObjectName("muted")
        bar.addWidget(self.status)
        self.recompute_btn = QPushButton("⟳ Przelicz teraz")
        self.recompute_btn.setToolTip("Statystyki liczą się w tle co kilka godzin (Ustawienia → Rynek)")
        self.recompute_btn.clicked.connect(self.recompute_requested.emit)
        bar.addWidget(self.recompute_btn)
        self.model.currentIndexChanged.connect(lambda _i: self._model_changed())
        self.storage.currentIndexChanged.connect(lambda _i: self.render())
        self.cls.currentIndexChanged.connect(lambda _i: self.render())

        tiles = QHBoxLayout()
        self.trend_tile = StatTile("Trend ceny")
        self.median_tile = StatTile("Mediana ceny ogłoszeń")
        self.active_tile = StatTile("Czas aktywności ogłoszenia")
        self.supply_tile = StatTile("Nowe ogłoszenia")
        for t in (self.trend_tile, self.median_tile, self.active_tile, self.supply_tile):
            tiles.addWidget(t, 1)

        self.price_chart = LineBandChart()
        self.price_chart.setObjectName("price_chart")
        self.price_box = QGroupBox("Ceny ogłoszeń: mediana (linia) i typowy zakres — 80% ofert (pasmo)")
        QVBoxLayout(self.price_box).addWidget(self.price_chart)
        self.supply_chart = BarChart()
        self.supply_chart.setObjectName("supply_chart")
        self.supply_box = QGroupBox("Podaż: nowe ogłoszenia dziennie")
        QVBoxLayout(self.supply_box).addWidget(self.supply_chart)

        self.best_box = QGroupBox("Najlepsze pory na zakupy")
        self.best_text = QLabel()
        self.best_text.setWordWrap(True)
        self.best_text.setTextFormat(Qt.TextFormat.RichText)
        self.day_chart, self.hour_chart = BarChart(), BarChart()
        self.day_chart.setObjectName("weekday_chart")
        self.hour_chart.setObjectName("hour_chart")
        grid = QGridLayout(self.best_box)
        grid.addWidget(self.best_text, 0, 0, 1, 2)
        grid.addWidget(QLabel("Okazje (KUPUJ / NEGOCJUJ) wg dnia tygodnia"), 1, 0)
        grid.addWidget(QLabel("Okazje wg godziny wystawienia"), 1, 1)
        grid.addWidget(self.day_chart, 2, 0)
        grid.addWidget(self.hour_chart, 2, 1)

        note = QLabel("Ceny to ceny w ogłoszeniach (wywoławcze), ta sama sztuka z kilku portali liczona raz. Czas "
                      "aktywności: od wystawienia do zniknięcia z portalu — ogłoszenie mogło zostać sprzedane albo "
                      "usunięte. Pory zakupów: według daty wystawienia z portalu, a gdy jej brak — chwili, w której "
                      "program zobaczył ofertę (gdy komputer jest wyłączony, oferty „pojawiają się” później).")
        note.setWordWrap(True)
        note.setObjectName("muted")

        body = QWidget()
        lay = QVBoxLayout(body)
        lay.addLayout(tiles)
        lay.addWidget(self.price_box)
        lay.addWidget(self.supply_box)
        lay.addWidget(self.best_box)
        lay.addWidget(note)
        lay.addStretch(1)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setWidget(body)
        outer = QVBoxLayout(self)
        outer.addLayout(bar)
        outer.addWidget(scroll, 1)
        self.refresh()

    # ------------------------------------------------------------ dane ---

    def refresh(self) -> None:
        """Po przeliczeniu statystyk: lista modeli i widok."""
        current = self.model.currentText()
        self.model.blockSignals(True)
        self.model.clear()
        self.model.addItems(self.repo.models())
        if current:
            i = self.model.findText(current)
            if i >= 0:
                self.model.setCurrentIndex(i)
        self.model.blockSignals(False)
        when = self.repo.computed_at()
        cfg = self._get_settings().market_stats
        self.status.setText(f"Policzone {when.astimezone():%d.%m %H:%M} · w tle co {cfg.recompute_hours} h"
                            if when else "Statystyki jeszcze niepoliczone — liczą się w tle")
        self._model_changed()

    def set_computing(self, running: bool) -> None:
        self.recompute_btn.setEnabled(not running)
        if running:
            self.status.setText("Liczenie statystyk w tle…")

    def select_model(self, model: str) -> None:
        i = self.model.findText(model)
        if i >= 0:
            self.model.setCurrentIndex(i)

    def _model_changed(self) -> None:
        model = self.model.currentText()
        self.storage.blockSignals(True)
        keep = self.storage.currentData()
        self.storage.clear()
        self.storage.addItem("Wszystkie", ALL_STORAGE)
        for gb in self.repo.storages(model) if model else []:
            self.storage.addItem(format_storage(gb), gb)
        i = self.storage.findData(keep)
        self.storage.setCurrentIndex(max(0, i))
        self.storage.blockSignals(False)
        self.render()

    def _set_range(self, days: int) -> None:
        self.days = days
        self.render()

    def render(self) -> None:
        cfg = self._get_settings().market_stats
        model = self.model.currentText()
        storage = self.storage.currentData() or ALL_STORAGE
        cls = self.cls.currentData()
        today = datetime.now().astimezone().date()
        first = today - timedelta(days=self.days - 1)
        rows = self.repo.daily(model, storage, cls, first) if model else []
        name = f"{model} {format_storage(storage) if storage else ''}".strip() or "—"

        # ceny: tylko dni z wystarczającą liczbą ofert
        pts = usable_points(rows, cfg)
        enough = len(pts) >= min(cfg.min_points, max(2, self.days // 2))
        band = [BandPoint(r.day, r.median, r.p10, r.p90, r.n) for r in pts]
        why = (f"{TOO_LITTLE} — potrzeba co najmniej {min(cfg.min_points, max(2, self.days // 2))} dni z "
               f"{cfg.min_offers_point}+ ofertami (jest {len(pts)})")
        self.price_chart.set_data(band if enough else [], first, today, why)
        self.price_box.setTitle(f"Ceny ogłoszeń {name} ({CLASSES[cls]}): mediana (linia) i typowy zakres — "
                                "80% ofert (pasmo)")

        # podaż: nowe ogłoszenia dziennie (dni bez ofert = 0)
        by_day = {r.day: r.new_count for r in rows}
        days = [first + timedelta(days=i) for i in range(self.days)]
        counts = [by_day.get(d, 0) for d in days]
        total_new = sum(counts)
        self.supply_chart.set_data([d.strftime("%d.%m") for d in days], counts,
                                   [f"{d:%d.%m.%Y}: {c} nowych ogłoszeń" for d, c in zip(days, counts, strict=True)],
                                   label_every=max(1, self.days // 8), show=total_new >= cfg.min_points,
                                   empty_text=f"{TOO_LITTLE} (nowych ogłoszeń w zakresie: {total_new})")

        # kafelki
        found = self.repo.trend_for(model, storage, cls) if model else None
        if found is None or found[0].direction == "unknown":
            self.trend_tile.set(TOO_LITTLE, f"potrzeba {cfg.trend_min_offers} nowych ofert z {cfg.min_points}+ dni "
                                            f"w ostatnich {cfg.trend_days} dniach")
        else:
            t, overall = found
            what = (f"bez wyraźnej zmiany w {t.days} dni" if t.direction == "flat"
                    else f"{t.pct:+.1f}% w {t.days} dni".replace(".", ",").replace("-", "−"))
            self.trend_tile.set(f"{t.arrow} {t.label}", f"{what} · {t.offers} nowych ofert"
                                + (" · wszystkie pamięci" if overall and storage else ""))
        if pts:
            last = pts[-1]
            self.median_tile.set(f"{last.median:,.0f} zł".replace(",", " "),
                                 f"{last.day:%d.%m}: {last.n} ofert, zakres {last.p10:,.0f}–{last.p90:,.0f} zł"
                                 .replace(",", " "))
        else:
            self.median_tile.set("—", TOO_LITTLE)
        at = self.repo.active_time(model) if model else None
        if at is not None and at.median_days is not None:
            self.active_tile.set(f"~{at.median_days:.0f} dni", f"{model}: mediana z {at.count} zakończonych ogłoszeń")
        else:
            everyone = self.repo.active_time(None)
            extra = (f" · wszystkie modele: ~{everyone.median_days:.0f} dni" if everyone and everyone.median_days
                     is not None else "")
            self.active_tile.set("—", f"{TOO_LITTLE} (zakończonych: {at.count if at else 0}){extra}")
        per_day = f"{total_new / self.days:.1f}".replace(".", ",")
        self.supply_tile.set(str(total_new), f"w zakresie {self.days} dni · średnio {per_day} dziennie")

        self._render_best(model)

    def _render_best(self, model: str) -> None:
        bt = self.repo.best_times(model) if model else None
        scope = model
        if bt is None or not bt.enough:
            bt, scope = self.repo.best_times(None), "wszystkie modele"
        if bt is None:
            self.best_text.setText(TOO_LITTLE)
            self.day_chart.set_data([], [], empty_text=TOO_LITTLE)
            self.hour_chart.set_data([], [], empty_text=TOO_LITTLE)
            return
        self.best_box.setTitle(f"Najlepsze pory na zakupy — {scope}")
        self.best_text.setText("<br>".join(bt.describe()))
        self.day_chart.set_data(
            WEEKDAYS_SHORT, [s.good for s in bt.weekdays],
            [f"{WEEKDAYS[i]}: {s.good} okazji z {s.total} nowych ofert"
             + (f"\nceny: {s.price_index:+.0f}% względem mediany" if s.price_index is not None else "")
             for i, s in enumerate(bt.weekdays)], show=bt.enough,
            empty_text=f"{TOO_LITTLE} (okazji: {bt.good_total}, potrzeba "
                       f"{self._get_settings().market_stats.min_good_offers})")
        self.hour_chart.set_data(
            [str(h) for h in range(24)], [s.good for s in bt.hours],
            [f"{h}:00–{h + 1}:00: {s.good} okazji z {s.total} nowych ofert" for h, s in enumerate(bt.hours)],
            label_every=3, show=bt.enough, empty_text=TOO_LITTLE)

