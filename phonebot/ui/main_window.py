"""Główne okno: pasek narzędzi, tabela ofert, status pobierania."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QModelIndex, QPoint, QSize, QSortFilterProxyModel, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QDockWidget,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QSystemTrayIcon,
    QTableView,
    QToolBar,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Mode, Offer, OfferStatus, RowColor, Valuation
from ..core.view_filter import ViewFilter, matches
from ..net.http import HostRateLimiter, ResponseCache
from ..paths import thumbnails_dir
from ..services.evaluator import Evaluator
from ..services.scanner import ScanReport
from ..sources import SOURCE_NAMES
from ..storage.repositories import FetchRunRepository, OfferRepository, PartsRepository, SettingsRepository
from .filters_panel import FiltersPanel
from .icons import app_icon
from .images import THUMB_SIZE, ThumbnailCache
from .location_dialog import LocationDialog
from .offer_details import PHOTO_SIZE, OfferDetailsDialog
from .parts_editor import PartsEditor
from .settings_dialog import SettingsDialog
from .source_status import SourceStatusBar
from .table_model import SORT_ROLE, Col, OffersTableModel
from .theme import COLOR_LABEL, ROW_BACKGROUND
from .workers import FuncWorker, ScanWorker, start_in_thread

log = logging.getLogger(__name__)


class OfferFilterProxy(QSortFilterProxyModel):
    """Sortowanie + filtry widoku (``ViewFilter``) bez ponownego wyceniania."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view_filter = ViewFilter()

    def set_view_filter(self, f: ViewFilter) -> None:
        if hasattr(self, "beginFilterChange"):  # Qt ≥ 6.10
            self.beginFilterChange()
            self.view_filter = f
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.view_filter = f
            self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, source_parent: QModelIndex) -> bool:  # noqa: N802
        offer, val = self.sourceModel().row_at(source_row)
        return matches(offer, val, self.view_filter)


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
        self.setWindowIcon(app_icon())
        self.resize(1400, 800)
        self._quitting = False
        self.quit_on_close = False  # ustawiane w app.py; w testach okno nie kończy aplikacji
        self._tray_hint_shown = False
        self._next_refresh: datetime | None = None

        cache_dir = thumbs_dir or thumbnails_dir()
        self.thumbs = ThumbnailCache(cache_dir, self)
        self.photos = ThumbnailCache(cache_dir, self, size=QSize(PHOTO_SIZE))
        self.model = OffersTableModel(self.thumbs, self)
        self.proxy = OfferFilterProxy(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(SORT_ROLE)
        self.proxy.set_view_filter(self.settings.view_filter)

        self._build_toolbar()
        self._build_table()
        self._build_filters()
        self._build_source_status()
        self._status = QLabel("Gotowy.")
        self.statusBar().addWidget(self._status, 1)
        self.auto_label = QLabel()
        self.statusBar().addPermanentWidget(self.auto_label)
        self._build_tray()
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._auto_refresh)
        self._configure_timer()
        self.refresh_source_status()
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
        self.refresh_action.triggered.connect(lambda: self.start_scan(force=True))
        tb.addAction(self.refresh_action)
        tb.addSeparator()

        settings_action = QAction("⚙ Ustawienia", self)
        settings_action.triggered.connect(self.open_settings)
        tb.addAction(settings_action)
        parts_action = QAction("🔧 Tabela części", self)
        parts_action.triggered.connect(self.open_parts_editor)
        tb.addAction(parts_action)
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

    def _build_source_status(self) -> None:
        self.addToolBarBreak()
        tb = QToolBar("Źródła", self)
        tb.setMovable(False)
        self.source_status = SourceStatusBar(SOURCE_NAMES, self)
        self.source_status.diagnose_requested.connect(self.run_diagnosis)
        tb.addWidget(self.source_status)
        self.addToolBar(tb)
        self.refresh_source_status()

    def refresh_source_status(self) -> None:
        """Status źródeł z ostatnich przebiegów zapisanych w bazie."""
        runs = FetchRunRepository(self.conn).latest_by_source()
        for key, name in SOURCE_NAMES.items():
            if not self.settings.enabled_sources.get(key, True):
                self.source_status.set_status(key, name, "disabled")
                continue
            row = runs.get(key)
            if row is None:
                self.source_status.set_status(key, name, "never")
                continue
            when = datetime.fromisoformat(row["finished_at"]) if row["finished_at"] else None
            self.source_status.set_status(key, name, row["status"], found=row["offers_found"],
                                          error=row["error"], when=when)

    def run_diagnosis(self) -> None:
        """Diagnostyka źródeł w tle; raport w oknie i w pliku diagnostyka.txt."""
        if getattr(self, "_diag_worker", None) is not None:
            return
        import asyncio

        from ..diagnose import diagnose, format_report
        from ..paths import data_dir

        settings = self.settings
        self._status.setText("Diagnostyka źródeł… (ok. 30–60 s)")

        def job() -> str:
            report = format_report(asyncio.run(diagnose(settings)))
            (data_dir() / "diagnostyka.txt").write_text(report, encoding="utf-8")
            return report

        worker = FuncWorker(job)
        worker.finished.connect(self._show_diagnosis)
        worker.failed.connect(lambda msg: self._status.setText(f"Diagnostyka nie powiodła się: {msg}"))
        self._diag_worker = worker
        thread = start_in_thread(worker, self)
        thread.finished.connect(lambda: setattr(self, "_diag_worker", None))

    def _show_diagnosis(self, report: str) -> None:
        from ..paths import data_dir

        self._status.setText(f"Diagnostyka zapisana: {data_dir() / 'diagnostyka.txt'}")
        dialog = QDialog(self)
        dialog.setWindowTitle("Diagnostyka źródeł")
        dialog.resize(1000, 700)
        text = QPlainTextEdit(report)
        text.setReadOnly(True)
        text.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        close = QPushButton("Zamknij")
        close.clicked.connect(dialog.accept)
        lay = QVBoxLayout(dialog)
        lay.addWidget(QLabel(f"Raport zapisany w: {data_dir() / 'diagnostyka.txt'} — "
                             "prześlij go, jeśli któreś źródło nie działa."))
        lay.addWidget(text)
        lay.addWidget(close)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        self.last_diagnosis = report

    def _build_tray(self) -> None:
        self.tray: QSystemTrayIcon | None = None
        if not QSystemTrayIcon.isSystemTrayAvailable():
            return
        tray = QSystemTrayIcon(app_icon(), self)
        tray.setToolTip("PhoneBot")
        menu = QMenu(self)
        menu.addAction("Pokaż okno", self.show_from_tray)
        menu.addAction("Odśwież teraz", lambda: self.start_scan(force=True))
        menu.addSeparator()
        menu.addAction("Zakończ", self.quit_app)
        tray.setContextMenu(menu)
        tray.activated.connect(lambda reason: self.show_from_tray()
                               if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        tray.messageClicked.connect(self.show_from_tray)
        tray.show()
        self._tray_menu = menu
        self.tray = tray

    def show_from_tray(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.tray:
            self.tray.setIcon(app_icon())

    def quit_app(self) -> None:
        self._quitting = True
        self.close()

    # ------------------------------------------------ auto-odświeżanie ---

    def _configure_timer(self) -> None:
        minutes = self.settings.refresh_minutes
        if minutes > 0:
            self.refresh_timer.start(minutes * 60_000)
            self._next_refresh = datetime.now() + timedelta(minutes=minutes)
        else:
            self.refresh_timer.stop()
            self._next_refresh = None
        self._update_auto_label()

    def _update_auto_label(self) -> None:
        if self._next_refresh is None:
            self.auto_label.setText("Auto-odświeżanie: wyłączone ")
        else:
            self.auto_label.setText(f"Auto co {self.settings.refresh_minutes} min · "
                                    f"następne {self._next_refresh:%H:%M} ")

    def _auto_refresh(self) -> None:
        self._next_refresh = datetime.now() + timedelta(minutes=self.settings.refresh_minutes)
        self._update_auto_label()
        self.start_scan(force=False)  # automat respektuje pauzę po blokadzie portalu

    def _build_filters(self) -> None:
        self.filters = FiltersPanel(self.settings.view_filter, self.settings.location_name, self)
        self.filters.changed.connect(self._filter_changed)
        self.filters.location_requested.connect(self.change_location)
        dock = QDockWidget("Filtry", self)
        dock.setWidget(self.filters)
        dock.setFeatures(QDockWidget.DockWidgetFeature.DockWidgetMovable
                         | QDockWidget.DockWidgetFeature.DockWidgetClosable)
        self.addDockWidget(Qt.DockWidgetArea.LeftDockWidgetArea, dock)
        self.filters_dock = dock

    # ------------------------------------------------------------- dane ---

    def reload(self) -> None:
        """Wczytuje oferty z bazy i wycenia je w bieżącym trybie."""
        offers = OfferRepository(self.conn).list(include_hidden=self.show_hidden_action.isChecked())
        rows = Evaluator(self.conn, self.settings).evaluate_all(offers)
        self.model.set_rows(rows)
        self.stack.setCurrentWidget(self.table if rows else self.empty_label)
        self._update_count()

    def _update_count(self) -> None:
        rows = self.model.rows()
        shown = self.proxy.rowCount()
        greens = sum(1 for r in range(shown)
                     if self._row_at(self.proxy.index(r, 0))[1].color is RowColor.GREEN)
        total = f" z {len(rows)}" if shown != len(rows) else ""
        self.count_label.setText(f"Ofert: {shown}{total} (zielonych: {greens})  ")

    def _filter_changed(self, f: ViewFilter) -> None:
        self.proxy.set_view_filter(f)
        self.settings.view_filter = f
        self.settings_repo.save(self.settings)
        self._update_count()

    def apply_settings(self, settings) -> None:
        settings.view_filter = self.settings.view_filter
        self.settings = settings
        self.settings_repo.save(settings)
        self.limiter.delay_s = settings.request_delay_s
        self._configure_timer()
        self.refresh_source_status()
        self.filters.set_location_name(settings.location_name)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(settings.mode))
        self.mode_combo.blockSignals(False)
        self.reload()

    def open_settings(self) -> SettingsDialog:
        dialog = SettingsDialog(self.settings, self)
        dialog.parts_editor_requested.connect(self.open_parts_editor)
        dialog.accepted.connect(lambda: self.apply_settings(dialog.result_settings()))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        return dialog

    def open_parts_editor(self) -> PartsEditor:
        editor = PartsEditor(PartsRepository(self.conn), self)
        editor.accepted.connect(self.reload)
        editor.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        editor.open()
        return editor

    def change_location(self) -> LocationDialog:
        s = self.settings
        dialog = LocationDialog(s.location_name, s.home_lat, s.home_lon, self)

        def accepted() -> None:
            place = dialog.place()
            s.location_name, s.home_lat, s.home_lon = place.name, place.lat, place.lon
            self.settings_repo.save(s)
            self.filters.set_location_name(place.name)
            self.reload()

        dialog.accepted.connect(accepted)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        return dialog

    def _mode_changed(self) -> None:
        self.settings.mode = self.mode_combo.currentData()
        self.settings_repo.save(self.settings)
        self.reload()

    # ------------------------------------------------------- pobieranie ---

    def start_scan(self, force: bool = True) -> None:
        if self._thread is not None:
            return
        self.refresh_action.setEnabled(False)
        self._status.setText("Pobieranie ofert…")
        worker = ScanWorker(self.db_path, self.settings, self.limiter, self.cache, force=force)
        worker.progress.connect(self._status.setText)
        worker.finished.connect(self._scan_finished)
        worker.failed.connect(self._scan_failed)
        self._worker = worker
        self._thread = start_in_thread(worker, self)
        self._thread.finished.connect(self._thread_done)

    def _scan_finished(self, report: ScanReport) -> None:
        errors = [f"{s.name}: {s.error}" for s in report.sources if s.error]
        labels = {"blocked": "ZABLOKOWANE", "changed": "ZMIANA FORMATU", "network": "BRAK POŁĄCZENIA",
                  "timeout": "ZA DŁUGO", "empty": "BRAK OFERT"}
        summary = "; ".join(
            f"{s.name}: {s.saved} ofert ({s.new} nowych)" if s.ok and s.kind == "ok"
            else f"{s.name}: {labels.get(s.kind, 'BŁĄD')}" for s in report.sources
        )
        now = datetime.now().astimezone()
        for s in report.sources:
            self.source_status.set_status(s.key, s.name, s.kind, found=s.saved, error=s.error, when=now)
        post = getattr(report, "post", None)
        if post is not None:
            if post.ai_analyzed:
                summary += f"; AI: {post.ai_analyzed} opisów"
            if post.ai_error:
                errors.append(f"Analiza AI: {post.ai_error}")
                summary += "; AI: BŁĄD"
            if post.telegram_error:
                errors.append(post.telegram_error)
                summary += "; Telegram: BŁĄD"
        self._status.setText(f"{datetime.now():%H:%M} · " + (summary or "Brak włączonych portali."))
        self._status.setToolTip("\n".join(errors))
        self.reload()
        if post is not None and post.green:
            self.notify_green(post.green)

    def notify_green(self, green: list) -> None:
        """Powiadomienie na pulpicie o nowych zielonych ofertach."""
        n = len(green)
        title = f"PhoneBot: {n} {'nowa zielona oferta' if n == 1 else 'nowe zielone oferty'}"
        lines = [f"{g.headline} · zysk {g.profit:,.0f} zł".replace(",", " ") if g.profit is not None else g.headline
                 for g in green[:4]]
        if n > 4:
            lines.append(f"…i {n - 4} więcej")
        self.last_notification = (title, "\n".join(lines))
        if self.settings.notify_desktop and self.tray is not None:
            self.tray.showMessage(title, "\n".join(lines), app_icon(), 15_000)
            if not self.isVisible() or self.isMinimized():
                self.tray.setIcon(app_icon(badge=True))
        QApplication.alert(self)

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
        if not self._quitting and self.settings.minimize_to_tray and self.tray is not None:
            # zamknięcie okna chowa aplikację do zasobnika — odświeżanie działa dalej
            event.ignore()
            self.hide()
            if not self._tray_hint_shown:
                self.tray.showMessage("PhoneBot działa w tle",
                                      "Oferty są dalej odświeżane. Kliknij ikonę, aby otworzyć okno.",
                                      app_icon(), 5000)
                self._tray_hint_shown = True
            return
        self.refresh_timer.stop()
        if self.tray is not None:
            self.tray.hide()
        if self.quit_on_close:
            QApplication.quit()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        super().closeEvent(event)
