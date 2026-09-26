"""Panel filtrów widoku (po lewej stronie okna)."""
from __future__ import annotations

import dataclasses

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ..core.catalog import model_names
from ..core.models import Condition, RowColor
from ..core.view_filter import ViewFilter
from .table_model import SOURCE_NAMES
from .theme import COLOR_LABEL


def _checks(values: list[tuple[str, str]], selected: list[str], on_change) -> tuple[QWidget, dict[str, QCheckBox]]:
    box = QWidget()
    lay = QVBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(6)
    checks = {}
    for value, label in values:
        cb = QCheckBox(label)
        cb.setChecked(value in selected)
        cb.toggled.connect(on_change)
        lay.addWidget(cb)
        checks[value] = cb
    return box, checks


class FiltersPanel(QScrollArea):
    changed = Signal(object)  # ViewFilter
    location_requested = Signal()

    def __init__(self, f: ViewFilter, location_name: str, parent=None):
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setMinimumWidth(240)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._loading = True
        self._timer = QTimer(self, singleShot=True, interval=250)
        self._timer.timeout.connect(self._emit)

        inner = QWidget()
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(0, 0, 6, 0)
        lay.setSpacing(10)

        # --- lokalizacja ---
        loc = QGroupBox("Lokalizacja")
        self.location_label = QLabel()
        change = QPushButton("Zmień…")
        change.clicked.connect(self.location_requested.emit)
        self.radius = QSpinBox(minimum=0, maximum=1000, singleStep=10, suffix=" km", specialValueText="bez limitu")
        self.radius.setValue(f.radius_km)
        self.radius.valueChanged.connect(self._changed)
        self.keep_shipping = QCheckBox("dalsze z wysyłką też pokazuj")
        self.keep_shipping.setChecked(f.radius_keeps_shipping)
        self.keep_shipping.toggled.connect(self._changed)
        self.shipping_only = QCheckBox("tylko oferty z wysyłką")
        self.shipping_only.setChecked(f.shipping_only)
        self.shipping_only.toggled.connect(self._changed)
        row = QHBoxLayout()
        row.addWidget(self.location_label, 1)
        row.addWidget(change)
        ll = QFormLayout(loc)
        ll.addRow(row)
        ll.addRow("Promień:", self.radius)
        ll.addRow(self.keep_shipping)
        ll.addRow(self.shipping_only)
        self.set_location_name(location_name)
        lay.addWidget(loc)

        # --- szukaj / cena / zysk ---
        basic = QGroupBox("Cena i zysk")
        self.text = QLineEdit(f.text)
        self.text.setPlaceholderText("szukaj w tytule/mieście…")
        self.text.textChanged.connect(self._changed)
        self.price_min = QDoubleSpinBox(maximum=20000, singleStep=50, suffix=" zł", decimals=0,
                                        specialValueText="bez limitu")
        self.price_max = QDoubleSpinBox(maximum=20000, singleStep=50, suffix=" zł", decimals=0,
                                        specialValueText="bez limitu")
        self.price_min.setValue(f.price_min)
        self.price_max.setValue(f.price_max)
        self.min_profit_on = QCheckBox("Min. zysk:")
        self.min_profit_on.setChecked(f.min_profit_enabled)
        self.min_profit = QDoubleSpinBox(minimum=-5000, maximum=10000, singleStep=50, suffix=" zł", decimals=0)
        self.min_profit.setValue(f.min_profit)
        for w in (self.price_min, self.price_max, self.min_profit):
            w.valueChanged.connect(self._changed)
        self.min_profit_on.toggled.connect(self._changed)
        bl = QFormLayout(basic)
        bl.addRow(self.text)
        bl.addRow("Cena od:", self.price_min)
        bl.addRow("Cena do:", self.price_max)
        bl.addRow(self.min_profit_on, self.min_profit)
        lay.addWidget(basic)

        # --- ocena, stan, portal ---
        colors_box, self.color_checks = _checks([(c.value, COLOR_LABEL[c]) for c in RowColor], f.colors, self._changed)
        cond_box, self.cond_checks = _checks([(c.value, c.label) for c in Condition], f.conditions, self._changed)
        src_box, self.src_checks = _checks(list(SOURCE_NAMES.items()), f.sources, self._changed)
        risk_box, self.risk_checks = _checks([("low", "niskie"), ("medium", "⚠ średnie"), ("high", "⛔ wysokie")],
                                             f.risk_levels, self._changed)
        for title, w in (("Ocena (puste = wszystkie)", colors_box), ("Stan (puste = wszystkie)", cond_box),
                         ("Portal (puste = wszystkie)", src_box), ("Ryzyko oszustwa (puste = wszystkie)", risk_box)):
            g = QGroupBox(title)
            QVBoxLayout(g).addWidget(w)
            lay.addWidget(g)

        # --- modele ---
        models = QGroupBox("Modele (puste = wszystkie)")
        self.models = QListWidget()
        self.models.setMinimumHeight(180)
        for name in [*model_names(), "iPhone SE"]:
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in f.models else Qt.CheckState.Unchecked)
            self.models.addItem(item)
        self.models.itemChanged.connect(self._changed)
        clear_models = QPushButton("Odznacz wszystkie modele")
        clear_models.clicked.connect(self._clear_models)
        ml = QVBoxLayout(models)
        ml.addWidget(self.models)
        ml.addWidget(clear_models)
        lay.addWidget(models)

        reset = QPushButton("Wyczyść wszystkie filtry")
        reset.clicked.connect(lambda: self.set_filter(ViewFilter()))
        lay.addWidget(reset)
        lay.addStretch(1)
        self.setWidget(inner)
        self._loading = False

    # --- API ---

    def set_location_name(self, name: str) -> None:
        self.location_label.setText(f"<b>{name}</b>")

    def current(self) -> ViewFilter:
        def picked(checks: dict[str, QCheckBox]) -> list[str]:
            return [v for v, cb in checks.items() if cb.isChecked()]

        models = [self.models.item(i).text() for i in range(self.models.count())
                  if self.models.item(i).checkState() == Qt.CheckState.Checked]
        return ViewFilter(
            models=models, price_min=self.price_min.value(), price_max=self.price_max.value(),
            radius_km=self.radius.value(), radius_keeps_shipping=self.keep_shipping.isChecked(),
            conditions=picked(self.cond_checks), sources=picked(self.src_checks),
            min_profit_enabled=self.min_profit_on.isChecked(), min_profit=self.min_profit.value(),
            shipping_only=self.shipping_only.isChecked(), colors=picked(self.color_checks), text=self.text.text(),
            risk_levels=picked(self.risk_checks),
        )

    def set_filter(self, f: ViewFilter) -> None:
        self._loading = True
        self.text.setText(f.text)
        self.price_min.setValue(f.price_min)
        self.price_max.setValue(f.price_max)
        self.radius.setValue(f.radius_km)
        self.keep_shipping.setChecked(f.radius_keeps_shipping)
        self.shipping_only.setChecked(f.shipping_only)
        self.min_profit_on.setChecked(f.min_profit_enabled)
        self.min_profit.setValue(f.min_profit)
        for checks, selected in ((self.color_checks, f.colors), (self.cond_checks, f.conditions),
                                 (self.src_checks, f.sources), (self.risk_checks, f.risk_levels)):
            for v, cb in checks.items():
                cb.setChecked(v in selected)
        for i in range(self.models.count()):
            item = self.models.item(i)
            item.setCheckState(Qt.CheckState.Checked if item.text() in f.models else Qt.CheckState.Unchecked)
        self._loading = False
        self._emit()

    def _clear_models(self) -> None:
        self._loading = True
        for i in range(self.models.count()):
            self.models.item(i).setCheckState(Qt.CheckState.Unchecked)
        self._loading = False
        self._changed()

    def _changed(self, *_args) -> None:
        if not self._loading:
            self._timer.start()

    def _emit(self) -> None:
        self.changed.emit(dataclasses.replace(self.current()))
