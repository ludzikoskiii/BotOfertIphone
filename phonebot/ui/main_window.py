"""Główne okno: filtry po lewej, tabela ofert w środku, szczegóły po prawej, status na dole."""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QModelIndex, QPoint, QSize, QSortFilterProxyModel, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import QAction, QDesktopServices, QFont, QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMenu,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Mode, Offer, OfferStatus, RowColor, Valuation
from ..core.view_filter import ViewFilter, matches
from ..net.http import HostRateLimiter, ResponseCache
from ..paths import thumbnails_dir
from ..services.evaluator import Evaluator
from ..services.offer_guard import OfferGuard
from ..services.scanner import ScanReport
from ..sources import SOURCE_NAMES
from ..storage.repositories import (
    FetchRunRepository,
    OfferRepository,
    PartsRepository,
    RejectedRepository,
    SettingsRepository,
)
from .filters_panel import FiltersPanel
from .icons import app_icon
from .images import THUMB_SIZE, ThumbnailCache
from .location_dialog import LocationDialog
from .offer_details import PANEL_PHOTO_SIZE, PHOTO_SIZE, OfferDetailsDialog, OfferDetailsView
from .parts_editor import PartsEditor
from .rejected_dialog import RejectedDialog
from .settings_dialog import SettingsDialog
from .source_status import SourceStatusBar
from .style import MARGIN, apply_theme, system_prefers_dark
from .table_model import (
    ALWAYS_VISIBLE,
    DEFAULT_WIDTHS,
    HEADERS,
    Col,
    OffersTableModel,
    VerdictDelegate,
    col_from_key,
    col_key,
)
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

    def sort(self, column: int, order: Qt.SortOrder = Qt.SortOrder.AscendingOrder) -> None:
        """Sortuje model źródłowy (szybko, w Pythonie); proxy tylko filtruje i zachowuje kolejność."""
        self.sourceModel().sort(column, order)

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

        self.palette_ = apply_theme(self.settings.ui_theme, self.settings.ui_font_pt)
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
        self.proxy.set_view_filter(self.settings.view_filter)

        self._ui_save_timer = QTimer(self, singleShot=True, interval=600)
        self._ui_save_timer.timeout.connect(self._save_ui_state)
        self._build_toolbar()
        self._build_table()
        self._build_filters()
        self._build_details()
        self._build_layout()
        self._build_status_bar()
        self._build_tray()
        hints = QApplication.styleHints()
        if hasattr(hints, "colorSchemeChanged"):
            hints.colorSchemeChanged.connect(self._system_scheme_changed)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self._auto_refresh)
        self._configure_timer()
        self.refresh_source_status()
        self._apply_filter_rules()
        self.reload()
        # sprzątanie starych miniatur po starcie, żeby nie opóźniać otwarcia okna
        QTimer.singleShot(5000, lambda: (self.thumbs.prune_disk(), self.photos.prune_disk()))

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
        self.refresh_action.setToolTip("Pobierz nowe oferty ze wszystkich portali (F5)")
        self.refresh_action.triggered.connect(lambda: self.start_scan(force=True))
        tb.addAction(self.refresh_action)
        tb.addSeparator()

        self.filters_action = QAction("☰ Filtry", self)
        self.filters_action.setCheckable(True)
        self.filters_action.setChecked(self.settings.filters_visible)
        self.filters_action.setShortcut("Ctrl+F")
        self.filters_action.toggled.connect(self._toggle_filters)
        tb.addAction(self.filters_action)
        self.details_action = QAction("▤ Szczegóły", self)
        self.details_action.setCheckable(True)
        self.details_action.setChecked(self.settings.details_visible)
        self.details_action.setShortcut("Ctrl+D")
        self.details_action.toggled.connect(self._toggle_details)
        tb.addAction(self.details_action)
        self.columns_menu = QMenu("Kolumny", self)
        self.columns_menu.aboutToShow.connect(self._fill_columns_menu)
        columns_btn = QToolButton(self)
        columns_btn.setText("▦ Kolumny")
        columns_btn.setToolTip("Wybierz kolumny tabeli (także prawy klik na nagłówku)")
        columns_btn.setMenu(self.columns_menu)
        columns_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        tb.addWidget(columns_btn)
        self.show_hidden_action = QAction("Pokaż ukryte", self)
        self.show_hidden_action.setCheckable(True)
        self.show_hidden_action.setToolTip("Pokaż także oferty, które ukryłeś")
        self.show_hidden_action.toggled.connect(lambda _checked: self.reload())
        tb.addAction(self.show_hidden_action)
        tb.addSeparator()

        self.rejected_action = QAction("🚫 Odrzucone", self)
        self.rejected_action.setToolTip("Ogłoszenia odrzucone przez filtr (akcesoria, części, „kupię”…)")
        self.rejected_action.triggered.connect(self.open_rejected)
        tb.addAction(self.rejected_action)
        parts_action = QAction("🔧 Tabela części", self)
        parts_action.triggered.connect(self.open_parts_editor)
        tb.addAction(parts_action)
        settings_action = QAction("⚙ Ustawienia", self)
        settings_action.triggered.connect(self.open_settings)
        tb.addAction(settings_action)

    def _build_table(self) -> None:
        view = QTableView(self)
        view.setModel(self.proxy)
        view.setSortingEnabled(True)
        view.sortByColumn(Col.PROFIT, Qt.SortOrder.DescendingOrder)
        view.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        view.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        view.setAlternatingRowColors(False)
        view.setShowGrid(False)
        view.setWordWrap(False)
        view.setMouseTracking(False)
        view.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        view.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        view.customContextMenuRequested.connect(self._context_menu)
        view.setItemDelegateForColumn(Col.VERDICT, VerdictDelegate(view))
        view.setIconSize(THUMB_SIZE)
        vh = view.verticalHeader()
        vh.hide()
        vh.setSectionResizeMode(QHeaderView.ResizeMode.Fixed)
        header = view.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setSectionsMovable(True)
        header.setHighlightSections(False)
        header.setStretchLastSection(True)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(
            lambda pos: (self._fill_columns_menu(), self.columns_menu.exec(header.mapToGlobal(pos))))
        bold = QFont(view.font())
        bold.setBold(True)
        fm = QFontMetrics(bold)
        self._min_widths = {c: fm.horizontalAdvance(HEADERS[c]) + 28 for c in Col}  # tekst + odstępy + strzałka
        for col in Col:
            default = max(DEFAULT_WIDTHS[col], self._min_widths[col])
            view.setColumnWidth(col, self.settings.column_widths.get(col_key(col), default))
        hidden = {c for c in map(col_from_key, self.settings.hidden_columns) if c is not None} - ALWAYS_VISIBLE
        for col in Col:
            view.setColumnHidden(col, col in hidden)
        header.sectionResized.connect(self._column_resized)
        view.doubleClicked.connect(self._double_clicked)
        view.clicked.connect(self._cell_clicked)
        QShortcut(QKeySequence(Qt.Key.Key_Return), view, activated=self._details_for_current)
        self.table = view
        self._update_row_height()

        self.empty_label = QLabel("Brak ofert w bazie.\nKliknij „⟳ Odśwież oferty” (F5), aby pobrać ogłoszenia.")
        self.empty_label.setObjectName("muted")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack = QStackedWidget(self)
        self.stack.addWidget(self.table)
        self.stack.addWidget(self.empty_label)

    def _update_row_height(self) -> None:
        text_h = self.table.fontMetrics().height() + 16
        photo = not self.table.isColumnHidden(Col.PHOTO)
        self.table.verticalHeader().setDefaultSectionSize(max(text_h, THUMB_SIZE.height() + 8) if photo else text_h)

    def visible_columns(self) -> list[Col]:
        return [c for c in Col if not self.table.isColumnHidden(c)]

    def _fill_columns_menu(self) -> None:
        self.columns_menu.clear()
        for col in Col:
            act = self.columns_menu.addAction(HEADERS[col])
            act.setCheckable(True)
            act.setChecked(not self.table.isColumnHidden(col))
            act.setEnabled(col not in ALWAYS_VISIBLE)
            act.toggled.connect(lambda checked, c=col: self.set_column_visible(c, checked))
        self.columns_menu.addSeparator()
        self.columns_menu.addAction("Przywróć domyślne kolumny", self.reset_columns)

    def set_column_visible(self, col: Col, visible: bool) -> None:
        if col in ALWAYS_VISIBLE:
            return
        self.table.setColumnHidden(col, not visible)
        if visible and self.table.columnWidth(col) < 30:
            self.table.setColumnWidth(col, max(DEFAULT_WIDTHS[col], self._min_widths[col]))
        self.settings.hidden_columns = [col_key(c) for c in Col if self.table.isColumnHidden(c)]
        if col is Col.PHOTO:
            self._update_row_height()
        self.settings_repo.save(self.settings)

    def reset_columns(self) -> None:
        from ..core.settings import DEFAULT_HIDDEN_COLUMNS

        header = self.table.horizontalHeader()
        for col in Col:
            header.moveSection(header.visualIndex(col), col)
            self.table.setColumnWidth(col, max(DEFAULT_WIDTHS[col], self._min_widths[col]))
            self.table.setColumnHidden(col, col_key(col) in DEFAULT_HIDDEN_COLUMNS)
        self.settings.hidden_columns = list(DEFAULT_HIDDEN_COLUMNS)
        self.settings.column_widths = {}
        self._update_row_height()
        self.settings_repo.save(self.settings)

    def _column_resized(self, index: int, _old: int, new: int) -> None:
        header = self.table.horizontalHeader()
        last = next((header.logicalIndex(v) for v in range(header.count() - 1, -1, -1)
                     if not header.isSectionHidden(header.logicalIndex(v))), None)
        if new > 0 and index != last:  # ostatnia kolumna jest rozciągana — jej szerokość nie jest wyborem
            self.settings.column_widths[col_key(Col(index))] = new
            self._ui_save_timer.start()

    def _build_details(self) -> None:
        self.details = OfferDetailsView(self.settings, OfferRepository(self.conn), self.photos, self)
        self.details.setMinimumWidth(PANEL_PHOTO_SIZE.width() + 2 * MARGIN + 24)
        self.details.status_changed.connect(self._status_changed)
        self.details.full_view_requested.connect(self._details_for_current)
        self.table.selectionModel().currentRowChanged.connect(self._current_changed)

    def _build_layout(self) -> None:
        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(self.filters)
        split.addWidget(self.stack)
        split.addWidget(self.details)
        split.setStretchFactor(0, 0)
        split.setStretchFactor(1, 1)
        split.setStretchFactor(2, 0)
        split.setChildrenCollapsible(False)
        split.setHandleWidth(6)
        sizes = self.settings.splitter_sizes
        split.setSizes(sizes if len(sizes) == 3 and all(x > 0 for x in sizes) else [250, 850, 360])
        split.splitterMoved.connect(lambda *_: self._ui_save_timer.start())
        self.filters.setVisible(self.settings.filters_visible)
        self.details.setVisible(self.settings.details_visible)
        wrap = QWidget()
        lay = QVBoxLayout(wrap)
        lay.setContentsMargins(MARGIN // 2, MARGIN // 2, MARGIN // 2, 0)
        lay.addWidget(split)
        self.splitter = split
        self.setCentralWidget(wrap)

    def _toggle_filters(self, visible: bool) -> None:
        self.filters.setVisible(visible)
        self.settings.filters_visible = visible
        self._ui_save_timer.start()

    def _toggle_details(self, visible: bool) -> None:
        self.details.setVisible(visible)
        self.settings.details_visible = visible
        if visible:
            self._current_changed(self.table.currentIndex())
        self._ui_save_timer.start()

    def _save_ui_state(self) -> None:
        self._ui_save_timer.stop()
        sizes = self.splitter.sizes()
        if all(x > 0 for x in sizes):  # ukryty panel ma rozmiar 0 — zapamiętaj ostatni widoczny układ
            self.settings.splitter_sizes = sizes
        try:
            self.settings_repo.save(self.settings)
        except sqlite3.ProgrammingError:  # baza już zamknięta (zamykanie programu)
            log.debug("Układ okna nie zapisany — baza zamknięta")

    def _current_changed(self, index: QModelIndex, _prev: QModelIndex | None = None) -> None:
        if self.details.isHidden():  # panel wyłączony — nie buduj raportu na darmo
            return
        if index.isValid():
            self.details.set_offer(*self._row_at(index))
        else:
            self.details.set_offer(None, None)

    def _build_status_bar(self) -> None:
        bar = self.statusBar()
        bar.setSizeGripEnabled(False)
        self._status = QLabel("Gotowy.")
        self._status.setMinimumWidth(120)
        bar.addWidget(self._status, 1)
        self.count_label = QLabel()
        self.count_label.setToolTip("Oferty widoczne po filtrach / wszystkie w bazie")
        self.refresh_label = QLabel()
        self.refresh_label.setObjectName("muted")
        self.source_status = SourceStatusBar(SOURCE_NAMES, self)
        self.source_status.diagnose_requested.connect(self.run_diagnosis)
        self.auto_label = QLabel()
        self.auto_label.setObjectName("muted")
        for w in (self.count_label, self._sep(), self.refresh_label, self._sep(), self.source_status, self._sep(),
                  self.auto_label):
            bar.addPermanentWidget(w)
        self.refresh_source_status()

    @staticmethod
    def _sep() -> QLabel:
        lbl = QLabel("·")
        lbl.setObjectName("muted")
        return lbl

    def refresh_source_status(self) -> None:
        """Status źródeł z ostatnich przebiegów zapisanych w bazie."""
        runs = FetchRunRepository(self.conn).latest_by_source()
        finished = [datetime.fromisoformat(r["finished_at"]) for r in runs.values() if r["finished_at"]]
        self.set_last_refresh(max(finished) if finished else None)
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

    def set_last_refresh(self, when: datetime | None) -> None:
        if when is None:
            self.refresh_label.setText("Nie odświeżano")
            return
        local = when.astimezone()
        today = datetime.now().astimezone().date()
        day = "" if local.date() == today else f"{local:%d.%m} "
        self.refresh_label.setText(f"Odświeżono {day}{local:%H:%M}")

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

    # ------------------------------------------------------------- dane ---

    def reload(self) -> None:
        """Wczytuje oferty z bazy i wycenia je w bieżącym trybie."""
        selected = self.current_offer_id()
        offers = OfferRepository(self.conn).list(include_hidden=self.show_hidden_action.isChecked())
        rows = Evaluator(self.conn, self.settings).evaluate_all(offers)
        self.model.set_rows(rows)
        self.stack.setCurrentWidget(self.table if rows else self.empty_label)
        self._select_offer(selected)
        self._update_count()
        self._update_rejected_count()

    def current_offer_id(self) -> int | None:
        index = self.table.currentIndex()
        return self._row_at(index)[0].id if index.isValid() else None

    def _select_offer(self, offer_id: int | None) -> None:
        row = self.model.row_of(offer_id) if offer_id is not None else None
        index = self.proxy.mapFromSource(self.model.index(row, Col.MODEL)) if row is not None else QModelIndex()
        if index.isValid():
            self.table.setCurrentIndex(index)
            self.table.scrollTo(index)
        self._current_changed(self.table.currentIndex())

    def _update_count(self) -> None:
        rows = self.model.rows()
        shown = self.proxy.rowCount()
        greens = sum(1 for r in range(shown)
                     if self._row_at(self.proxy.index(r, 0))[1].color is RowColor.GREEN)
        total = f" z {len(rows)}" if shown != len(rows) else ""
        self.count_label.setText(f"Ofert: <b>{shown}</b>{total} · zielonych: <b>{greens}</b>")

    def _filter_changed(self, f: ViewFilter) -> None:
        selected = self.current_offer_id()
        self.proxy.set_view_filter(f)
        self._select_offer(selected)
        self.settings.view_filter = f
        self.settings_repo.save(self.settings)
        self._update_count()

    def _apply_filter_rules(self) -> int:
        """Nowe reguły filtra (aktualizacja programu lub zmiana ustawień) → sprawdź też zapisane oferty."""
        try:
            moved = OfferGuard(self.conn, self.settings).refilter_stored()
        except Exception:  # noqa: BLE001 — porządki nie mogą zablokować otwarcia okna
            log.exception("Ponowne sprawdzenie zapisanych ofert nie powiodło się")
            return 0
        if moved:
            self._status.setText(f"Nowe reguły filtra: {moved} zapisanych ofert przeniesiono do „Odrzucone”.")
        return moved

    def apply_settings(self, settings) -> None:
        old = self.settings
        # stan układu zmieniany w oknie głównym (nie w ustawieniach) zostaje bez zmian
        for name in ("view_filter", "hidden_columns", "column_widths", "splitter_sizes", "filters_visible",
                     "details_visible"):
            setattr(settings, name, getattr(old, name))
        self.settings = settings
        self.details.settings = settings
        if (settings.ui_theme, settings.ui_font_pt) != (old.ui_theme, old.ui_font_pt):
            self.apply_appearance()
        self.settings_repo.save(settings)
        self.limiter.delay_s = settings.request_delay_s
        self._configure_timer()
        self.refresh_source_status()
        self.filters.set_location_name(settings.location_name)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(settings.mode))
        self.mode_combo.blockSignals(False)
        self._apply_filter_rules()
        self.reload()

    def apply_appearance(self) -> None:
        """Przełącza motyw/czcionkę bez restartu."""
        self.palette_ = apply_theme(self.settings.ui_theme, self.settings.ui_font_pt)
        self.model.set_palette(self.palette_)
        self.source_status.restyle()
        self.thumbs.restyle()
        self.photos.restyle()
        self.details.render()
        self._update_row_height()
        self.table.viewport().update()

    def _system_scheme_changed(self, *_args) -> None:
        if self.settings.ui_theme == "system" and system_prefers_dark() != self.palette_.dark:
            self.apply_appearance()

    def open_settings(self) -> SettingsDialog:
        fp = RejectedRepository(self.conn).false_positives_by_keyword()
        dialog = SettingsDialog(self.settings, self, false_positives=fp)
        dialog.parts_editor_requested.connect(self.open_parts_editor)
        dialog.accepted.connect(lambda: self.apply_settings(dialog.result_settings()))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        return dialog

    def open_rejected(self) -> RejectedDialog:
        dialog = RejectedDialog(RejectedRepository(self.conn), self)
        dialog.restored.connect(lambda _oid: self.reload())
        dialog.finished.connect(lambda _r: self._update_rejected_count())
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        return dialog

    def _update_rejected_count(self) -> None:
        n = RejectedRepository(self.conn).count()
        self.rejected_action.setText(f"🚫 Odrzucone ({n})")

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
        self.set_last_refresh(now)
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
            if self.details.offer is not None and self.details.offer.id == offer_id:
                self.details.offer.status = st
                self.details._refresh_buttons()
                self.details.render()

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
        self._save_ui_state()
        if self.tray is not None:
            self.tray.hide()
        if self.quit_on_close:
            QApplication.quit()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        super().closeEvent(event)
