"""Wątek lokalnego AI: modele ładowane raz przy starcie, analiza zdjęć i opisów oraz douczanie w tle.

Wszystkie ciężkie operacje (trening, pobranie modelu zdjęć, CLIP, zapytania do Ollamy) dzieją się tutaj,
w osobnym wątku z własnym połączeniem z bazą — okno programu nie zawiesza się. Zdjęcia i opisy są
analizowane porcjami, więc między porcjami wątek obsługuje inne polecenia (np. „Douczyć model”)
i szybko reaguje na zamknięcie.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal, Slot

from ..core.settings import Settings
from ..core.text import plural
from ..net.http import HostRateLimiter
from ..services.ai_service import AiService, DescJob, PhotoJob
from ..storage.db import connect

log = logging.getLogger(__name__)

PHOTO_RETRY_S = 15 * 60  # po nieudanym pobraniu/wczytaniu modelu zdjęć nie próbuj częściej
PHOTO_CHUNK = 5  # zdjęć na porcję
LLM_CHECK_S = 60  # stan Ollamy (działa? jest model?) sprawdzany najwyżej raz na minutę


class AiWorker(QObject):
    status = Signal(str)  # krótki opis stanu do paska statusu
    text_updated = Signal(int)  # ile ofert dostało wynik klasyfikatora tytułów (-1 = wszystkie, po treningu)
    photos_done = Signal(int)  # ile zdjęć przeanalizowano w tej porcji
    retrained = Signal(object)  # ModelInfo
    photo_model_ready = Signal(bool)
    desc_done = Signal(int)  # ile opisów przeczytał lokalny model językowy w tej porcji
    llm_state = Signal(bool, str)  # Ollama gotowa? + opis stanu

    def __init__(self, db_path: Path, settings: Settings, models_directory: Path | None = None,
                 limiter: HostRateLimiter | None = None):
        super().__init__()
        self.db_path = db_path
        self.limiter = limiter  # wspólny z wyszukiwaniem: strony ofert w tym samym limicie zapytań na portal
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
        self._desc_pending: deque[DescJob] = deque()
        self._desc_scheduled = False
        self._desc_total = self._desc_done = 0
        self._llm_checked_at: float | None = None
        self._llm_ok = False
        self._llm_client = None
        self._fetcher = None

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

    def _desc_off(self) -> bool:
        return self._stop or not self.settings.ml.llm_enabled

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
            self.status.emit(f"AI: przeanalizowano {plural(self._batch_done, 'zdjęcie', 'zdjęcia', 'zdjęć')}")
        return done

    # --- opisy: lokalny model językowy (Ollama) ---

    def _ollama(self):
        from ..ml.ollama import OllamaClient

        url = self.settings.ml.llm_url
        if self._llm_client is None or self._llm_client.base_url != url.rstrip("/"):
            if self._llm_client is not None:
                self._llm_client.close()
            self._llm_client = OllamaClient(url)
        return self._llm_client

    def _page_fetcher(self):
        from ..sources.pages import PageFetcher

        if self._fetcher is None:
            self._fetcher = PageFetcher(self.limiter, stop=self._desc_off)
        return self._fetcher

    def reset_llm_check(self) -> None:
        """Wołane z wątku okna po zmianie ustawień Ollamy (przypisanie jest bezpieczne między wątkami)."""
        self._llm_checked_at = None

    def _llm_ready(self) -> bool:
        """Czy Ollama działa i ma model (sprawdzane najwyżej raz na minutę)."""
        now = time.monotonic()
        if self._llm_checked_at is not None and now - self._llm_checked_at < LLM_CHECK_S:
            return self._llm_ok
        self._llm_checked_at = now
        model = self.settings.ml.llm_model
        status = self._ollama().status()
        if not status.running:
            ok, msg = False, f"Ollama nie działa — uruchom program Ollama ({status.error})"
        elif not status.has_model(model):
            ok, msg = False, f"w Ollamie nie ma modelu {model} — Ustawienia → AI lokalne → „Pobierz model”"
        else:
            ok, msg = True, f"Ollama {status.version}: {model}"
        if ok != self._llm_ok or not ok:
            self.llm_state.emit(ok, msg)
        self._llm_ok = ok
        return ok

    @Slot(list)
    def analyze_desc(self, jobs: list) -> None:
        """Dodaje oferty „DO WERYFIKACJI” (lista ``DescJob``) do kolejki czytania opisów."""
        if not jobs or self._desc_off():
            return
        if not self._llm_ready():
            return
        if not self._desc_pending:
            self._desc_total = self._desc_done = 0
        self._desc_pending.extend(jobs)
        self._desc_total += len(jobs)
        self._schedule_desc()

    def _schedule_desc(self) -> None:
        if not self._desc_scheduled and self._desc_pending:
            self._desc_scheduled = True
            QTimer.singleShot(0, self.process_desc)

    @Slot()
    def process_desc(self) -> int:
        """Czyta jeden opis z kolejki (kilka sekund na karcie graficznej). Zwraca 1 albo 0."""
        from ..ml.ollama import OllamaModelMissing, OllamaNotRunning

        self._desc_scheduled = False
        if not self._desc_pending:
            return 0
        if self._desc_off():
            self._desc_pending.clear()
            return 0
        job = self._desc_pending.popleft()
        self.status.emit(f"AI: czytanie opisów {self._desc_done + 1}/{self._desc_total} (Ollama)…")
        try:
            done = self._service().analyze_descriptions([job], client=self._ollama(), fetcher=self._page_fetcher(),
                                                        stop=self._desc_off)
        except (OllamaNotRunning, OllamaModelMissing) as e:
            self._desc_pending.clear()
            self._llm_ok, self._llm_checked_at = False, time.monotonic()
            self.llm_state.emit(False, str(e))
            self.status.emit(f"AI: analiza opisów wstrzymana — {e}")
            return 0
        except Exception as e:  # noqa: BLE001
            log.exception("Analiza opisu nie powiodła się")
            self.status.emit(f"AI: błąd analizy opisu — {e}")
            done = 0
        self._desc_done += 1
        if done:
            self.desc_done.emit(done)
        if self._desc_pending:
            self._schedule_desc()
        else:
            self.status.emit(f"AI: przeczytano {plural(self._desc_done, 'opis', 'opisy', 'opisów')} (Ollama)")
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
        self._desc_pending.clear()
        if self._fetcher is not None:
            self._fetcher.close()
            self._fetcher = None
        if self._llm_client is not None:
            self._llm_client.close()
            self._llm_client = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None
