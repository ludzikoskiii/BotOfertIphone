"""Główne okno: pasek narzędzi, tabela ofert, status pobierania."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

from PySide6.QtCore import QModelIndex, QSortFilterProxyModel, Qt, QThread, QUrl
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHeaderView,
    QLabel,
    QMainWindow,
    QStackedWidget,
    QTableView,
    QToolBar,
    QWidget,
)

from ..core.models import Mode
from ..net.http import HostRateLimiter, ResponseCache
from ..paths import thumbnails_dir
from ..services.evaluator import Evaluator
from ..services.scanner import ScanReport
from ..storage.repositories import OfferRepository, SettingsRepository
from .images import THUMB_SIZE, ThumbnailCache
from .table_model import SORT_ROLE, Col, OffersTableModel
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

        self.thumbs = ThumbnailCache(thumbs_dir or thumbnails_dir(), self)
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
        view.setAlternatingRowColors(True)
        view.setIconSize(THUMB_SIZE)
        view.verticalHeader().setDefaultSectionSize(THUMB_SIZE.height() + 6)
        view.verticalHeader().hide()
        header = view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        widths = {Col.PHOTO: 84, Col.MODEL: 150, Col.STORAGE: 70, Col.CONDITION: 120, Col.PRICE: 90,
                  Col.MARKET: 115, Col.PROFIT: 90, Col.MAX_BUY: 120, Col.VERDICT: 90, Col.SOURCE: 110,
                  Col.LOCATION: 170, Col.ADDED: 95, Col.LINK: 75}
        for col, w in widths.items():
            view.setColumnWidth(col, w)
        view.doubleClicked.connect(self._open_offer)
        view.clicked.connect(self._cell_clicked)
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
        offers = OfferRepository(self.conn).list()
        rows = Evaluator(self.conn, self.settings).evaluate_all(offers)
        self.model.set_rows(rows)
        self.stack.setCurrentWidget(self.table if rows else self.empty_label)
        self.count_label.setText(f"Ofert: {len(rows)}  ")

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

    def _offer_at(self, index: QModelIndex):
        return self.model.row_at(self.proxy.mapToSource(index).row())[0]

    def _open_offer(self, index: QModelIndex) -> None:
        QDesktopServices.openUrl(QUrl(self._offer_at(index).raw.url))

    def _cell_clicked(self, index: QModelIndex) -> None:
        if index.column() == Col.LINK:
            self._open_offer(index)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        super().closeEvent(event)
