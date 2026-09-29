"""Zakładka „Magazyn części”: partie części, zgodność modeli, niski stan."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime

from PySide6.QtCore import QDate, Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.catalog import model_names
from ..core.inventory import QUALITIES, Lot, low_stock
from ..core.models import Defect
from ..storage.repositories import InventoryRepository

HEADERS = ["Rodzaj", "Pasujące modele", "Jakość", "Ilość", "Cena zakupu", "Data zakupu", "Dostawca", "Uwagi"]
PART_KINDS = [d for d in Defect if d not in (Defect.NO_POWER, Defect.WATER_DAMAGE)]


class LotDialog(QDialog):
    """Dodanie / edycja partii części."""

    def __init__(self, lot: Lot | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Część w magazynie")
        self.lot = lot or Lot(None, Defect.SCREEN, [], "replacement", 1, 0.0, datetime.now(UTC), "", "")
        form = QFormLayout(self)
        self.part = QComboBox()
        for d in PART_KINDS:
            self.part.addItem(d.label, d.value)
        self.part.setCurrentIndex(max(0, self.part.findData(self.lot.part.value)))
        form.addRow("Rodzaj:", self.part)
        self.models = QListWidget()
        self.models.setMinimumHeight(200)
        for name in model_names():
            item = QListWidgetItem(name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if name in self.lot.models else Qt.CheckState.Unchecked)
            self.models.addItem(item)
        form.addRow("Pasujące modele:", self.models)
        self.quality = QComboBox()
        for key, label in QUALITIES.items():
            self.quality.addItem(label, key)
        self.quality.setCurrentIndex(max(0, self.quality.findData(self.lot.quality)))
        form.addRow("Jakość:", self.quality)
        self.qty = QSpinBox(minimum=0, maximum=9999)
        self.qty.setValue(self.lot.qty)
        form.addRow("Ilość:", self.qty)
        self.price = QDoubleSpinBox(maximum=100000, decimals=2, suffix=" zł / szt.")
        self.price.setValue(self.lot.unit_price)
        form.addRow("Cena zakupu:", self.price)
        self.date = QDateEdit(calendarPopup=True)
        when = (self.lot.bought_at or datetime.now(UTC)).astimezone()
        self.date.setDate(QDate(when.year, when.month, when.day))
        form.addRow("Data zakupu:", self.date)
        self.supplier = QLineEdit(self.lot.supplier)
        form.addRow("Dostawca:", self.supplier)
        self.note = QLineEdit(self.lot.note)
        form.addRow("Uwagi:", self.note)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _accept(self) -> None:
        models = [self.models.item(i).text() for i in range(self.models.count())
                  if self.models.item(i).checkState() == Qt.CheckState.Checked]
        if not models:
            QMessageBox.warning(self, "Magazyn", "Zaznacz co najmniej jeden pasujący model.")
            return
        d = self.date.date()
        self.lot.part = Defect(self.part.currentData())
        self.lot.models = models
        self.lot.quality = self.quality.currentData()
        self.lot.qty = self.qty.value()
        self.lot.unit_price = self.price.value()
        self.lot.bought_at = datetime(d.year(), d.month(), d.day(), 12, tzinfo=UTC)
        self.lot.supplier = self.supplier.text().strip()
        self.lot.note = self.note.text().strip()
        self.accept()


class CompatDialog(QDialog):
    """Edytowalna tabela zgodności: części wspólne dla kilku modeli."""

    def __init__(self, compat: dict[str, list[list[str]]], parent=None):
        from .settings_dialog import EditableTable

        super().__init__(parent)
        self.setWindowTitle("Zgodność części między modelami")
        self.resize(760, 460)
        lay = QVBoxLayout(self)
        info = QLabel("Każdy wiersz to grupa modeli, w których dana część jest taka sama (np. ekran iPhone XR = "
                      "iPhone 11). Część kupiona do jednego modelu z grupy pasuje do pozostałych. Domyślne grupy są "
                      "orientacyjne — sprawdź u dostawcy przed naprawą.")
        info.setWordWrap(True)
        lay.addWidget(info)
        labels = {d.value: d.label for d in Defect}
        rows = [[labels.get(part, part), ", ".join(group)] for part, groups in compat.items() for group in groups]
        self.table = EditableTable(["Część (np. Wyświetlacz / szyba)", "Modele (po przecinku)"], rows)
        lay.addWidget(self.table)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

    def compat(self) -> dict[str, list[list[str]]]:
        by_label = {d.label.lower(): d.value for d in Defect} | {d.value: d.value for d in Defect}
        out: dict[str, list[list[str]]] = {}
        for part, models in self.table.values():
            key = by_label.get(part.strip().lower())
            group = [m.strip() for m in models.split(",") if m.strip()]
            if key and len(group) >= 2:
                out.setdefault(key, []).append(group)
        return out


class InventoryTab(QWidget):
    changed = Signal()  # stan magazynu zmieniony → nowa wycena ofert

    def __init__(self, conn: sqlite3.Connection, get_settings: Callable, save_settings: Callable, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.repo = InventoryRepository(conn)
        self._get_settings, self._save_settings = get_settings, save_settings
        lay = QVBoxLayout(self)
        bar = QHBoxLayout()
        for text, slot, name in (("＋ Dodaj część", self.add_lot, "inv_add"), ("Edytuj", self.edit_lot, "inv_edit"),
                                 ("Usuń", self.delete_lot, "inv_delete"),
                                 ("Zgodność części…", self.edit_compat, "inv_compat")):
            b = QPushButton(text)
            b.setObjectName(name)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        self.summary = QLabel()
        bar.addWidget(self.summary)
        lay.addLayout(bar)
        self.warning = QLabel()
        self.warning.setObjectName("low_stock")
        self.warning.setWordWrap(True)
        self.warning.setStyleSheet("color: #e67700;")
        lay.addWidget(self.warning)
        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(lambda _i: self.edit_lot())
        lay.addWidget(self.table)
        hint = QLabel("Wycena naprawy: jeśli masz część na stanie, koszt liczony jest po Twojej cenie zakupu "
                      "(najstarsza sztuka pierwsza); jeśli nie — z tabeli cen części. Oferty, do których masz części, "
                      "mają znacznik „🧩 masz część” i premię do oceny (Ustawienia → Zakup i naprawa). Użycie części "
                      "w transakcji zdejmuje ją ze stanu.")
        hint.setWordWrap(True)
        hint.setObjectName("muted")
        lay.addWidget(hint)
        self.refresh()

    # ------------------------------------------------------------------ dane ---

    def refresh(self) -> None:
        self._lots = self.repo.lots()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self._lots))
        for r, lot in enumerate(self._lots):
            when = lot.bought_at.astimezone().strftime("%Y-%m-%d") if lot.bought_at else "—"
            values = [lot.part.label, ", ".join(lot.models), QUALITIES.get(lot.quality, lot.quality), lot.qty,
                      f"{lot.unit_price:.2f} zł", when, lot.supplier, lot.note]
            for c, v in enumerate(values):
                item = QTableWidgetItem()
                item.setData(Qt.ItemDataRole.DisplayRole, v)
                item.setData(Qt.ItemDataRole.UserRole, lot.id)
                if c == 3 and lot.qty <= 0:
                    item.setForeground(Qt.GlobalColor.gray)
                self.table.setItem(r, c, item)
        self.table.setSortingEnabled(True)
        for c in (0, 2, 4, 5, 6):
            self.table.resizeColumnToContents(c)
        pieces = sum(max(0, lot.qty) for lot in self._lots)
        self.summary.setText(f"Na stanie: <b>{pieces}</b> szt. · wartość <b>{self.repo.value():.0f} zł</b>")
        cfg = self._get_settings().inventory
        warnings = low_stock(self._lots, self.repo.usage(), cfg, datetime.now(UTC))
        self.warning.setText("⚠ Kończą się części, które często zużywasz: "
                             + "; ".join(w.message(cfg.frequent_days) for w in warnings) if warnings else "")
        self.warning.setVisible(bool(warnings))
        self.low_stock_warnings = warnings

    def _selected(self) -> Lot | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        lot_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        return next((lot for lot in self._lots if lot.id == lot_id), None)

    def add_lot(self, *, dialog: LotDialog | None = None) -> None:
        dlg = dialog or LotDialog(parent=self)
        if dialog is not None or dlg.exec() == QDialog.DialogCode.Accepted:
            self.repo.save(dlg.lot)
            self._changed()

    def edit_lot(self) -> None:
        lot = self._selected()
        if lot is None:
            return
        dlg = LotDialog(lot, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            self.repo.save(dlg.lot)
            self._changed()

    def delete_lot(self) -> None:
        lot = self._selected()
        if lot is None:
            return
        if QMessageBox.question(self, "Magazyn", f"Usunąć „{lot.label}” ({lot.qty} szt.)?") \
                == QMessageBox.StandardButton.Yes:
            self.repo.delete(lot.id)
            self._changed()

    def edit_compat(self) -> None:
        settings = self._get_settings()
        dlg = CompatDialog(settings.inventory.compat, self)
        if dlg.exec() == QDialog.DialogCode.Accepted:
            settings.inventory.compat = dlg.compat()
            self._save_settings(settings)
            self._changed()

    def _changed(self) -> None:
        self.refresh()
        self.changed.emit()
