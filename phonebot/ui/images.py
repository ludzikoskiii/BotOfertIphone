"""Asynchroniczne ładowanie miniatur (QNetworkAccessManager + cache na dysku)."""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

log = logging.getLogger(__name__)

THUMB_SIZE = QSize(72, 54)
MAX_PARALLEL = 4


def placeholder(text: str = "brak\nzdjęcia", size: QSize = THUMB_SIZE) -> QPixmap:
    pm = QPixmap(size)
    pm.fill(QColor("#e9ecef"))
    p = QPainter(pm)
    p.setPen(QColor("#868e96"))
    font = QFont()
    font.setPointSize(7 if size.width() < 150 else 11)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return pm


class ThumbnailCache(QObject):
    """Zwraca miniaturę od razu (z pamięci/dysku) albo placeholder i pobiera ją w tle."""

    ready = Signal(str)

    def __init__(self, cache_dir: Path, parent: QObject | None = None, size: QSize = THUMB_SIZE):
        super().__init__(parent)
        self.cache_dir = cache_dir
        self.size = size
        self._mem: dict[str, QPixmap] = {}
        self._failed: set[str] = set()
        self._queue: list[str] = []
        self._active: set[str] = set()
        self._nam = QNetworkAccessManager(self)
        self._placeholder = placeholder(size=size)
        self._loading = placeholder("…", size)

    def _path(self, url: str) -> Path:
        name = hashlib.sha1(url.encode()).hexdigest()
        return self.cache_dir / f"{name}_{self.size.width()}.jpg"

    def get(self, url: str | None) -> QPixmap:
        if not url or url in self._failed:
            return self._placeholder
        if url in self._mem:
            return self._mem[url]
        path = self._path(url)
        if path.exists():
            pm = QPixmap(str(path))
            if not pm.isNull():
                self._mem[url] = pm
                return pm
        if url not in self._active and url not in self._queue:
            self._queue.append(url)
            self._pump()
        return self._loading

    def _pump(self) -> None:
        while self._queue and len(self._active) < MAX_PARALLEL:
            url = self._queue.pop(0)
            self._active.add(url)
            reply = self._nam.get(QNetworkRequest(QUrl(url)))
            reply.finished.connect(lambda r=reply, u=url: self._done(r, u))

    def _done(self, reply: QNetworkReply, url: str) -> None:
        self._active.discard(url)
        try:
            if reply.error() != QNetworkReply.NetworkError.NoError:
                log.debug("Miniatura %s: %s", url, reply.errorString())
                self._failed.add(url)
            else:
                pm = QPixmap()
                if pm.loadFromData(bytes(reply.readAll())):
                    pm = pm.scaled(self.size, Qt.AspectRatioMode.KeepAspectRatio,
                                   Qt.TransformationMode.SmoothTransformation)
                    pm.save(str(self._path(url)), "JPG", 85)
                    self._mem[url] = pm
                else:
                    self._failed.add(url)
            self.ready.emit(url)
        finally:
            reply.deleteLater()
            self._pump()
