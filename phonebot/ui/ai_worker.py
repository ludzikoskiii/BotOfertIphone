"""Wątek lokalnego AI: modele ładowane raz przy starcie, analiza zdjęć i douczanie w tle.

Wszystkie ciężkie operacje (trening, pobranie modelu zdjęć, CLIP) dzieją się tutaj, w osobnym wątku
z własnym połączeniem z bazą — okno programu nie zawiesza się. Zdjęcia są analizowane porcjami, więc
między porcjami wątek obsługuje inne polecenia (np. „Douczyć model”) i szybko reaguje na zamknięcie.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ..core.settings import Settings
from ..services.ai_service import AiService, PhotoJob
from ..storage.db import connect

log = logging.getLogger(__name__)

PHOTO_RETRY_S = 15 * 60  # po nieudanym pobraniu/wczytaniu modelu zdjęć nie próbuj częściej
PHOTO_CHUNK = 5  # zdjęć na porcję


class AiWorker(QObject):
    status = Signal(str)  # krótki opis stanu do paska statusu
    text_updated = Signal(int)  # ile ofert dostało wynik klasyfikatora tytułów (-1 = wszystkie, po treningu)
    photos_done = Signal(int)  # ile zdjęć przeanalizowano w tej porcji
    retrained = Signal(object)  # ModelInfo
    photo_model_ready = Signal(bool)

    def __init__(self, db_path: Path, settings: Settings, models_directory: Path | None = None):
        super().__init__()
        self.db_path = db_path
        # Ustawienia podmienia okno programu (przypisanie referencji jest bezpieczne między wątkami),
        # więc wyłączenie analizy zdjęć działa od razu, także w trakcie długiej kolejki.
        self.settings = settings
        self.models_directory = models_directory
        self._conn = None
        self._stop = False
        self._photo_ready = False
        self._photo_failed_at: float | None = None
        self._pending: deque[PhotoJob] = deque()
        self._chunk_scheduled = False
        self._batch_total = 0
        self._batch_done = 0

    # --- pomocnicze (w wątku roboczym) ---

    def _service(self) -> AiService:
        if self._conn is None:
            self._conn = connect(self.db_path)
        return AiService(self._conn, self.settings, self.models_directory)

    def stop(self) -> None:
        """Wołane z wątku GUI przy zamykaniu — przerywa analizę zdjęć i pobieranie modelu."""
        self._stop = True

    def _photos_off(self) -> bool:
        return self._stop or not self.settings.ml.photo_enabled

    # --- sloty (wykonywane w wątku roboczym) ---

    @Slot()
    def start(self) -> None:
        """Ładuje modele raz przy starcie programu."""
        svc = self._service()
        try:
            if self.settings.ml.text_enabled:
                self.status.emit("AI: przygotowanie klasyfikatora tytułów…")
                clf = svc.text_classifier()
                if clf is None:  # pierwsze uruchomienie: trening na zbiorze startowym i danych z bazy
                    self.status.emit("AI: pierwszy trening klasyfikatora tytułów (kilka sekund)…")
                    clf = svc.text_classifier(train_if_missing=True)
                    self.text_updated.emit(-1)
                else:
                    self.text_updated.emit(svc.update_text_predictions())
                if clf is not None:
                    self.status.emit(f"AI: klasyfikator tytułów gotowy ({clf.info.accuracy:.0%} trafień)")
                self.retrain_if_needed()
        except Exception as e:  # noqa: BLE001
            log.exception("Klasyfikator tytułów nie wystartował")
            self.status.emit(f"AI: błąd klasyfikatora tytułów — {e}")
        if self.settings.ml.photo_enabled:
            self._load_photo_model()

    def _load_photo_model(self) -> bool:
        from ..ml.photo_model import MODEL_SIZE_MB, PhotoModelError, get_photo_classifier

        if self._photo_failed_at is not None and time.monotonic() - self._photo_failed_at < PHOTO_RETRY_S:
            self.photo_model_ready.emit(False)  # okno zleci te zdjęcia ponownie przy następnym odświeżeniu
            return False

        def progress(done: int, total: int) -> None:
            if self._stop:
                raise PhotoModelError("przerwano — program jest zamykany")
            if total:
                self.status.emit(f"AI: pobieranie modelu zdjęć {done / total:.0%} z {MODEL_SIZE_MB} MB (raz)…")

        try:
            self.status.emit("AI: ładowanie modelu zdjęć…")
            get_photo_classifier(self.models_directory, progress=progress)
        except PhotoModelError as e:
            log.warning("Model zdjęć niedostępny: %s", e)
            self._photo_failed_at = time.monotonic()
            self.status.emit(f"AI: analiza zdjęć niedostępna — {e}")
            self.photo_model_ready.emit(False)
            return False
        self._photo_ready, self._photo_failed_at = True, None
        self.status.emit("AI: modele gotowe")
        self.photo_model_ready.emit(True)
        return True

    @Slot(list)
    def analyze(self, jobs: list) -> None:
        """Dodaje główne zdjęcia (lista ``PhotoJob``) do kolejki analizy."""
        if not jobs or self._photos_off():
            return
        if not self._photo_ready and not self._load_photo_model():
            return
        if not self._pending:
            self._batch_total = self._batch_done = 0
        self._pending.extend(j if isinstance(j, PhotoJob) else PhotoJob(*j) for j in jobs)
        self._batch_total += len(jobs)
        self._schedule_chunk()

    def _schedule_chunk(self) -> None:
        if not self._chunk_scheduled and self._pending:
            self._chunk_scheduled = True
            QTimer.singleShot(0, self.process_chunk)

    @Slot()
    def process_chunk(self) -> int:
        """Analizuje jedną porcję zdjęć z kolejki. Zwraca liczbę przeanalizowanych."""
        self._chunk_scheduled = False
        if not self._pending:
            return 0
        if self._photos_off():
            self._pending.clear()
            return 0
        chunk = [self._pending.popleft() for _ in range(min(PHOTO_CHUNK, len(self._pending)))]

        def progress(i: int, _n: int) -> None:
            self.status.emit(f"AI: analiza zdjęć {self._batch_done + i}/{self._batch_total}")

        try:
            done = self._service().analyze_photos(chunk, stop=self._photos_off, progress=progress)
        except Exception as e:  # noqa: BLE001
            log.exception("Analiza zdjęć nie powiodła się")
            self.status.emit(f"AI: błąd analizy zdjęć — {e}")
            self._pending.clear()
            return 0
        self._batch_done += done
        if done:
            self.photos_done.emit(done)
        if self._pending:
            self._schedule_chunk()
        elif self._batch_done:
            self.status.emit(f"AI: przeanalizowano {self._batch_done} zdjęć")
        return done

    @Slot()
    def retrain_if_needed(self) -> None:
        """Automatyczne douczanie: po [N] nowych oznaczeniach albo wielu nowych odrzuconych ofertach."""
        try:
            needed = self.settings.ml.text_enabled and self._service().needs_retraining()
        except Exception:  # noqa: BLE001
            log.exception("Nie udało się sprawdzić, czy douczyć model")
            return
        if needed:
            self.retrain()

    @Slot()
    def retrain(self) -> None:
        svc = self._service()
        self.status.emit("AI: douczanie klasyfikatora tytułów…")
        try:
            info = svc.retrain()
        except Exception as e:  # noqa: BLE001
            log.exception("Douczanie nie powiodło się")
            self.status.emit(f"AI: douczanie nie powiodło się — {e}")
            return
        self.status.emit(f"AI: model douczony — {info.accuracy:.0%} trafień na danych testowych")
        self.retrained.emit(info)
        self.text_updated.emit(-1)

    @Slot()
    def close(self) -> None:
        self._pending.clear()
        if self._conn is not None:
            self._conn.close()
            self._conn = None
