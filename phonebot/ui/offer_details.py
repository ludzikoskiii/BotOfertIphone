"""Okno szczegółów oferty: zdjęcia, pełne wyliczenie, negocjacje, akcje."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTextBrowser,
    QVBoxLayout,
)

from ..core.models import Offer, OfferStatus, Valuation
from ..core.settings import Settings
from ..storage.repositories import OfferRepository
from .details_html import build_details_html
from .images import ThumbnailCache

PHOTO_SIZE = QSize(320, 240)


class OfferDetailsDialog(QDialog):
    status_changed = Signal(int, str)  # offer_id, OfferStatus.value

    def __init__(self, offer: Offer, val: Valuation, settings: Settings, repo: OfferRepository,
                 photos: ThumbnailCache, parent=None):
        super().__init__(parent)
        self.offer, self.val, self.repo, self.photos = offer, val, repo, photos
        self._photo_idx = 0
        self.setWindowTitle(f"Szczegóły — {offer.raw.title}")
        self.resize(1000, 720)

        # --- lewa kolumna: zdjęcia ---
        self.photo = QLabel()
        self.photo.setFixedSize(PHOTO_SIZE)
        self.photo.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prev_btn, self.next_btn = QPushButton("◀"), QPushButton("▶")
        self.photo_label = QLabel()
        self.photo_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.prev_btn.clicked.connect(lambda: self._show_photo(self._photo_idx - 1))
        self.next_btn.clicked.connect(lambda: self._show_photo(self._photo_idx + 1))
        nav = QHBoxLayout()
        nav.addWidget(self.prev_btn)
        nav.addWidget(self.photo_label, 1)
        nav.addWidget(self.next_btn)
        left = QVBoxLayout()
        left.addWidget(self.photo)
        left.addLayout(nav)
        left.addStretch(1)
        photos.ready.connect(self._photo_ready)

        # --- prawa kolumna: raport ---
        self.browser = QTextBrowser()
        self.browser.setOpenExternalLinks(True)
        history = repo.price_history(offer.id) if offer.id else []
        self.browser.setHtml(build_details_html(offer, val, settings, history))

        body = QHBoxLayout()
        body.addLayout(left)
        body.addWidget(self.browser, 1)

        # --- przyciski ---
        self.open_btn = QPushButton("Otwórz ogłoszenie w przeglądarce")
        self.open_btn.setDefault(True)
        self.open_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(offer.raw.url)))
        self.watch_btn = QPushButton()
        self.watch_btn.clicked.connect(self._toggle_watch)
        self.hide_btn = QPushButton()
        self.hide_btn.clicked.connect(self._toggle_hidden)
        close_btn = QPushButton("Zamknij")
        close_btn.clicked.connect(self.accept)
        buttons = QHBoxLayout()
        buttons.addWidget(self.open_btn)
        buttons.addWidget(self.watch_btn)
        buttons.addWidget(self.hide_btn)
        buttons.addStretch(1)
        buttons.addWidget(close_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(body, 1)
        layout.addLayout(buttons)
        self._refresh_buttons()
        self._show_photo(0)

    # --- zdjęcia ---

    def _show_photo(self, idx: int) -> None:
        urls = self.offer.raw.photos
        self._photo_idx = max(0, min(idx, len(urls) - 1)) if urls else 0
        url = urls[self._photo_idx] if urls else None
        self.photo.setPixmap(self.photos.get(url))
        self.photo_label.setText(f"{self._photo_idx + 1} / {len(urls)}" if urls else "brak zdjęć")
        self.prev_btn.setEnabled(self._photo_idx > 0)
        self.next_btn.setEnabled(self._photo_idx < len(urls) - 1)

    def _photo_ready(self, url: str) -> None:
        urls = self.offer.raw.photos
        if urls and urls[self._photo_idx] == url:
            self._show_photo(self._photo_idx)

    # --- status ---

    def _set_status(self, status: OfferStatus) -> None:
        if self.offer.id is None:
            return
        self.repo.set_status(self.offer.id, status)
        self.offer.status = status
        self._refresh_buttons()
        self.status_changed.emit(self.offer.id, status.value)

    def _toggle_watch(self) -> None:
        watched = self.offer.status is OfferStatus.WATCHED
        self._set_status(OfferStatus.NEW if watched else OfferStatus.WATCHED)

    def _toggle_hidden(self) -> None:
        hidden = self.offer.status is OfferStatus.HIDDEN
        self._set_status(OfferStatus.NEW if hidden else OfferStatus.HIDDEN)

    def _refresh_buttons(self) -> None:
        s = self.offer.status
        self.watch_btn.setText("☆ Przestań obserwować" if s is OfferStatus.WATCHED else "★ Obserwuj")
        self.hide_btn.setText("Przywróć (odkryj)" if s is OfferStatus.HIDDEN else "Ukryj ofertę")
