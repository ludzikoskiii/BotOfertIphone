"""Praca w tle: pobieranie ofert w osobnym wątku, żeby GUI nigdy się nie zawieszało."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..core.settings import Settings
from ..net.http import HostRateLimiter, ResponseCache
from ..services.scanner import Scanner
from ..storage.db import connect

log = logging.getLogger(__name__)


class ScanWorker(QObject):
    progress = Signal(str)
    finished = Signal(object)  # ScanReport
    failed = Signal(str)

    def __init__(self, db_path: Path, settings: Settings, limiter: HostRateLimiter, cache: ResponseCache):
        super().__init__()
        self.db_path = db_path
        self.settings = settings
        self.limiter = limiter
        self.cache = cache

    @Slot()
    def run(self) -> None:
        conn = connect(self.db_path)
        try:
            scanner = Scanner(conn, self.settings, self.limiter, self.cache)
            report = asyncio.run(scanner.run(self.progress.emit))
            self.finished.emit(report)
        except Exception as e:  # nie pozwól, by wyjątek zabił wątek bez informacji
            log.exception("Skanowanie nie powiodło się")
            self.failed.emit(str(e) or e.__class__.__name__)
        finally:
            conn.close()


def start_in_thread(worker: QObject, parent: QObject) -> QThread:
    """Uruchamia ``worker.run`` w nowym QThread; wątek i worker sprzątają się same."""
    thread = QThread(parent)
    worker.moveToThread(thread)
    thread.started.connect(worker.run)
    for sig in (worker.finished, worker.failed):  # type: ignore[attr-defined]
        sig.connect(thread.quit)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return thread
