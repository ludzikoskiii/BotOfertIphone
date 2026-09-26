"""Widok „Odrzucone oferty”: co filtr wyciął i dlaczego + przycisk „To jest telefon”."""
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ..core.listing_filter import STAGE_LABELS
from ..sources import SOURCE_NAMES
from ..storage.repositories import RejectedRepository, SellerRepository

ID_ROLE = Qt.ItemDataRole.UserRole


class RejectedDialog(QDialog):
    restored = Signal(int)  # id przywróconej oferty

    def __init__(self, repo: RejectedRepository, parent=None):
        super().__init__(parent)
        self.repo = repo
        self.setWindowTitle("Odrzucone oferty")
        self.resize(1100, 620)

        info = QLabel("Ogłoszenia, które filtr uznał za niebędące telefonem na sprzedaż. Jeśli filtr się pomylił, "
                      "zaznacz ofertę i kliknij „To jest telefon” — trafi do wyników, a przyszłe pobrania jej "
                      "nie odrzucą. Słowa kluczowe filtra zmienisz w Ustawieniach → Filtr ogłoszeń.")
        info.setWordWrap(True)
        self.stage_combo = QComboBox()
        self.stage_combo.addItem("Wszystkie powody", None)
        for key, label in STAGE_LABELS.items():
            self.stage_combo.addItem(label, key)
        self.stage_combo.currentIndexChanged.connect(self.reload)
        self.count_label = QLabel()

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["Tytuł", "Cena", "Portal", "Etap", "Powód odrzucenia"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setWordWrap(False)
        self.table.doubleClicked.connect(lambda _i: self.open_selected())

        self.restore_btn = QPushButton("✔ To jest telefon")
        self.restore_btn.setToolTip("Przywróć ofertę do wyników i nie odrzucaj jej w przyszłości")
        self.restore_btn.clicked.connect(self.restore_selected)
        self.seller_ok_btn = QPushButton("✔ Sprzedawca jest w porządku")
        self.seller_ok_btn.setToolTip("Usuń oznaczenie „sprzedawca seryjny” i przywróć wszystkie jego oferty")
        self.seller_ok_btn.clicked.connect(self.trust_selected_seller)
        self.seller_ok_btn.setEnabled(False)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        open_btn = QPushButton("Otwórz ogłoszenie")
        open_btn.clicked.connect(self.open_selected)
        close_btn = QPushButton("Zamknij")
        close_btn.clicked.connect(self.accept)

        top = QHBoxLayout()
        top.addWidget(QLabel("Pokaż:"))
        top.addWidget(self.stage_combo)
        top.addStretch(1)
        top.addWidget(self.count_label)
        buttons = QHBoxLayout()
        buttons.addWidget(self.restore_btn)
        buttons.addWidget(self.seller_ok_btn)
        buttons.addWidget(open_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)
        lay = QVBoxLayout(self)
        lay.addWidget(info)
        lay.addLayout(top)
        lay.addWidget(self.table)
        lay.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        stage = self.stage_combo.currentData()
        items = [r for r in self.repo.list() if stage is None or r.stage == stage]
        self._items = {r.id: r for r in items}
        self.table.setRowCount(len(items))
        for row, r in enumerate(items):
            cells = [r.title, f"{r.price:,.0f} zł".replace(",", " "), SOURCE_NAMES.get(r.source, r.source),
                     STAGE_LABELS.get(r.stage, r.stage), r.reason]
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(ID_ROLE, r.id)
                if col == 1:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                if col == 0:
                    item.setToolTip(r.url)
                self.table.setItem(row, col, item)
        self.table.resizeColumnToContents(1)
        self.count_label.setText(f"Odrzuconych: {len(items)}")
        self.restore_btn.setEnabled(bool(items))

    def _selected_id(self) -> int | None:
        row = self.table.currentRow()
        item = self.table.item(row, 0) if row >= 0 else None
        return item.data(ID_ROLE) if item else None

    def restore_selected(self) -> int | None:
        rid = self._selected_id()
        if rid is None:
            return None
        offer_id = self.repo.restore(rid)
        self.reload()
        if offer_id is not None:
            self.restored.emit(offer_id)
        return offer_id

    def _selection_changed(self) -> None:
        rid = self._selected_id()
        r = self._items.get(rid) if rid is not None else None
        self.seller_ok_btn.setEnabled(bool(r and r.stage == "seller" and r.raw.params.get("seller_id")))

    def trust_selected_seller(self) -> int:
        """Sprzedawca oznaczony jako seryjny przez pomyłkę: zdejmij oznaczenie, przywróć jego oferty."""
        rid = self._selected_id()
        r = self._items.get(rid) if rid is not None else None
        seller_id = r.raw.params.get("seller_id") if r else None
        if not r or not seller_id:
            return 0
        SellerRepository(self.repo.conn).unmark_serial(r.source, str(seller_id))
        restored = 0
        for other in self.repo.list():
            if other.source == r.source and other.stage == "seller" and \
                    str(other.raw.params.get("seller_id")) == str(seller_id):
                offer_id = self.repo.restore(other.id)
                if offer_id is not None:
                    restored += 1
                    self.restored.emit(offer_id)
        self.reload()
        return restored

    def open_selected(self) -> None:
        rid = self._selected_id()
        if rid is not None and rid in self._items:
            QDesktopServices.openUrl(QUrl(self._items[rid].url))
