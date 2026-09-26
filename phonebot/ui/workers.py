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

    def __init__(self, db_path: Path, settings: Settings, limiter: HostRateLimiter, cache: ResponseCache,
                 force: bool = False):
        super().__init__()
        self.force = force
        self.db_path = db_path
        self.settings = settings
        self.limiter = limiter
        self.cache = cache

    @Slot()
    def run(self) -> None:
        conn = connect(self.db_path)
        try:
            scanner = Scanner(conn, self.settings, self.limiter, self.cache)
            report = asyncio.run(scanner.run(self.progress.emit, force=self.force))
            try:
                report.post = run_post_scan(conn, self.settings, report)
            except Exception as e:  # powiadomienia nie mogą zepsuć wyników skanu
                log.exception("Błąd po skanowaniu")
                report.post = PostScanResult(error=str(e) or e.__class__.__name__)
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


class OllamaPullWorker(QObject):
    """Pobiera model do Ollamy (kilka GB) z postępem; przerywany przy zamknięciu okna ustawień."""

    progress = Signal(str)
    finished = Signal(object)
    failed = Signal(str)

    def __init__(self, url: str, model: str):
        super().__init__()
        self.url, self.model = url, model
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    @Slot()
    def run(self) -> None:
        from ..ml.ollama import OllamaClient, OllamaError

        def report(status: str, done: int, total: int) -> None:
            if total:
                self.progress.emit(f"⏳ {self.model}: {done / total:.0%} z {total / 1e9:.1f} GB")
            elif status:
                self.progress.emit(f"⏳ {self.model}: {status}")

        try:
            with OllamaClient(self.url) as client:
                client.pull(self.model, report, stop=lambda: self._stop)
            self.finished.emit(self.model)
        except OllamaError as e:
            self.failed.emit(str(e))
        except Exception as e:  # noqa: BLE001
            log.exception("Pobieranie modelu Ollamy nie powiodło się")
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
