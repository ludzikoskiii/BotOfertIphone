"""Główne okno: filtry po lewej, tabela ofert w środku, szczegóły po prawej, status na dole."""
from __future__ import annotations

import copy
import logging
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import (
    QModelIndex,
    QPoint,
    QSize,
    QSortFilterProxyModel,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
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
    QTabBar,
    QTableView,
    QToolBar,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ..core.dedup import merge_across_portals
from ..core.models import Mode, Offer, OfferStatus, RowColor, Valuation, Verdict
from ..core.selection import SelectionCriteria, is_picked
from ..core.sorting import MAX_LEVELS, level, spec_from_json, spec_to_json
from ..core.text import plural
from ..core.view_filter import ViewFilter, matches
from ..ml.desc_model import text_hash
from ..ml.photo_model import model_ready
from ..ml.seed_data import LABEL_NAMES
from ..ml.text_model import current_classifier
from ..net.http import HostRateLimiter, ResponseCache
from ..paths import models_dir, thumbnails_dir
from ..services.ai_service import DescJob, PhotoJob, labels_count
from ..services.evaluator import Evaluator
from ..services.offer_guard import OfferGuard
from ..services.scanner import ScanReport
from ..sources import REGISTRY, SOURCE_NAMES
from ..storage.repositories import (
    FetchRunRepository,
    LabelRepository,
    OfferRepository,
    PartsRepository,
    RejectedRepository,
    SettingsRepository,
)
from .ai_worker import AiWorker
from .filters_panel import FiltersPanel
from .icons import app_icon
from .images import THUMB_SIZE, ThumbnailCache
from .location_dialog import LocationDialog
from .offer_details import PANEL_PHOTO_SIZE, PHOTO_SIZE, OfferDetailsDialog, OfferDetailsView
from .parts_editor import PartsEditor
from .rejected_dialog import RejectedDialog
from .settings_dialog import SettingsDialog
from .sort_bar import SortBar
from .source_status import SourceStatusBar
from .style import MARGIN, apply_theme, system_prefers_dark
from .table_model import (
    ALWAYS_VISIBLE,
    COL_FIELD,
    DEFAULT_WIDTHS,
    FIELD_COL,
    HEADERS,
    Col,
    OffersTableModel,
    VerdictDelegate,
    col_from_key,
    col_key,
)
from .workers import FuncWorker, ScanWorker, start_in_thread

log = logging.getLogger(__name__)

LIST_ALL, LIST_PICKED = "all", "picked"
LIST_NAMES = {LIST_ALL: "Wszystkie oferty", LIST_PICKED: "Wybrane"}


class OfferFilterProxy(QSortFilterProxyModel):
    """Sortowanie + filtry widoku (``ViewFilter``) bez ponownego wyceniania."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view_filter = ViewFilter()
        self.list_key = LIST_ALL  # „Wszystkie oferty” albo „Wybrane” — filtry i sortowanie wspólne
        self.criteria = SelectionCriteria()

    def set_list(self, key: str, criteria: SelectionCriteria) -> None:
        if hasattr(self, "beginFilterChange"):  # Qt ≥ 6.10
            self.beginFilterChange()
            self.list_key, self.criteria = key, criteria
            self.endFilterChange(QSortFilterProxyModel.Direction.Rows)
        else:
            self.list_key, self.criteria = key, criteria
            self.invalidateFilter()

    def in_list(self, key: str, offer: Offer, val: Valuation) -> bool:
        if key == LIST_PICKED:
            return is_picked(offer, val, self.criteria)
        return offer.active  # nieaktualne oferty są tylko w „Wybrane”

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
        return self.in_list(self.list_key, offer, val) and matches(offer, val, self.view_filter)


class MainWindow(QMainWindow):
    # żądania do wątku lokalnego AI (połączenia kolejkowane: sloty wykonują się w wątku AI)
    ai_start_requested = Signal()
    ai_photos_requested = Signal(list)
    ai_desc_requested = Signal(list)
    ai_retrain_requested = Signal()
    ai_auto_retrain_requested = Signal()

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
        self.ai_worker: AiWorker | None = None  # lokalne AI (start_ai) — w osobnym wątku
        self._ai_thread: QThread | None = None
        self._photo_queued: set[tuple[str, str]] = set()
        self._desc_queued: set[tuple[str, str, str]] = set()  # (portal, id, skrót treści)
        self._settings_dialog: SettingsDialog | None = None

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
        if self.settings.table_list not in LIST_NAMES:
            self.settings.table_list = LIST_ALL
        self.proxy.set_list(self.settings.table_list, self.settings.selection)
        # ostatnie sortowanie tej listy (każda lista pamięta swoje)
        self.model.set_sort_spec(spec_from_json(self.settings.table_sort.get(self.settings.table_list)))

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
        # Telegram: zaległe wiadomości (cisza nocna, limit na godzinę, ponowienia) wysyłane w tle co 5 min
        self._tg_worker = None
        self.telegram_timer = QTimer(self, interval=5 * 60_000)
        self.telegram_timer.timeout.connect(self.flush_telegram)
        self.telegram_timer.start()
        # ceny referencyjne (Refurbed) — raz dziennie w tle; sprawdzane co godzinę, pierwszy raz minutę po starcie
        self._ref_worker = None
        self.reference_timer = QTimer(self, interval=60 * 60_000)
        self.reference_timer.timeout.connect(self.refresh_references)
        self.reference_timer.start()
        QTimer.singleShot(60_000, self.refresh_references)
        self._configure_timer()
        self.refresh_source_status()
        # wersja na telefon (serwer www w tle): włączana w Ustawieniach → Telefon
        self.web = None
        self._web_changes = 0
        self.web_timer = QTimer(self, interval=3000)  # zmiany z telefonu (obserwuj, ukryj…) → odśwież tabelę
        self.web_timer.timeout.connect(self._web_poll)
        self._configure_web()
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
        # sortowanie obsługuje okno (wielopoziomowe: klik = ta kolumna, Shift+klik = kolejny poziom)
        view.setSortingEnabled(False)
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
        header.setSectionsClickable(True)
        header.setSortIndicatorShown(True)
        header.sectionClicked.connect(self._header_clicked)
        header.setToolTip("Klik: sortuj po tej kolumnie (drugi klik odwraca kierunek). "
                          "Shift+klik: dodaj kolumnę jako kolejny poziom sortowania.")
        header.setStretchLastSection(True)
        header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        header.customContextMenuRequested.connect(
            lambda pos: (self._fill_columns_menu(), self.columns_menu.exec(header.mapToGlobal(pos))))
        bold = QFont(view.font())
        bold.setBold(True)
        fm = QFontMetrics(bold)
        # tekst + odstępy + strzałka + numer poziomu sortowania (np. „ ²↓”)
        self._min_widths = {c: fm.horizontalAdvance(HEADERS[c] + " ²↓") + 28 for c in Col}
        # etykieta werdyktu (kropka + tekst) musi się zmieścić w całości, także „DO WERYFIKACJI”
        self._min_widths[Col.VERDICT] = max(self._min_widths[Col.VERDICT],
                                            max(fm.horizontalAdvance(v.value) for v in Verdict) + 34 + 16)
        for col in Col:
            default = max(DEFAULT_WIDTHS[col], self._min_widths[col])
            width = self.settings.column_widths.get(col_key(col), default)
            view.setColumnWidth(col, max(width, self._min_widths[col]) if col is Col.VERDICT else width)
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

        self.sort_bar = SortBar(self)
        self.sort_bar.spec_changed.connect(self.model.set_sort_spec)
        self.model.sort_changed.connect(self._sort_changed)
        self.list_tabs = QTabBar(self)
        self.list_tabs.setObjectName("list_tabs")
        self.list_tabs.setExpanding(False)
        self.list_tabs.setDrawBase(False)
        for key, name in LIST_NAMES.items():
            self.list_tabs.setTabData(self.list_tabs.addTab(name), key)
        self.list_tabs.setTabToolTip(1, "Oferty spełniające Twoje kryteria (Ustawienia → Wybrane) i dodane ręcznie "
                                        "(★ Obserwuj). Oferty, które zniknęły z portalu, są oznaczone ⌛.")
        self.list_tabs.setCurrentIndex(list(LIST_NAMES).index(self.settings.table_list))
        self.list_tabs.currentChanged.connect(self._list_changed)
        self.table_area = QWidget(self)
        area = QVBoxLayout(self.table_area)
        area.setContentsMargins(0, 0, 0, 0)
        area.setSpacing(0)
        area.addWidget(self.list_tabs)
        area.addWidget(self.sort_bar)
        area.addWidget(self.stack, 1)
        self._sort_changed()

    # ------------------------------------------------------------ sortowanie ---

    def _header_clicked(self, section: int) -> None:
        field = COL_FIELD.get(Col(section))
        if field is None:
            return
        spec = list(self.model.sort_spec)
        pos = next((i for i, lv in enumerate(spec) if lv.field == field), None)
        if QApplication.keyboardModifiers() & Qt.KeyboardModifier.ShiftModifier:
            if pos is not None:
                spec[pos] = spec[pos].toggled()
            elif len(spec) < MAX_LEVELS:
                spec.append(level(field))
        elif pos == 0:
            spec[0] = spec[0].toggled()  # drugi klik w tę samą kolumnę odwraca kierunek, dalsze poziomy zostają
        else:
            spec = [level(field)]
        self.model.set_sort_spec(spec)

    def _sort_changed(self) -> None:
        """Pasek sortowania, strzałka w nagłówku i zapis w ustawieniach (pamiętane między uruchomieniami)."""
        spec = self.model.sort_spec
        self.sort_bar.set_spec(spec)
        header = self.table.horizontalHeader()
        col = FIELD_COL.get(spec[0].field) if spec else None
        header.blockSignals(True)
        if col is None:
            header.setSortIndicator(-1, Qt.SortOrder.AscendingOrder)
        else:
            header.setSortIndicator(col, Qt.SortOrder.DescendingOrder if spec[0].descending
                                    else Qt.SortOrder.AscendingOrder)
        header.blockSignals(False)
        saved = spec_to_json(spec)
        key = self.proxy.list_key
        if self.settings.table_sort.get(key) != saved:
            self.settings.table_sort[key] = saved
            self._ui_save_timer.start()

    # ------------------------------------------------ listy: Wszystkie / Wybrane ---

    def current_list(self) -> str:
        return self.proxy.list_key

    def _list_changed(self, index: int) -> None:
        key = self.list_tabs.tabData(index) or LIST_ALL
        selected = self.current_offer_id()
        self.proxy.set_list(key, self.settings.selection)
        self.settings.table_list = key
        self.model.set_sort_spec(spec_from_json(self.settings.table_sort.get(key)))  # sortowanie tej listy
        self._select_offer(selected)
        self._ui_save_timer.start()
        self._update_count()

    def _update_tab_counts(self) -> None:
        """Liczniki zakładek — oferty widoczne po wspólnych filtrach, np. „Wybrane (12)”."""
        counts = dict.fromkeys(LIST_NAMES, 0)
        f = self.proxy.view_filter
        for offer, val in self.model.rows():
            if not matches(offer, val, f):
                continue
            for key in LIST_NAMES:
                counts[key] += self.proxy.in_list(key, offer, val)
        for i, key in enumerate(LIST_NAMES):
            self.list_tabs.setTabText(i, f"{LIST_NAMES[key]} ({counts[key]})")

    def _mark_picked(self) -> None:
        """Oferty, które pierwszy raz trafiły do „Wybrane”, dostają datę (nieaktualne zostają potem na liście)."""
        crit = self.settings.selection
        new = [o for o, v in self.model.rows() if o.active and o.id is not None and o.picked_at is None
               and is_picked(o, v, crit)]
        if new:
            now = datetime.now(UTC)
            OfferRepository(self.conn).mark_picked([o.id for o in new], now)
            for o in new:
                o.picked_at = now

    def set_picked(self, offer_id: int, picked: bool) -> None:
        """Ręczne „Dodaj do Wybranych” (= Obserwuj) / „Usuń z Wybranych” — ma pierwszeństwo przed kryteriami."""
        repo = OfferRepository(self.conn)
        if picked:
            repo.set_status(offer_id, OfferStatus.WATCHED)
        else:
            repo.set_pick_excluded(offer_id, True)
        row = self.model.row_of(offer_id)
        if row is not None:
            offer = self.model.row_at(row)[0]
            offer.pick_excluded = not picked
            if picked:
                offer.status = OfferStatus.WATCHED
            elif offer.status is OfferStatus.WATCHED:
                offer.status = OfferStatus.NEW
            self._status_changed(offer_id, offer.status.value)
            self._mark_picked()

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
        self.details.pick_requested.connect(self.set_picked)
        self.details.full_view_requested.connect(self._details_for_current)
        self.details.not_phone.connect(self.mark_not_phone)
        self.details.message_copied.connect(self._show_status)
        self.table.selectionModel().currentRowChanged.connect(self._current_changed)

    def _build_layout(self) -> None:
        split = QSplitter(Qt.Orientation.Horizontal, self)
        split.addWidget(self.filters)
        split.addWidget(self.table_area)
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
        self.ai_label = QLabel("AI: wyłączone")
        self.ai_label.setObjectName("muted")
        self.ai_label.setToolTip("Lokalne AI: klasyfikator tytułów i analiza zdjęć (działa na Twoim komputerze)")
        self.web_label = QLabel()
        self.web_label.setObjectName("muted")
        self.web_label.hide()
        for w in (self.count_label, self._sep(), self.refresh_label, self._sep(), self.source_status, self._sep(),
                  self.ai_label, self._sep(), self.web_label, self.auto_label):
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
            cls = REGISTRY[key]
            if not self.settings.enabled_sources.get(key, cls.default_enabled):
                self.source_status.set_status(key, name, "disabled")
                continue
            if not cls.configured(self.settings):
                self.source_status.set_status(key, name, "disabled",
                                              error="brak kluczy API — Ustawienia → Portale")
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
        worker.failed.connect(self._diagnosis_failed)  # metoda okna — wykona się w wątku okna
        self._diag_worker = worker
        thread = start_in_thread(worker, self)
        thread.finished.connect(lambda: setattr(self, "_diag_worker", None))

    def _show_status(self, text: str) -> None:
        self._status.setText(text)

    def _diagnosis_failed(self, message: str) -> None:
        self._status.setText(f"Diagnostyka nie powiodła się: {message}")

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
        repo = OfferRepository(self.conn)
        offers = repo.list(include_hidden=self.show_hidden_action.isChecked())
        offers += repo.list_picked_inactive()  # zniknęły z portalu — w „Wybrane” jako nieaktualne
        rows = merge_across_portals(Evaluator(self.conn, self.settings).evaluate_all(offers))
        self.model.set_rows(rows)
        self._mark_picked()
        if getattr(self, "web", None) is not None:
            self.web.app.invalidate()
        self.stack.setCurrentWidget(self.table if rows else self.empty_label)
        self._select_offer(selected)
        self._update_count()
        self._update_rejected_count()
        self._queue_photo_analysis()
        self._queue_desc_analysis()

    # ------------------------------------------------------- lokalne AI ---

    def start_ai(self) -> None:
        """Uruchamia wątek lokalnego AI: modele ładowane raz, potem analiza w tle (wołane z app.py)."""
        if self.ai_worker is not None:
            return
        worker = AiWorker(self.db_path, self.settings, limiter=self.limiter)
        thread = QThread(self)
        worker.moveToThread(thread)
        self.ai_start_requested.connect(worker.start)
        self.ai_photos_requested.connect(worker.analyze)
        self.ai_desc_requested.connect(worker.analyze_desc)
        self.ai_retrain_requested.connect(worker.retrain)
        self.ai_auto_retrain_requested.connect(worker.retrain_if_needed)
        worker.status.connect(self.ai_label.setText)
        # metody okna (nie lambdy): Qt wywoła je w wątku okna — lambda wykonałaby się w wątku AI
        worker.photos_done.connect(self._ai_results_ready)
        worker.text_updated.connect(self._ai_results_ready)
        worker.desc_done.connect(self._ai_results_ready)
        worker.llm_state.connect(self._llm_state)
        worker.photo_model_ready.connect(self._photo_model_state)
        worker.retrained.connect(self._model_retrained)
        thread.finished.connect(worker.close)  # sygnał z wątku AI — połączenie z bazą zamyka ten sam wątek
        thread.finished.connect(worker.deleteLater)
        self.ai_worker, self._ai_thread = worker, thread
        thread.start()
        self.ai_start_requested.emit()

    @Slot(int)
    def _ai_results_ready(self, count: int) -> None:
        if count:
            self._schedule_reload()

    def _schedule_reload(self) -> None:
        """Wyniki AI napływają partiami — przeładuj tabelę najwyżej raz na 1,5 s."""
        if not hasattr(self, "_reload_timer"):
            self._reload_timer = QTimer(self, singleShot=True, interval=1500)
            self._reload_timer.timeout.connect(self.reload)
        if not self._reload_timer.isActive():
            self._reload_timer.start()

    def _queue_photo_analysis(self) -> None:
        """Zdjęcia analizujemy tylko dla ofert KUPUJ / NEGOCJUJ / DO WERYFIKACJI, każdą raz — najlepsze najpierw."""
        if self.ai_worker is None or not self.settings.ml.photo_enabled:
            return
        candidates = []
        for offer, val in self.model.rows():
            key = (offer.raw.source, offer.raw.source_id)
            if val.verdict is Verdict.SKIP or not offer.raw.photos or key in self._photo_queued or not offer.active:
                continue
            if offer.layers is not None and (offer.layers.photo_at is not None or offer.layers.photo_error):
                continue
            self._photo_queued.add(key)
            candidates.append((val.verdict.rank, val.score, offer))
        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
        jobs = [PhotoJob(o.raw.source, o.raw.source_id, o.raw.photos[0], o.id) for _, _, o in candidates]
        if jobs:
            self.ai_photos_requested.emit(jobs)

    def _queue_desc_analysis(self) -> None:
        """Opisy czyta lokalny model językowy (Ollama) — tylko oferty DO WERYFIKACJI, każdą treść raz."""
        if self.ai_worker is None or not self.settings.ml.llm_enabled:
            return
        candidates = []
        for offer, val in self.model.rows():
            if val.verdict is not Verdict.VERIFY or offer.id is None or not offer.active:
                continue
            key = text_hash(offer.raw.title, offer.raw.description)
            if offer.layers is not None and offer.layers.desc_hash == key:
                continue  # ten opis był już czytany (także z błędem)
            if (offer.raw.source, offer.raw.source_id, key) in self._desc_queued:
                continue
            self._desc_queued.add((offer.raw.source, offer.raw.source_id, key))
            candidates.append((val.score, offer))
        candidates.sort(key=lambda c: c[0], reverse=True)
        jobs = [DescJob(o.raw.source, o.raw.source_id, o.id, o.raw.title, o.raw.description, o.raw.url,
                        o.parsed.model) for _, o in candidates]
        if jobs:
            self.ai_desc_requested.emit(jobs)

    @Slot(bool, str)
    def _llm_state(self, ready: bool, message: str) -> None:
        self.ai_label.setToolTip(f"Lokalne AI. Analiza opisów: {message}")
        if not ready:  # Ollama nie działa / brak modelu — te oferty wrócą do kolejki przy kolejnym odświeżeniu
            self._desc_queued.clear()
            self._status.setText(f"Analiza opisów czeka: {message}")

    def _photo_model_state(self, ready: bool) -> None:
        if ready:
            self._queue_photo_analysis()
        else:  # model niedostępny (np. brak internetu przy pierwszym pobraniu) — zlecimy te zdjęcia później
            self._photo_queued.clear()

    def _model_retrained(self, info) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.set_model_info(info)

    def _maybe_retrain(self) -> None:
        """Automatyczne douczanie po uzbieraniu N nowych oznaczeń — wątek AI sprawdza, czy już czas."""
        if self.ai_worker is not None and self.settings.ml.text_enabled:
            self.ai_auto_retrain_requested.emit()

    def mark_not_phone(self, offer_id: int, label: str) -> None:
        """„To nie jest telefon”: oferta do „Odrzucone”, a tytuł do nauki klasyfikatora."""
        row = self.model.row_of(offer_id)
        if row is None:
            return
        offer = self.model.row_at(row)[0]
        LabelRepository(self.conn).add(offer.raw, label, "user")
        RejectedRepository(self.conn).reject_stored(offer, "manual", f"oznaczone ręcznie: {LABEL_NAMES[label]}")
        self._status.setText(f"„{offer.raw.title[:40]}” przeniesiono do „Odrzucone” ({LABEL_NAMES[label]}).")
        self.reload()
        self._maybe_retrain()

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
        self._update_tab_counts()
        rows = [r for r in self.model.rows() if r[0].active]
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
            guard = OfferGuard(self.conn, self.settings)
            moved = guard.refilter_stored()
        except Exception:  # noqa: BLE001 — porządki nie mogą zablokować otwarcia okna
            log.exception("Ponowne sprawdzenie zapisanych ofert nie powiodło się")
            return 0
        parts = []
        if moved:
            parts.append(f"Nowe reguły filtra: {moved} zapisanych ofert przeniesiono do „Odrzucone”.")
        if guard.restored:
            parts.append("+" + plural(guard.restored, "oferta z zagranicy wróciła", "oferty z zagranicy wróciły",
                                      "ofert z zagranicy wróciło") + " na listę.")
        if parts:
            self._status.setText(" ".join(parts))
        return moved

    def apply_settings(self, settings) -> None:
        old = self.settings
        # stan układu zmieniany w oknie głównym (nie w ustawieniach) zostaje bez zmian
        for name in ("view_filter", "hidden_columns", "column_widths", "table_sort", "table_list", "splitter_sizes",
                     "filters_visible", "details_visible"):
            setattr(settings, name, getattr(old, name))
        self.settings = settings
        self.details.settings = settings
        if self.ai_worker is not None:
            self.ai_worker.settings = settings  # działa od razu, także w trakcie analizy zdjęć
        if settings.ml.photo_enabled and not old.ml.photo_enabled:
            self._photo_queued.clear()
        if (settings.ml.llm_enabled, settings.ml.llm_url, settings.ml.llm_model) != (
                old.ml.llm_enabled, old.ml.llm_url, old.ml.llm_model):
            self._desc_queued.clear()
            if self.ai_worker is not None:
                self.ai_worker.reset_llm_check()  # nowy adres/model Ollamy — sprawdź od razu
        if (settings.ui_theme, settings.ui_font_pt) != (old.ui_theme, old.ui_font_pt):
            self.apply_appearance()
        self.settings_repo.save(settings)
        self.limiter.delay_s = settings.request_delay_s
        web_keys = ("web_enabled", "web_port", "web_bind", "web_url", "web_pin_hash")
        if any(getattr(settings, k) != getattr(old, k) for k in web_keys):
            if settings.web_pin_hash != old.web_pin_hash and self.web is not None:
                self.web.app.sessions.revoke_all()  # nowy PIN — telefony muszą zalogować się ponownie
            self._configure_web()
        elif self.web is not None:
            self.web.update_settings(settings)
        if settings.telegram_enabled:
            from ..services.telegram_queue import TelegramQueue

            TelegramQueue(self.conn, settings).ensure_since()  # oferty sprzed włączenia nie trafią na Telegram
        self._configure_timer()
        self.refresh_source_status()
        self.filters.set_location_name(settings.location_name)
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(settings.mode))
        self.mode_combo.blockSignals(False)
        self._apply_filter_rules()
        self.proxy.set_list(self.proxy.list_key, settings.selection)  # nowe kryteria „Wybrane”
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
        clf = current_classifier()
        dialog = SettingsDialog(self.settings, self, false_positives=fp, model_info=clf.info if clf else None,
                                photo_model_ready=model_ready(models_dir()),
                                labels=labels_count(self.conn, self.settings))
        dialog.parts_editor_requested.connect(self.open_parts_editor)
        dialog.retrain_requested.connect(self.ai_retrain_requested.emit)
        dialog.retrain_requested.connect(lambda: dialog.set_training_state("Douczanie w tle…"))
        self._settings_dialog = dialog
        dialog.finished.connect(lambda _r: setattr(self, "_settings_dialog", None))
        dialog.accepted.connect(lambda: self.apply_settings(dialog.result_settings()))
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.open()
        return dialog

    def open_rejected(self) -> RejectedDialog:
        dialog = RejectedDialog(RejectedRepository(self.conn), self)
        dialog.restored.connect(lambda _oid: self.reload())
        dialog.finished.connect(lambda _r: (self._update_rejected_count(), self._maybe_retrain()))
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

    # ------------------------------------------------------ wersja na telefon ---

    def _configure_web(self) -> None:
        from ..web.server import WebError, WebServer

        s = self.settings
        if not s.web_enabled:
            if self.web is not None:
                self.web.stop()
            self.web_timer.stop()
            self.web_label.hide()
            return
        if self.web is None:
            self.web = WebServer(self.db_path, s)
        try:
            url = self.web.start(s)
        except WebError as e:
            self.web_label.setText("📱 Telefon: BŁĄD")
            self.web_label.setToolTip(f"Wersja na telefon nie działa: {e}")
            self.web_label.show()
            self._status.setText(f"Wersja na telefon: {e}")
            return
        self._web_changes = self.web.app.changes
        self.web_timer.start()
        self.web_label.setText("📱 Telefon: działa")
        self.web_label.setToolTip(f"Serwer na {url}" + (f"\nAdres dla telefonu: {s.web_url}" if s.web_url else "")
                                  + "\nDostęp tylko z tego komputera albo przez Tailscale, z PIN-em.")
        self.web_label.show()

    def _web_poll(self) -> None:
        if self.web is not None and self.web.app.changes != self._web_changes:
            self._web_changes = self.web.app.changes
            self.reload()

    def refresh_references(self, force: bool = False) -> bool:
        """Ceny referencyjne w wątku roboczym (raz dziennie; błędy nie przeszkadzają w pracy)."""
        from ..services.reference_prices import due, refresh_in_background

        if self._ref_worker is not None or not self.settings.reference_enabled or not (force or due(self.conn)):
            return False
        worker = FuncWorker(refresh_in_background, self.db_path, copy.deepcopy(self.settings), force)
        worker.finished.connect(self._references_done)
        worker.failed.connect(self._references_done)
        self._ref_worker = worker
        start_in_thread(worker, self)
        return True

    def _references_done(self, result) -> None:
        self._ref_worker = None
        if isinstance(result, dict) and result:
            self.reload()  # nowa cena referencyjna → nowa wycena

    def flush_telegram(self) -> bool:
        """Wysyła zaległe powiadomienia Telegram w wątku roboczym (nigdy dwa naraz). Zwraca, czy uruchomiono."""
        s = self.settings
        if self._tg_worker is not None or not (s.telegram_enabled and s.telegram_bot_token and s.telegram_chat_id):
            return False
        from ..services.telegram_queue import flush_in_background

        worker = FuncWorker(flush_in_background, self.db_path, copy.deepcopy(s))
        worker.finished.connect(self._telegram_flushed)
        worker.failed.connect(self._telegram_flushed)
        self._tg_worker = worker
        start_in_thread(worker, self)
        return True

    def _telegram_flushed(self, result) -> None:
        self._tg_worker = None
        if isinstance(result, str) or getattr(result, "error", None):
            log.warning("Telegram (w tle): %s", getattr(result, "error", result))

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
            if post.error:
                errors.append(f"Po pobraniu: {post.error}")
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
        dialog.not_phone.connect(self.mark_not_phone)
        dialog.message_copied.connect(self._show_status)
        dialog.pick_requested.connect(self.set_picked)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        dialog.show()
        return dialog

    def set_offer_status(self, offer_id: int, status: OfferStatus) -> None:
        OfferRepository(self.conn).set_status(offer_id, status)
        self._status_changed(offer_id, status.value)

    def _status_changed(self, offer_id: int, status: str) -> None:
        st = OfferStatus(status)
        row = self.model.row_of(offer_id)
        if row is not None:  # ukrycie = słaba wskazówka dla klasyfikatora, że to nie telefon
            raw = self.model.row_at(row)[0].raw
            labels = LabelRepository(self.conn)
            if st is OfferStatus.HIDDEN and self.settings.ml.learn_from_hidden:
                labels.add(raw, "accessory", "hidden")
                self._maybe_retrain()
            elif st is not OfferStatus.HIDDEN:
                labels.remove(raw.source, raw.source_id, origin="hidden")
        if st is OfferStatus.HIDDEN and not self.show_hidden_action.isChecked():
            self.reload()
        else:
            self.model.update_status(offer_id, st)
            if st is OfferStatus.WATCHED:
                self._mark_picked()
            self._update_count()
            if self.details.offer is not None and self.details.offer.id == offer_id:
                self.details.offer.status = st
                self.details._refresh_buttons()
                self.details.render()

    def _context_menu(self, pos: QPoint) -> None:
        index = self.table.indexAt(pos)
        if not index.isValid():
            return
        offer, val = self._row_at(index)
        menu = QMenu(self)
        menu.addAction("Szczegóły i wyliczenie…", lambda: self.show_details(index))
        menu.addAction("Otwórz ogłoszenie w przeglądarce", lambda: self._open_offer(index))
        menu.addSeparator()
        if offer.status is OfferStatus.WATCHED:
            menu.addAction("☆ Przestań obserwować", lambda: self.set_offer_status(offer.id, OfferStatus.NEW))
        else:
            menu.addAction("★ Obserwuj", lambda: self.set_offer_status(offer.id, OfferStatus.WATCHED))
        if is_picked(offer, val, self.settings.selection):
            menu.addAction("✕ Usuń z Wybranych", lambda: self.set_picked(offer.id, False))
        else:
            menu.addAction("✓ Dodaj do Wybranych", lambda: self.set_picked(offer.id, True))
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
        if self.web is not None:
            self.web.stop()
        if self.tray is not None:
            self.tray.hide()
        if self.quit_on_close:
            QApplication.quit()
        if self._thread is not None:
            self._thread.quit()
            self._thread.wait(3000)
        if self.ai_worker is not None and self._ai_thread is not None:
            self.ai_worker.stop()  # przerwij analizę zdjęć po bieżącym zdjęciu i pobieranie modelu
            self._ai_thread.quit()
            self._ai_thread.wait(5000)
        super().closeEvent(event)
