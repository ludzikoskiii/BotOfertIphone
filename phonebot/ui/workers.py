"""Praca w tle: pobieranie ofert w osobnym wątku, żeby GUI nigdy się nie zawieszało."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal, Slot

from ..core.settings import Settings
from ..net.http import HostRateLimiter, ResponseCache
from ..services.post_scan import PostScanResult, run_post_scan
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
            try:
                if self.settings.llm_enabled:
                    self.progress.emit("Analiza opisów przez AI…")
                report.post = run_post_scan(conn, self.settings, report)
            except Exception as e:  # powiadomienia/AI nie mogą zepsuć wyników skanu
                log.exception("Błąd po skanowaniu")
                report.post = PostScanResult(ai_error=str(e))
            self.finished.emit(report)
        except Exception as e:  # nie pozwól, by wyjątek zabił wątek bez informacji
            log.exception("Skanowanie nie powiodło się")
            self.failed.emit(str(e) or e.__class__.__name__)
        finally:
            conn.close()


class FuncWorker(QObject):
    """Uruchamia dowolną funkcję w tle (np. test Telegrama, wyszukiwanie)."""

    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, func, *args):
        super().__init__()
        self.func, self.args = func, args

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(self.func(*self.args))
        except Exception as e:
            log.warning("Zadanie w tle nie powiodło się: %s", e)
            self.failed.emit(str(e) or e.__class__.__name__)


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
