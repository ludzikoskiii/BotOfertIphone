"""Zakładka „Transakcje”: kupione telefony, realny zysk i wnioski z porównania z wyceną (samodoskonalenie)."""
from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape

from PySide6.QtCore import QDate, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDateEdit,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..core.catalog import format_storage, model_names
from ..core.models import Condition, Defect
from ..core.transactions import (
    COST_KINDS,
    STATUSES,
    CostEntry,
    Transaction,
    corrections,
    insights,
    summarize,
)
from ..core.work_time import format_minutes
from ..sources import SOURCE_NAMES
from ..storage.repositories import InventoryRepository, TransactionRepository
from .parts_editor import DATA, ComboDelegate

PART_KINDS = [d for d in Defect if d not in (Defect.NO_POWER, Defect.WATER_DAMAGE, Defect.FACE_ID)]
NO_DATE = QDate(2000, 1, 1)  # „—” w polach dat (brak daty)
HEADERS = ["Data zakupu", "Model", "Pamięć", "Usterki", "Portal", "Status", "Zakup", "Koszty", "Sprzedaż", "Zysk",
           "Czas pracy", "Zysk/h", "Dni do sprzedaży"]


SORT_ROLE = Qt.ItemDataRole.UserRole + 1


class _SortItem(QTableWidgetItem):
    """Komórka sortowana po wartości liczbowej (``SORT_ROLE``), a nie po tekście „1 000 zł”."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        a, b = self.data(SORT_ROLE), other.data(SORT_ROLE)
        if a is not None and b is not None:
            return a < b
        # tekst porównywany wprost — ``super().__lt__`` w PySide wraca do tej metody (nieskończona rekurencja)
        return self.text().casefold() < other.text().casefold()


def zl(value: float | None) -> str:
    return "—" if value is None else f"{value:,.0f} zł".replace(",", " ")


def _date_edit() -> QDateEdit:
    w = QDateEdit(calendarPopup=True)
    w.setDisplayFormat("yyyy-MM-dd")
    w.setMinimumDate(NO_DATE)
    w.setSpecialValueText("—")
    return w


def _set_date(w: QDateEdit, when: datetime | None) -> None:
    if when is None:
        w.setDate(NO_DATE)
        return
    local = when.astimezone()
    w.setDate(QDate(local.year, local.month, local.day))


def _get_date(w: QDateEdit) -> datetime | None:
    d = w.date()
    if d == NO_DATE:
        return None
    return datetime(d.year(), d.month(), d.day(), 12, tzinfo=UTC)


class TransactionDialog(QDialog):
    """Dodanie / edycja transakcji. Zapis (z częściami z magazynu) w ``save``."""

    def __init__(self, tx: Transaction, conn: sqlite3.Connection, settings, parent=None):
        super().__init__(parent)
        self.tx, self.conn, self.settings = tx, conn, settings
        self.repo = TransactionRepository(conn)
        self._ready = False  # podsumowanie liczone dopiero po zbudowaniu wszystkich pól
        self.setWindowTitle("Transakcja" + (f" — {tx.title}" if tx.title else ""))
        self.resize(1000, 700)

        # --- telefon i zakup ---
        left = QFormLayout()
        self.model = QComboBox()
        self.model.setEditable(True)
        self.model.addItems(model_names())
        self.model.setCurrentText(tx.model or "")
        left.addRow("Model:", self.model)
        self.storage = QSpinBox(minimum=0, maximum=2048, singleStep=64, suffix=" GB")
        self.storage.setSpecialValueText("—")
        self.storage.setValue(tx.storage_gb or 0)
        left.addRow("Pamięć:", self.storage)
        self.condition = QComboBox()
        for c in Condition:
            self.condition.addItem(c.label, c.value)
        self.condition.setCurrentIndex(max(0, self.condition.findData(tx.condition.value)))
        left.addRow("Stan:", self.condition)
        self.defects = QListWidget()
        self.defects.setFixedHeight(150)
        for d in Defect:
            item = QListWidgetItem(d.label)
            item.setData(DATA, d.value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if d in tx.defects else Qt.CheckState.Unchecked)
            self.defects.addItem(item)
        left.addRow("Usterki:", self.defects)
        self.source = QComboBox()
        self.source.setEditable(True)
        for key, name in SOURCE_NAMES.items():
            self.source.addItem(name, key)
        i = self.source.findData(tx.source)
        if i >= 0:
            self.source.setCurrentIndex(i)
        else:
            self.source.setCurrentText(tx.source)
        left.addRow("Portal:", self.source)
        self.url = QLineEdit(tx.url)
        left.addRow("Link do oferty:", self.url)
        self.bought_at = _date_edit()
        _set_date(self.bought_at, tx.bought_at or datetime.now(UTC))
        left.addRow("Data zakupu:", self.bought_at)
        self.buy_price = QDoubleSpinBox(maximum=100000, decimals=2, suffix=" zł")
        self.buy_price.setValue(tx.buy_price)
        left.addRow("Cena zakupu:", self.buy_price)
        buy_box = QGroupBox("Telefon i zakup")
        buy_box.setLayout(left)

        # --- naprawa ---
        self.parts = QTableWidget(0, 3)
        self.parts.setHorizontalHeaderLabels(["Część z magazynu", "Ilość", "Cena (Twoja)"])
        self.parts.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.parts.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.parts.verticalHeader().hide()
        self.parts.setItemDelegateForColumn(0, ComboDelegate([(d.label, d.value) for d in PART_KINDS], False, self))
        self.parts.setFixedHeight(110)
        wanted = self.repo.wanted_parts(tx.id) if tx.id is not None else self._suggested_parts()
        for part, qty in wanted:
            self._add_part(part, qty)
        self.parts.itemChanged.connect(lambda _i: self._update_result())
        add_part, del_part = QPushButton("＋ Część"), QPushButton("− Usuń")
        add_part.clicked.connect(lambda: self._add_part(PART_KINDS[0], 1))
        del_part.clicked.connect(lambda: self._remove_row(self.parts))
        self.stock_label = QLabel()
        self.stock_label.setObjectName("muted")
        self.stock_label.setWordWrap(True)
        self.costs = QTableWidget(0, 3)
        self.costs.setHorizontalHeaderLabels(["Rodzaj", "Opis", "Kwota zł"])
        self.costs.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.costs.setColumnWidth(0, 190)
        self.costs.verticalHeader().hide()
        self.costs.setItemDelegateForColumn(0, ComboDelegate([(v, k) for k, v in COST_KINDS.items()], False, self))
        self.costs.setFixedHeight(120)
        for c in tx.costs:
            self._add_cost(c)
        self.costs.itemChanged.connect(lambda _i: self._update_result())
        add_cost, del_cost = QPushButton("＋ Koszt"), QPushButton("− Usuń")
        add_cost.clicked.connect(lambda: self._add_cost(CostEntry("other", "", 0.0)))
        del_cost.clicked.connect(lambda: self._remove_row(self.costs))
        self.repair_minutes = QSpinBox(minimum=0, maximum=100000, singleStep=5, suffix=" min")
        self.repair_minutes.setSpecialValueText("—")
        self.repair_minutes.setValue(tx.repair_minutes or 0)
        self.handling_minutes = QSpinBox(minimum=0, maximum=100000, singleStep=5, suffix=" min")
        self.handling_minutes.setValue(tx.handling_minutes or 0)
        self.handling_minutes.setToolTip("Odbiór, sprawdzenie, wystawienie ogłoszenia, sprzedaż (wstępnie z ustawień)")

        repair = QFormLayout()
        row = QHBoxLayout()
        row.addWidget(add_part)
        row.addWidget(del_part)
        row.addStretch(1)
        repair.addRow(self.parts)
        repair.addRow(row)
        repair.addRow(self.stock_label)
        row2 = QHBoxLayout()
        row2.addWidget(add_cost)
        row2.addWidget(del_cost)
        row2.addStretch(1)
        repair.addRow(QLabel("Inne koszty (wysyłka, prowizje, dojazd, części spoza magazynu):"))
        repair.addRow(self.costs)
        repair.addRow(row2)
        repair.addRow("Faktyczny czas naprawy:", self.repair_minutes)
        repair.addRow("Czas obsługi:", self.handling_minutes)
        repair_box = QGroupBox("Naprawa i koszty")
        repair_box.setLayout(repair)

        # --- sprzedaż ---
        sell = QFormLayout()
        self.status = QComboBox()
        for key, label in STATUSES.items():
            self.status.addItem(label, key)
        self.status.setCurrentIndex(max(0, self.status.findData(tx.status)))
        sell.addRow("Status:", self.status)
        self.listed_at = _date_edit()
        _set_date(self.listed_at, tx.listed_at)
        sell.addRow("Data wystawienia:", self.listed_at)
        self.sold_at = _date_edit()
        _set_date(self.sold_at, tx.sold_at)
        sell.addRow("Data sprzedaży:", self.sold_at)
        self.sell_price = QDoubleSpinBox(maximum=100000, decimals=2, suffix=" zł")
        self.sell_price.setSpecialValueText("—")
        self.sell_price.setValue(tx.sell_price or 0)
        sell.addRow("Cena sprzedaży:", self.sell_price)
        self.sold_where = QComboBox()
        self.sold_where.setEditable(True)
        self.sold_where.addItems(["", *[ch.name for ch in settings.sales_channels]])
        self.sold_where.setCurrentText(tx.sold_where)
        sell.addRow("Gdzie sprzedałem:", self.sold_where)
        self.note = QLineEdit(tx.note)
        sell.addRow("Uwagi:", self.note)
        sell_box = QGroupBox("Sprzedaż")
        sell_box.setLayout(sell)

        self.result = QLabel()
        self.result.setWordWrap(True)
        self.result.setTextFormat(Qt.TextFormat.RichText)
        for w in (self.buy_price, self.sell_price):
            w.valueChanged.connect(lambda _v: self._update_result())
        for w in (self.repair_minutes, self.handling_minutes):
            w.valueChanged.connect(lambda _v: self._update_result())
        self.model.currentTextChanged.connect(lambda _t: self._update_result())

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Zapisz")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Anuluj")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)

        cols = QHBoxLayout()
        cols.addWidget(buy_box, 1)
        right = QVBoxLayout()
        right.addWidget(repair_box)
        right.addWidget(sell_box)
        cols.addLayout(right, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(cols)
        lay.addWidget(self.result)
        lay.addWidget(buttons)
        self._ready = True
        self._update_result()

    # ------------------------------------------------------------ części ---

    def _suggested_parts(self) -> list[tuple[Defect, int]]:
        """Nowa transakcja: usterki, do których masz część na stanie."""
        if not self.settings.inventory.enabled:
            return []
        stock = InventoryRepository(self.conn).stock(self.settings.inventory)
        return [(d, 1) for d in self.tx.defects if d in PART_KINDS and stock.oldest(self.tx.model, d) is not None]

    def _add_part(self, part: Defect, qty: int) -> None:
        self.parts.blockSignals(True)
        r = self.parts.rowCount()
        self.parts.insertRow(r)
        item = QTableWidgetItem(part.label)
        item.setData(DATA, part.value)
        self.parts.setItem(r, 0, item)
        self.parts.setItem(r, 1, QTableWidgetItem(str(qty)))
        price = QTableWidgetItem("")
        price.setFlags(price.flags() & ~Qt.ItemFlag.ItemIsEditable)
        self.parts.setItem(r, 2, price)
        self.parts.blockSignals(False)
        self._update_result()

    def _add_cost(self, c: CostEntry) -> None:
        self.costs.blockSignals(True)
        r = self.costs.rowCount()
        self.costs.insertRow(r)
        kind = QTableWidgetItem(COST_KINDS.get(c.kind, c.kind))
        kind.setData(DATA, c.kind)
        self.costs.setItem(r, 0, kind)
        self.costs.setItem(r, 1, QTableWidgetItem(c.label))
        self.costs.setItem(r, 2, QTableWidgetItem(f"{c.amount:g}"))
        self.costs.blockSignals(False)
        self._update_result()

    def _remove_row(self, table: QTableWidget) -> None:
        r = table.currentRow()
        if r < 0:
            r = table.rowCount() - 1
        if r >= 0:
            table.removeRow(r)
            self._update_result()

    def wanted_parts(self) -> list[tuple[Defect, int]]:
        out = []
        for r in range(self.parts.rowCount()):
            try:
                qty = int(self.parts.item(r, 1).text())
            except (ValueError, AttributeError):
                raise ValueError(f"Niepoprawna ilość części w wierszu {r + 1}.") from None
            if qty > 0:
                out.append((Defect(self.parts.item(r, 0).data(DATA)), qty))
        return out

    def cost_entries(self) -> list[CostEntry]:
        out = []
        for r in range(self.costs.rowCount()):
            text = (self.costs.item(r, 2).text() if self.costs.item(r, 2) else "").replace(",", ".").replace(" ", "")
            try:
                amount = float(text or 0)
            except ValueError:
                raise ValueError(f"Niepoprawna kwota kosztu w wierszu {r + 1}: {text!r}") from None
            label = self.costs.item(r, 1).text().strip() if self.costs.item(r, 1) else ""
            kind = self.costs.item(r, 0).data(DATA) if self.costs.item(r, 0) else "other"
            if amount or label:
                out.append(CostEntry(kind or "other", label, amount))
        return out

    def _parts_estimate(self, wanted: list[tuple[Defect, int]]) -> tuple[float, list[str]]:
        """Koszt części: zużyte już w tej transakcji po ich cenie, nowe — po cenie najstarszej sztuki."""
        inv = InventoryRepository(self.conn)
        model = self.model.currentText().strip() or None
        current = {p: q for p, q in (self.repo.wanted_parts(self.tx.id) if self.tx.id is not None else [])}
        used = {}
        for p in self.tx.parts:
            used.setdefault(p.part, []).append(p)
        stock = inv.stock(self.settings.inventory)
        total, info = 0.0, []
        for r, (part, qty) in enumerate(wanted):
            if current.get(part) == qty and part in used:
                price = sum(p.total for p in used[part])
                text = f"{price:.0f} zł (zużyte)"
            else:
                lot = stock.oldest(model, part)
                have = stock.available(model, part) + current.get(part, 0)
                price = (lot.unit_price * qty) if lot else 0.0
                text = f"~{price:.0f} zł" if lot else "brak na stanie"
                info.append(f"{part.label}: na stanie {have} szt." + (f" (najstarsza {lot.unit_price:.0f} zł)"
                                                                     if lot else ""))
            total += price
            item = self.parts.item(r, 2)
            if item is not None:
                self.parts.blockSignals(True)
                item.setText(text)
                self.parts.blockSignals(False)
        return round(total, 2), info

    # ------------------------------------------------------------ wynik ---

    def _update_result(self) -> None:
        if not self._ready:
            return
        try:
            wanted = self.wanted_parts()
            costs = self.cost_entries()
        except ValueError as e:
            self.result.setText(f'<span style="color:#c92a2a">{escape(str(e))}</span>')
            return
        parts_cost, info = self._parts_estimate(wanted)
        self.stock_label.setText(" · ".join(info) if info else "Części zdejmowane z magazynu po Twojej cenie zakupu "
                                                              "(najstarsza sztuka pierwsza).")
        other = sum(c.amount for c in costs)
        total = self.buy_price.value() + parts_cost + other
        lines = [f"Koszty: zakup {zl(self.buy_price.value())} + części {zl(parts_cost)} + inne {zl(other)} = "
                 f"<b>{zl(total)}</b>"]
        minutes = self.repair_minutes.value() + self.handling_minutes.value()
        if self.sell_price.value() > 0:
            profit = self.sell_price.value() - total
            rate = f" · <b>{profit / (minutes / 60):.0f} zł/h</b>" if minutes else ""
            color = "#2b8a3e" if profit > 0 else "#c92a2a"
            lines.append(f'Realny zysk: <b style="color:{color}">{zl(profit)}</b> · czas {format_minutes(minutes)}'
                         + rate)
        snap = self.tx.snapshot
        if snap is not None:
            parts = [f"werdykt {escape(snap.verdict)}"]
            if snap.resale_shown or snap.resale_value:
                parts.append(f"sprzedaż ~{zl(snap.resale_shown or snap.resale_value)}")
            if snap.repair_cost_shown is not None:
                parts.append(f"naprawa ~{zl(snap.repair_cost_shown)}")
            if snap.profit is not None:
                parts.append(f"zysk {zl(snap.profit)}")
            if snap.repair_minutes is not None or snap.handling_minutes is not None:
                parts.append(f"czas {format_minutes((snap.repair_minutes or 0) + (snap.handling_minutes or 0))}")
            lines.append('<span style="color:gray">Wycena programu przy zakupie: ' + ", ".join(parts) + "</span>")
        self.result.setText("<br>".join(lines))

    # ------------------------------------------------------------- zapis ---

    def read(self) -> Transaction:
        tx = self.tx
        tx.model = self.model.currentText().strip() or None
        tx.storage_gb = self.storage.value() or None
        tx.condition = Condition(self.condition.currentData())
        tx.defects = [Defect(self.defects.item(i).data(DATA)) for i in range(self.defects.count())
                      if self.defects.item(i).checkState() == Qt.CheckState.Checked]
        idx = self.source.findText(self.source.currentText())  # portal z listy → klucz, inaczej wpisany tekst
        tx.source = self.source.itemData(idx) if idx >= 0 else self.source.currentText().strip()
        tx.url = self.url.text().strip()
        tx.bought_at = _get_date(self.bought_at)
        tx.buy_price = self.buy_price.value()
        tx.costs = self.cost_entries()
        tx.repair_minutes = self.repair_minutes.value() or None
        tx.handling_minutes = self.handling_minutes.value() or None
        tx.listed_at = _get_date(self.listed_at)
        tx.sold_at = _get_date(self.sold_at)
        tx.sell_price = self.sell_price.value() or None
        tx.sold_where = self.sold_where.currentText().strip()
        tx.note = self.note.text().strip()
        status = self.status.currentData()
        # status z dat: sprzedaż wpisana → „sprzedany”; data wystawienia → co najmniej „wystawiony”
        if tx.sell_price is not None and tx.sold_at is not None:
            status = "sold"
        elif tx.listed_at is not None and status in ("bought", "repair"):
            status = "listed"
        tx.status = status
        return tx

    def save(self) -> list[Defect]:
        """Zapisuje transakcję i zdejmuje części z magazynu. Zwraca części, których zabrakło (nic nie zapisano)."""
        wanted = self.wanted_parts()
        return self.repo.save_with_parts(self.read(), wanted, self.settings.inventory)

    def _accept(self) -> None:
        try:
            if self.status.currentData() == "sold" and not self.sell_price.value():
                raise ValueError("Status „sprzedany” — wpisz cenę sprzedaży.")
            missing = self.save()
        except ValueError as e:
            QMessageBox.warning(self, "Transakcja", str(e))
            return
        if missing:
            QMessageBox.warning(self, "Transakcja", "Brakuje na stanie: " + ", ".join(d.label for d in missing)
                                + ". Dodaj części w zakładce „Magazyn części” albo wpisz je jako „inne koszty”.")
            return
        self.accept()


class TransactionsTab(QWidget):
    changed = Signal()  # transakcje / poprawki zmienione → nowa wycena ofert (i stan magazynu)

    def __init__(self, conn: sqlite3.Connection, get_settings: Callable, save_settings: Callable, parent=None):
        super().__init__(parent)
        self.conn = conn
        self.repo = TransactionRepository(conn)
        self._get_settings, self._save_settings = get_settings, save_settings
        self._txs: list[Transaction] = []
        self.insight_texts: list[str] = []
        bar = QHBoxLayout()
        for text, slot, name in (("＋ Dodaj transakcję", self.add, "tx_add"), ("Edytuj", self.edit, "tx_edit"),
                                 ("Usuń", self.delete, "tx_delete"), ("Otwórz ogłoszenie ↗", self.open_offer,
                                                                       "tx_open")):
            b = QPushButton(text)
            b.setObjectName(name)
            b.clicked.connect(slot)
            bar.addWidget(b)
        self.status_filter = QComboBox()
        self.status_filter.addItem("Wszystkie", "")
        for key, label in STATUSES.items():
            self.status_filter.addItem(label.capitalize(), key)
        self.status_filter.currentIndexChanged.connect(lambda _i: self.refresh())
        bar.addWidget(QLabel("Pokaż:"))
        bar.addWidget(self.status_filter)
        bar.addStretch(1)
        self.summary = QLabel()
        bar.addWidget(self.summary)

        self.table = QTableWidget(0, len(HEADERS))
        self.table.setHorizontalHeaderLabels(HEADERS)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSortingEnabled(True)
        self.table.doubleClicked.connect(lambda _i: self.edit())

        box = QGroupBox("Wnioski: wycena programu a Twoje wyniki")
        self.learning = QCheckBox("Uwzględniaj poprawki z transakcji w wycenie ofert")
        self.learning.setObjectName("learning_enabled")
        self.learning.setToolTip("Poprawka działa od 3 transakcji danego typu (model + usterki) i rośnie stopniowo "
                                 "z liczbą transakcji. Wyłączone — wnioski i podgląd w szczegółach oferty zostają.")
        self.learning.setChecked(get_settings().learning.enabled)
        self.learning.toggled.connect(self._toggle_learning)
        self.insights = QTextBrowser()
        self.insights.setObjectName("insights")
        box_lay = QVBoxLayout(box)
        box_lay.addWidget(self.learning)
        box_lay.addWidget(self.insights)

        split = QSplitter(Qt.Orientation.Vertical)
        split.addWidget(self.table)
        split.addWidget(box)
        split.setSizes([420, 220])
        lay = QVBoxLayout(self)
        lay.addLayout(bar)
        lay.addWidget(split, 1)
        self.refresh()

    # ------------------------------------------------------------ dane ---

    def refresh(self) -> None:
        settings = self._get_settings()
        all_txs = self.repo.all()
        wanted = self.status_filter.currentData()
        self._txs = [t for t in all_txs if not wanted or t.status == wanted]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self._txs))
        for r, tx in enumerate(self._txs):
            profit = tx.profit
            est = tx.snapshot.profit if (profit is None and tx.snapshot) else None
            values = [
                (tx.bought_at.astimezone().strftime("%Y-%m-%d") if tx.bought_at else "—", None),
                (tx.model or "?", None),
                (format_storage(tx.storage_gb), tx.storage_gb or 0),
                (", ".join(d.label for d in tx.defects) or "bez usterek", None),
                (SOURCE_NAMES.get(tx.source, tx.source), None),
                (tx.status_label, None),
                (zl(tx.buy_price), tx.buy_price),
                (zl(tx.parts_cost + tx.other_costs), tx.parts_cost + tx.other_costs),
                (zl(tx.sell_price), tx.sell_price or 0),
                (zl(profit) if profit is not None else (f"≈ {zl(est)}" if est is not None else "—"),
                 profit if profit is not None else (est if est is not None else -1e9)),
                (format_minutes(tx.minutes), tx.minutes or 0),
                ("—" if tx.profit_per_hour is None else f"{tx.profit_per_hour:.0f} zł/h", tx.profit_per_hour or -1e9),
                ("—" if tx.sell_days is None else f"{tx.sell_days:.0f}", tx.sell_days if tx.sell_days is not None
                 else 1e9),
            ]
            for c, (text, sort) in enumerate(values):
                item = _SortItem(text)
                if sort is not None:
                    item.setData(SORT_ROLE, float(sort))
                item.setData(Qt.ItemDataRole.UserRole, tx.id)
                if c == 9:
                    if profit is not None:
                        item.setForeground(Qt.GlobalColor.darkGreen if profit > 0 else Qt.GlobalColor.red)
                    else:
                        item.setForeground(Qt.GlobalColor.gray)
                        item.setToolTip("Szacowany zysk z wyceny przy zakupie — realny po sprzedaży")
                if c >= 6:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                self.table.setItem(r, c, item)
        self.table.setSortingEnabled(True)
        for c in (0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12):
            self.table.resizeColumnToContents(c)
        s = summarize(all_txs)
        per_hour = f" · średnio <b>{s.per_hour:.0f} zł/h</b>" if s.per_hour is not None else ""
        self.summary.setText(f"Transakcji: <b>{s.count}</b> · sprzedanych: <b>{s.sold}</b> · realny zysk: "
                             f"<b>{zl(s.profit)}</b>{per_hour} · w telefonach na stanie: <b>{zl(s.frozen)}</b>")
        self._render_insights(all_txs, settings)

    def _render_insights(self, txs: list[Transaction], settings) -> None:
        cfg = settings.learning
        items = insights(corrections(txs, cfg), cfg)
        if not items:
            self.insights.setHtml("<p style='color:gray'>Brak jeszcze transakcji do porównania. Kliknij „🛒 Kupiłem” "
                                  "w szczegółach oferty albo „＋ Dodaj transakcję”. Po naprawie i sprzedaży porównam "
                                  f"wycenę z wynikiem; poprawki działają od {cfg.min_transactions} transakcji "
                                  "danego typu (model + usterki).</p>")
            self.insight_texts = []
            return
        html = ["<ul>"]
        for ins in items:
            style = "" if ins.active else " style='color:gray'"
            html.append(f"<li{style}>{escape(ins.text)}</li>")
        html.append("</ul>")
        self.insights.setHtml("".join(html))
        self.insight_texts = [i.text for i in items]

    def _selected(self) -> Transaction | None:
        row = self.table.currentRow()
        if row < 0 or self.table.item(row, 0) is None:
            return None
        tx_id = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
        return next((t for t in self._txs if t.id == tx_id), None)

    # ---------------------------------------------------------- akcje ---

    def open_dialog(self, tx: Transaction, *, dialog: TransactionDialog | None = None) -> bool:
        """Okno transakcji; ``dialog`` (testy) — zapis bez pokazywania okna. True = zapisano."""
        if dialog is None:
            if TransactionDialog(tx, self.conn, self._get_settings(), self).exec() != QDialog.DialogCode.Accepted:
                return False
        elif dialog.save():  # zabrakło części — nic nie zapisano
            return False
        self.refresh()
        self.changed.emit()
        return True

    def add(self) -> None:
        self.open_dialog(Transaction(bought_at=datetime.now(UTC),
                                     handling_minutes=self._default_handling()))

    def _default_handling(self) -> int:
        w = self._get_settings().work
        return w.parcel_minutes + w.check_minutes + w.listing_minutes + w.selling_minutes

    def edit(self) -> None:
        tx = self._selected()
        if tx is not None:
            self.open_dialog(tx)

    def edit_id(self, tx_id: int) -> None:
        tx = self.repo.get(tx_id)
        if tx is not None:
            self.open_dialog(tx)

    def delete(self) -> None:
        tx = self._selected()
        if tx is None:
            return
        box = QMessageBox(QMessageBox.Icon.Question, "Transakcje",
                          f"Usunąć transakcję „{tx.model or '?'}” z {tx.bought_at:%Y-%m-%d}?"
                          if tx.bought_at else "Usunąć transakcję?", parent=self)
        back = box.addButton("Usuń i zwróć części na stan", QMessageBox.ButtonRole.AcceptRole)
        keep = box.addButton("Usuń (części zostają zużyte)", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Anuluj", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        if box.clickedButton() not in (back, keep):
            return
        self.repo.delete(tx.id, return_parts=box.clickedButton() is back)
        self.refresh()
        self.changed.emit()

    def open_offer(self) -> None:
        tx = self._selected()
        if tx is not None and tx.url:
            QDesktopServices.openUrl(QUrl(tx.url))

    def sync_settings(self) -> None:
        """Po zmianie w oknie ustawień: przełącznik poprawek i wnioski."""
        self.learning.blockSignals(True)
        self.learning.setChecked(self._get_settings().learning.enabled)
        self.learning.blockSignals(False)
        self.refresh()

    def _toggle_learning(self, on: bool) -> None:
        settings = self._get_settings()
        settings.learning.enabled = on
        self._save_settings(settings)
        self.refresh()
        self.changed.emit()
