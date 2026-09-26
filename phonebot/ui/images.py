"""Asynchroniczne ładowanie miniatur (QNetworkAccessManager + cache na dysku)."""
from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict, deque
from pathlib import Path

from PySide6.QtCore import QObject, QSize, Qt, QUrl, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest

from .theme import current

log = logging.getLogger(__name__)

THUMB_SIZE = QSize(72, 54)
MAX_PARALLEL = 4
DISK_MAX_AGE_DAYS = 30  # miniatury starsze niż tyle dni są usuwane z dysku


def placeholder(text: str = "brak\nzdjęcia", size: QSize = THUMB_SIZE) -> QPixmap:
    pal = current()
    pm = QPixmap(size)
    pm.fill(QColor(pal.surface_alt))
    p = QPainter(pm)
    p.setPen(QColor(pal.muted))
    font = QFont()
    font.setPointSize(7 if size.width() < 150 else 11)
    p.setFont(font)
    p.drawText(pm.rect(), Qt.AlignmentFlag.AlignCenter, text)
    p.end()
    return pm


class ThumbnailCache(QObject):
    """Zwraca miniaturę od razu (z pamięci/dysku) albo placeholder i pobiera ją w tle."""

    ready = Signal(str)

    def __init__(self, cache_dir: Path, parent: QObject | None = None, size: QSize = THUMB_SIZE,
                 max_in_memory: int | None = None):
        super().__init__(parent)
        self.cache_dir = cache_dir
        self.size = size
        # pamięć ograniczona (LRU): duże zdjęcia zajmują ~300 KB, miniatury ~15 KB
        self.max_in_memory = max_in_memory or (3000 if size.width() <= THUMB_SIZE.width() else 60)
        self._mem: OrderedDict[str, QPixmap] = OrderedDict()
        self._failed: set[str] = set()
        self._queue: deque[str] = deque()
        self._queued: set[str] = set()
        self._active: set[str] = set()
        self._nam = QNetworkAccessManager(self)
        self.restyle()

    def restyle(self) -> None:
        """Placeholdery w kolorach bieżącego motywu."""
        self._placeholder = placeholder(size=self.size)
        self._loading = placeholder("…", self.size)

    def _path(self, url: str) -> Path:
        name = hashlib.sha1(url.encode()).hexdigest()
        return self.cache_dir / f"{name}_{self.size.width()}.jpg"

    def get(self, url: str | None) -> QPixmap:
        if not url or url in self._failed:
            return self._placeholder
        if url in self._mem:
            self._mem.move_to_end(url)
            return self._mem[url]
        path = self._path(url)
        if path.exists():
            pm = QPixmap(str(path))
            if not pm.isNull():
                try:
                    path.touch()  # używana — nie usuwaj przy czyszczeniu
                except OSError:
                    pass
                self._remember(url, pm)
                return pm
        if url not in self._active and url not in self._queued:
            self._queue.append(url)
            self._queued.add(url)
            self._pump()
        return self._loading

    def _remember(self, url: str, pm: QPixmap) -> None:
        self._mem[url] = pm
        self._mem.move_to_end(url)
        while len(self._mem) > self.max_in_memory:
            self._mem.popitem(last=False)

    def prune_disk(self, max_age_days: int = DISK_MAX_AGE_DAYS) -> int:
        """Usuwa z dysku miniatury nieużywane od ``max_age_days`` dni."""
        cutoff = time.time() - max_age_days * 86400
        removed = 0
        try:
            for f in self.cache_dir.glob(f"*_{self.size.width()}.jpg"):
                if f.stat().st_mtime < cutoff:
                    f.unlink(missing_ok=True)
                    removed += 1
        except OSError:
            log.debug("Czyszczenie miniatur nie powiodło się", exc_info=True)
        return removed

    def _pump(self) -> None:
        while self._queue and len(self._active) < MAX_PARALLEL:
            url = self._queue.popleft()
            self._queued.discard(url)
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
                    self._remember(url, pm)
                else:
                    self._failed.add(url)
            self.ready.emit(url)
        finally:
            reply.deleteLater()
            self._pump()
