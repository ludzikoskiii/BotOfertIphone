"""Edytor tabeli cen części i usług (zapis do bazy)."""
from __future__ import annotations

from PySide6.QtCore import QAbstractItemModel, QModelIndex, Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.catalog import model_names
from ..core.models import Defect
from ..core.parts import ANY_MODEL, PartPrice
from ..storage.repositories import PartsRepository

ALL = "— wszystkie modele —"
DATA = Qt.ItemDataRole.UserRole
ORIGINAL = Qt.ItemDataRole.UserRole + 1


class ComboDelegate(QStyledItemDelegate):
    """Lista rozwijana tworzona dopiero przy edycji komórki (szybko przy setkach wierszy)."""

    def __init__(self, choices: list[tuple[str, str]], editable: bool, parent=None):
        super().__init__(parent)
        self.choices = choices
        self.editable = editable

    def createEditor(self, parent: QWidget, option: QStyleOptionViewItem, index: QModelIndex) -> QWidget:  # noqa: N802
        combo = QComboBox(parent)
        combo.setEditable(self.editable)
        for label, data in self.choices:
            combo.addItem(label, data)
        return combo

    def setEditorData(self, editor: QWidget, index: QModelIndex) -> None:  # noqa: N802
        i = editor.findData(index.data(DATA))
        if i >= 0:
            editor.setCurrentIndex(i)
        else:
            editor.setCurrentText(index.data() or "")

    def setModelData(self, editor: QWidget, model: QAbstractItemModel, index: QModelIndex) -> None:  # noqa: N802
        text = editor.currentText().strip()
        i = editor.findText(text)
        data = editor.itemData(i) if i >= 0 else text
        model.setData(index, text, Qt.ItemDataRole.DisplayRole)
        model.setData(index, data, DATA)


class PartsEditor(QDialog):
    def __init__(self, repo: PartsRepository, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("Tabela cen części i usług")
        self.resize(760, 620)
        self._deleted: set[tuple[str, str]] = set()

        info = QLabel("Ceny części przy samodzielnej naprawie. Wiersz z modelem „*” to cena domyślna dla modeli "
                      "bez własnego wpisu. Usterki bez ceny (np. Face ID, zalanie) liczone są jako ryzyko "
                      "z ustawień. Wartości startowe są orientacyjne — popraw je według swojego dostawcy.")
        info.setWordWrap(True)

        self.model_filter = QComboBox()
        self.model_filter.addItems([ALL, ANY_MODEL, *model_names()])
        self.model_filter.currentTextChanged.connect(self._apply_filter)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Model", "Część / usługa", "Cena zł", "Uwagi"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSortingEnabled(False)
        models = [(m, m) for m in (ANY_MODEL, *model_names())]
        self.table.setItemDelegateForColumn(0, ComboDelegate(models, editable=True, parent=self))
        self.table.setItemDelegateForColumn(1, ComboDelegate([(d.label, d.value) for d in Defect], False, self))
        for row in repo.all():
            self._add(row)

        add, remove = QPushButton("Dodaj pozycję"), QPushButton("Usuń zaznaczoną")
        add.clicked.connect(self._add_new)
        remove.clicked.connect(self._remove)
        top = QHBoxLayout()
        top.addWidget(QLabel("Pokaż:"))
        top.addWidget(self.model_filter, 1)
        top.addWidget(add)
        top.addWidget(remove)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Zapisz")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Anuluj")
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)

        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addLayout(top)
        lay.addWidget(self.table)
        lay.addWidget(buttons)

    def _add(self, row: PartPrice | None = None) -> int:
        r = self.table.rowCount()
        self.table.insertRow(r)
        model_name = row.model if row else ANY_MODEL
        part = row.part if row else Defect.SCREEN
        model_item = QTableWidgetItem(model_name)
        model_item.setData(DATA, model_name)
        # klucz oryginalny — żeby zmiana modelu/części usuwała stary wpis
        model_item.setData(ORIGINAL, (row.model, row.part.value) if row else None)
        part_item = QTableWidgetItem(part.label)
        part_item.setData(DATA, part.value)
        self.table.setItem(r, 0, model_item)
        self.table.setItem(r, 1, part_item)
        self.table.setItem(r, 2, QTableWidgetItem(f"{row.price:g}" if row else "0"))
        self.table.setItem(r, 3, QTableWidgetItem(row.note if row else ""))
        return r

    def _add_new(self) -> None:
        r = self._add()
        current = self.model_filter.currentText()
        if current != ALL:
            self.table.item(r, 0).setText(current)
            self.table.item(r, 0).setData(DATA, current)
        self.table.scrollToBottom()
        self.table.setCurrentCell(r, 2)

    def _remove(self) -> None:
        r = self.table.currentRow()
        if r < 0:
            return
        original = self.table.item(r, 0).data(ORIGINAL)
        if original:
            self._deleted.add(tuple(original))
        self.table.removeRow(r)

    def _apply_filter(self, model: str) -> None:
        for r in range(self.table.rowCount()):
            self.table.setRowHidden(r, model != ALL and self.table.item(r, 0).text() != model)

    def rows(self) -> list[tuple[PartPrice, tuple[str, str] | None]]:
        out = []
        for r in range(self.table.rowCount()):
            model = self.table.item(r, 0).text().strip() or ANY_MODEL
            part = Defect(self.table.item(r, 1).data(DATA))
            try:
                price = float(self.table.item(r, 2).text().replace(",", "."))
            except ValueError as e:
                raise ValueError(f"Niepoprawna cena w wierszu {r + 1}: {self.table.item(r, 2).text()!r}") from e
            note = self.table.item(r, 3).text() if self.table.item(r, 3) else ""
            out.append((PartPrice(model, part, price, note), self.table.item(r, 0).data(ORIGINAL)))
        return out

    def _save(self) -> None:
        try:
            rows = self.rows()
        except ValueError as e:
            QMessageBox.warning(self, "Błąd", str(e))
            return
        conn = self.repo.conn
        conn.execute("BEGIN")
        try:
            new_keys = {(p.model, p.part.value) for p, _ in rows}
            originals = {tuple(o) for _, o in rows if o}
            for model, part in (self._deleted | originals) - new_keys:
                self.repo.delete(model, Defect(part))
            for p, _ in rows:
                self.repo.upsert(p)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        self.accept()
