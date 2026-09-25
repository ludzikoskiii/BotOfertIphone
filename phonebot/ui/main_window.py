"""Główne okno: pasek narzędzi, tabela ofert, status pobierania."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from PySide6.QtCore import QModelIndex, QPoint, QSize, QSortFilterProxyModel, Qt, QThread, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QStackedWidget,
    QTableView,
    QToolBar,
    QWidget,
)

from ..core.models import Mode, Offer, OfferStatus, RowColor, Valuation
from ..net.http import HostRateLimiter, ResponseCache
from ..paths import thumbnails_dir
from ..services.evaluator import Evaluator
from ..services.scanner import ScanReport
from ..storage.repositories import OfferRepository, SettingsRepository
from .images import THUMB_SIZE, ThumbnailCache
from .offer_details import PHOTO_SIZE, OfferDetailsDialog
from .table_model import SORT_ROLE, Col, OffersTableModel
from .theme import COLOR_LABEL, ROW_BACKGROUND
from .workers import ScanWorker, start_in_thread

log = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    def __init__(self, conn: sqlite3.Connection, db_path: Path, thumbs_dir: Path | None = None):
        super().__init__()
        self.conn = conn
        self.db_path = db_path
        self.settings_repo = SettingsRepository(conn)
        self.settings = self.settings_repo.load()
        self.limiter = HostRateLimiter(self.settings.request_delay_s)
        self.cache = ResponseCache()
        self._thread: QThread | None = None
        self._worker: ScanWorker | None = None  # referencja chroni przed usunięciem przez GC

        self.setWindowTitle("PhoneBot — opłacalne iPhone'y")
        self.resize(1400, 800)

        cache_dir = thumbs_dir or thumbnails_dir()
        self.thumbs = ThumbnailCache(cache_dir, self)
        self.photos = ThumbnailCache(cache_dir, self, size=QSize(PHOTO_SIZE))
        self.model = OffersTableModel(self.thumbs, self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)

        self._build_toolbar()
        self._build_table()
        self._status = QLabel("Gotowy.")
        self.statusBar().addWidget(self._status, 1)
        self.reload()

    # ---------------------------------------------------------------- UI ---

    def _build_toolbar(self) -> None:
        tb = QToolBar("Główny", self)
        tb.setMovable(False)
        self.addToolBar(tb)

        tb.addWidget(QLabel(" Tryb: "))
        self.mode_combo = QComboBox()
        for mode in Mode:
            self.mode_combo.addItem(mode.label, mode.value)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(self.settings.mode))
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        tb.addWidget(self.mode_combo)
        tb.addSeparator()

        self.refresh_action = QAction("⟳ Odśwież oferty", self)
        self.refresh_action.setShortcut("F5")
        self.refresh_action.triggered.connect(self.start_scan)
        tb.addAction(self.refresh_action)
        tb.addSeparator()

        self.show_hidden_action = QAction("Pokaż ukryte", self)
        self.show_hidden_action.setCheckable(True)
        self.show_hidden_action.toggled.connect(lambda _checked: self.reload())
        tb.addAction(self.show_hidden_action)
        tb.addSeparator()

        legend = "  ".join(
            f'<span style="background:{ROW_BACKGROUND[c]}">&nbsp;&nbsp;&nbsp;&nbsp;</span> {COLOR_LABEL[c]}'
            for c in RowColor
        )
        tb.addWidget(QLabel(f"&nbsp;{legend}&nbsp;&nbsp;⚑ = czerwone flagi&nbsp;&nbsp;★ = obserwowana"))

        self.count_label = QLabel()
        spacer = QWidget()
        spacer.setSizePolicy(spacer.sizePolicy().horizontalPolicy().Expanding,
                             spacer.sizePolicy().verticalPolicy().Preferred)
        tb.addWidget(spacer)
        tb.addWidget(self.count_label)

    def _build_table(self) -> None:
        view = QTableView(self)
        view.setModel(self.proxy)
        view.setSortingEnabled(True)
        view.sortByColumn(Col.PROFIT, Qt.SortOrder.DescendingOrder)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setAlternatingRowColors(False)
        view.setWordWrap(False)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(self._context_menu)
        view.setStyleSheet("QTableView::item:selected { background: #339af0; color: white; }")
        view.setIconSize(THUMB_SIZE)
        view.verticalHeader().setDefaultSectionSize(THUMB_SIZE.height() + 6)
        view.verticalHeader().hide()
        header = view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        widths = {Col.PHOTO: 84, Col.MODEL: 150, Col.STORAGE: 70, Col.CONDITION: 120, Col.PRICE: 90,
                  Col.MARKET: 115, Col.PROFIT: 90, Col.MAX_BUY: 120, Col.VERDICT: 125, Col.SOURCE: 110,
                  Col.LOCATION: 170, Col.ADDED: 95, Col.LINK: 75}
        for col, w in widths.items():
            view.setColumnWidth(col, w)
        view.doubleClicked.connect(self._double_clicked)
        view.clicked.connect(self._cell_clicked)
        QShortcut(QKeySequence(Qt.Key.Key_Return), view, activated=self._details_for_current)
        self.table = view

        self.empty_label = QLabel("Brak ofert w bazie.\nKliknij „⟳ Odśwież oferty” (F5), aby pobrać ogłoszenia.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_label.setStyleSheet("color: #868e96; font-size: 15px;")
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty_label)
        self.setCentralWidget(self.stack)

    # ------------------------------------------------------------- dane ---

    def reload(self) -> None:
        """Wczytuje oferty z bazy i wycenia je w bieżącym trybie."""
        offers = OfferRepository(self.conn).list(include_hidden=self.show_hidden_action.isChecked())
        rows = Evaluator(self.conn, self.settings).evaluate_all(offers)
        self.model.set_rows(rows)
        self.stack.setCurrentWidget(self.table if rows else self.empty_label)
        greens = sum(1 for _, v in rows if v.color is RowColor.GREEN)
        self.count_label.setText(f"Ofert: {len(rows)} (zielonych: {greens})  ")

    def _mode_changed(self) -> None:
        self.settings.mode = self.mode_combo.currentData()
        self.settings_repo.save(self.settings)
        self.reload()

    # ------------------------------------------------------- pobieranie ---

    def start_scan(self) -> None:
        if self._thread is not None:
            return
        self.refresh_action.setEnabled(False)
        self._status.setText("Pobieranie ofert…")
        worker = ScanWorker(self.db_path, self.settings, self.limiter, self.cache)
        worker.progress.connect(self._status.setText)
        worker.finished.connect(self._scan_finished)
        worker.failed.connect(self._scan_failed)
        self._worker = worker
        self._thread = start_in_thread(worker, self)
        self._thread.finished.connect(self._thread_done)

    def _scan_finished(self, report: ScanReport) -> None:
        errors = [f"{s.name}: {s.error}" for s in report.sources if s.error]
        summary = "; ".join(
            f"{s.name}: {s.saved} ofert ({s.new} nowych)" if s.ok else f"{s.name}: BŁĄD" for s in report.sources
        )
        self._status.setText(summary or "Brak włączonych portali.")
        if errors:
            self._status.setToolTip("\n".join(errors))
        self.reload()

    def _scan_failed(self, message: str) -> None:
        self._status.setText(f"Błąd pobierania: {message}")

    def _thread_done(self) -> None:
        self._thread = None
        self._worker = None
        self.refresh_action.setEnabled(True)

    # ------------------------------------------------------- interakcje ---

    def _row_at(self, index: QModelIndex) -> tuple[Offer, Valuation]:
        return self.model.row_at(self.proxy.mapToSource(index).row())

    def _open_offer(self, index: QModelIndex) -> None:
        QDesktopServices.openUrl(QUrl(self._row_at(index)[0].raw.url))

    def _cell_clicked(self, index: QModelIndex) -> None:
        if index.column() == Col.LINK:
            self._open_offer(index)

    def _double_clicked(self, index: QModelIndex) -> None:
        if index.column() != Col.LINK:
            self.show_details(index)

    def _details_for_current(self) -> None:
        index = self.table.currentIndex()
        if index.isValid():
            self.show_details(index)

    def show_details(self, index: QModelIndex) -> OfferDetailsDialog:
        offer, val = self._row_at(index)
        dialog = OfferDetailsDialog(offer, val, self.settings, OfferRepository(self.conn), self.photos, self)
        dialog.status_changed.connect(self._status_changed)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        return dialog

    def set_offer_status(self, offer_id: int, status: OfferStatus) -> None:
        OfferRepository(self.conn).set_status(offer_id, status)
        self._status_changed(offer_id, status.value)

    def _status_changed(self, offer_id: int, status: str) -> None:
        st = OfferStatus(status)
        if st is OfferStatus.HIDDEN and not self.show_hidden_action.isChecked():
            self.reload()
        else:
            self.model.update_status(offer_id, st)

    def _context_menu(self, pos: QPoint) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        offer, _ = self._row_at(index)
        menu = QMenu(self)
        menu.addAction("Szczegóły i wyliczenie…", lambda: self.show_details(index))
        menu.addAction("Otwórz ogłoszenie w przeglądarce", lambda: self._open_offer(index))
        menu.addSeparator()
        if offer.status is OfferStatus.WATCHED:
            menu.addAction("☆ Przestań obserwować", lambda: self.set_offer_status(offer.id, OfferStatus.NEW))
        else:
            menu.addAction("★ Obserwuj", lambda: self.set_offer_status(offer.id, OfferStatus.WATCHED))
        if offer.status is OfferStatus.HIDDEN:
            menu.addAction("Przywróć (odkryj)", lambda: self.set_offer_status(offer.id, OfferStatus.NEW))
        else:
            menu.addAction("Ukryj ofertę", lambda: self.set_offer_status(offer.id, OfferStatus.HIDDEN))
        menu.exec(self.table.viewport().mapToGlobal(pos))

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        super().closeEvent(event)
