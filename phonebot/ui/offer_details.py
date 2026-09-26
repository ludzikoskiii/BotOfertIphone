"""Szczegóły oferty: zdjęcia, pełne wyliczenie, negocjacje, akcje.

``OfferDetailsView`` to widżet używany w panelu bocznym głównego okna (układ pionowy)
i w osobnym oknie ``OfferDetailsDialog`` (układ poziomy, większe zdjęcie).
"""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QStackedWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ..core.models import Offer, OfferStatus, Valuation
from ..core.settings import Settings
from ..storage.repositories import OfferRepository
from .details_html import build_details_html
from .images import ThumbnailCache
from .style import MARGIN, SPACING

PHOTO_SIZE = QSize(320, 240)
PANEL_PHOTO_SIZE = QSize(272, 204)  # w panelu bocznym zdjęcie jest pomniejszane


NOT_PHONE_CHOICES = (
    ("accessory", "Akcesorium (etui, szkło, ładowarka, pudełko…)"),
    ("part", "Część (wyświetlacz, płyta, bateria…)"),
    ("wanted", "Ogłoszenie kupna / zamiany"),
)


class OfferDetailsView(QWidget):
    status_changed = Signal(int, str)  # offer_id, OfferStatus.value
    full_view_requested = Signal()
    not_phone = Signal(int, str)  # offer_id, klasa (accessory | part | wanted)

    def __init__(self, settings: Settings, repo: OfferRepository, photos: ThumbnailCache, parent=None, *,
                 compact: bool = True):
        super().__init__(parent)
        self.settings, self.repo, self.photos = settings, repo, photos
        self.offer: Offer | None = None
        self.val: Valuation | None = None
        self._photo_idx = 0

        # --- zdjęcia ---
        self.photo = QLabel()
        self._photo_size = PANEL_PHOTO_SIZE if compact else PHOTO_SIZE
        self.photo.setFixedSize(self._photo_size)
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prev_btn, self.next_btn = QPushButton("◀"), QPushButton("▶")
        self.photo_label = QLabel()
        self.photo_label.setObjectName("muted")
        self.photo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prev_btn.clicked.connect(lambda: self._show_photo(self._photo_idx - 1))
        self.next_btn.clicked.connect(lambda: self._show_photo(self._photo_idx + 1))
        nav = QHBoxLayout()
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.photo_label, 1)
        nav.addWidget(self.next_btn)
        photo_box = QVBoxLayout()
        photo_box.setSpacing(SPACING // 2)
        photo_box.addWidget(self.photo, 0, Qt.AlignmentFlag.AlignHCenter)
        photo_box.addLayout(nav)
        photos.ready.connect(self._photo_ready)

        # --- raport ---
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)

        # --- przyciski ---
        self.open_btn = QPushButton("↗ Otwórz" if compact else "Otwórz ogłoszenie ↗")
        self.open_btn.setToolTip("Otwiera ogłoszenie w przeglądarce")
        self.open_btn.clicked.connect(self._open_offer)
        self.compact = compact
        self.watch_btn = QPushButton()
        self.watch_btn.clicked.connect(self._toggle_watch)
        self.hide_btn = QPushButton()
        self.hide_btn.clicked.connect(self._toggle_hidden)
        self.not_phone_btn = QPushButton("✖ Nie telefon" if compact else "✖ To nie jest telefon")
        self.not_phone_btn.setToolTip("Przenosi ofertę do „Odrzucone” i uczy klasyfikator tytułów, "
                                      "że takie ogłoszenia to nie telefony")
        menu = QMenu(self.not_phone_btn)
        for label, text in NOT_PHONE_CHOICES:
            menu.addAction(text, lambda label=label: self._mark_not_phone(label))
        self.not_phone_btn.setMenu(menu)
        buttons = QHBoxLayout()
        buttons.setSpacing(SPACING)
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.watch_btn)
        if not compact:
            buttons.addWidget(self.hide_btn)
            buttons.addWidget(self.not_phone_btn)
        buttons.addStretch(1)
        second = QHBoxLayout()  # w wąskim panelu druga linia przycisków
        second.setSpacing(SPACING)
        if compact:
            full = QPushButton("⤢")
            full.setToolTip("Pełne okno szczegółów (Enter / podwójne kliknięcie)")
            full.clicked.connect(self.full_view_requested.emit)
            buttons.addWidget(full)
            second.addWidget(self.hide_btn)
            second.addWidget(self.not_phone_btn)
            second.addStretch(1)

        content = QWidget()
        if compact:
            body = QVBoxLayout(content)
            body.addLayout(photo_box)
            body.addWidget(self.browser, 1)
        else:
            body = QHBoxLayout(content)
            left = QVBoxLayout()
            left.addLayout(photo_box)
            left.addStretch(1)
            body.addLayout(left)
            body.addWidget(self.browser, 1)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(SPACING)
        body_wrap = QVBoxLayout()
        body_wrap.setContentsMargins(0, 0, 0, 0)
        body_wrap.setSpacing(SPACING)
        body_wrap.addWidget(content, 1)
        body_wrap.addLayout(buttons)
        if compact:
            body_wrap.addLayout(second)
        page = QWidget()
        page.setLayout(body_wrap)

        self.placeholder = QLabel("Wybierz ofertę w tabeli,\naby zobaczyć wyliczenie i negocjacje.")
        self.placeholder.setObjectName("muted")
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.placeholder)
        self.stack.addWidget(page)
        self._page = page
        lay = QVBoxLayout(self)
        lay.setContentsMargins(MARGIN if compact else 0, MARGIN if compact else 0, MARGIN if compact else 0, 0)
        lay.addWidget(self.stack)

    # --- API ---

    def set_offer(self, offer: Offer | None, val: Valuation | None) -> None:
        self.offer, self.val = offer, val
        if offer is None or val is None:
            self.stack.setCurrentWidget(self.placeholder)
            self.browser.clear()
            return
        self.stack.setCurrentWidget(self._page)
        self.render()
        self._refresh_buttons()
        self._show_photo(0)

    def render(self) -> None:
        """Przebudowuje raport (np. po zmianie motywu lub ustawień)."""
        if self.offer is None or self.val is None:
            return
        history = self.repo.price_history(self.offer.id) if self.offer.id else []
        scroll = self.browser.verticalScrollBar().value()
        self.browser.setHtml(build_details_html(self.offer, self.val, self.settings, history))
        self.browser.verticalScrollBar().setValue(scroll)

    def _open_offer(self) -> None:
        if self.offer is not None:
            QDesktopServices.openUrl(QUrl(self.offer.raw.url))

    # --- zdjęcia ---

    def _show_photo(self, idx: int) -> None:
        urls = self.offer.raw.photos if self.offer else []
        self._photo_idx = max(0, min(idx, len(urls) - 1)) if urls else 0
        url = urls[self._photo_idx] if urls else None
        pm = self.photos.get(url)
        if pm.size() != self._photo_size:
            pm = pm.scaled(self._photo_size, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        self.photo.setPixmap(pm)
        self.photo_label.setText(f"{self._photo_idx + 1} / {len(urls)}" if urls else "brak zdjęć")
        self.prev_btn.setEnabled(self._photo_idx > 0)
        self.next_btn.setEnabled(self._photo_idx < len(urls) - 1)

    def _photo_ready(self, url: str) -> None:
        urls = self.offer.raw.photos if self.offer else []
        if urls and urls[self._photo_idx] == url:
            self._show_photo(self._photo_idx)

    # --- status ---

    def _set_status(self, status: OfferStatus) -> None:
        if self.offer is None or self.offer.id is None:
            return
        self.repo.set_status(self.offer.id, status)
        self.offer.status = status
        self._refresh_buttons()
        self.status_changed.emit(self.offer.id, status.value)

    def _mark_not_phone(self, label: str) -> None:
        if self.offer is not None and self.offer.id is not None:
            self.not_phone.emit(self.offer.id, label)

    def _toggle_watch(self) -> None:
        watched = self.offer is not None and self.offer.status is OfferStatus.WATCHED
        self._set_status(OfferStatus.NEW if watched else OfferStatus.WATCHED)

    def _toggle_hidden(self) -> None:
        hidden = self.offer is not None and self.offer.status is OfferStatus.HIDDEN
        self._set_status(OfferStatus.NEW if hidden else OfferStatus.HIDDEN)

    def _refresh_buttons(self) -> None:
        s = self.offer.status if self.offer else OfferStatus.NEW
        watched, hidden = s is OfferStatus.WATCHED, s is OfferStatus.HIDDEN
        if self.compact:
            self.watch_btn.setText("☆ Nie obserwuj" if watched else "★ Obserwuj")
            self.hide_btn.setText("Odkryj" if hidden else "Ukryj")
        else:
            self.watch_btn.setText("☆ Przestań obserwować" if watched else "★ Obserwuj")
            self.hide_btn.setText("Przywróć (odkryj)" if hidden else "Ukryj ofertę")
        self.watch_btn.setToolTip("Obserwowane oferty są wyróżnione w tabeli")
        self.hide_btn.setToolTip("Ukryte oferty nie pokazują się w tabeli (przycisk „Pokaż ukryte”)")


class OfferDetailsDialog(QDialog):
    """Pełne okno szczegółów (podwójne kliknięcie / Enter w tabeli)."""

    status_changed = Signal(int, str)
    not_phone = Signal(int, str)

    def __init__(self, offer: Offer, val: Valuation, settings: Settings, repo: OfferRepository,
                 photos: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Szczegóły — {offer.raw.title}")
        self.resize(1000, 720)
        self.view = OfferDetailsView(settings, repo, photos, self, compact=False)
        self.view.set_offer(offer, val)
        self.view.status_changed.connect(self.status_changed.emit)
        self.view.not_phone.connect(self.not_phone.emit)
        self.view.not_phone.connect(lambda *_: self.accept())  # oferta znika z tabeli — okno też
        self.view.open_btn.setDefault(True)
        self.browser, self.watch_btn, self.hide_btn = self.view.browser, self.view.watch_btn, self.view.hide_btn
        self._toggle_watch, self._toggle_hidden = self.view._toggle_watch, self.view._toggle_hidden
        close_btn = QPushButton("Zamknij")
        close_btn.clicked.connect(self.accept)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(MARGIN, MARGIN, MARGIN, MARGIN)
        lay.addWidget(self.view, 1)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(close_btn)
        lay.addLayout(row)
