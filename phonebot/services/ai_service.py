"""Lokalne AI bez GUI: dane do nauki z bazy, douczanie klasyfikatora, wyniki warstw w bazie.

Wszystko działa na Twoim komputerze i nic nie kosztuje: scikit-learn (tytuły) i CLIP w onnxruntime
(zdjęcia). Warstwa GUI (``ui/ai_worker.py``) uruchamia to w osobnym wątku.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ..core.settings import Settings
from ..ml.text_model import (
    STAGE_TO_LABEL,
    Example,
    ModelInfo,
    TextClassifier,
    get_classifier,
    seed_training_set,
    set_classifier,
)
from ..storage.repositories import AiRepository, LabelRepository, RejectedRepository

log = logging.getLogger(__name__)

PHOTO_TIMEOUT_S = 10.0
PHOTO_HOST_DELAY_S = 1.0  # odstęp między zdjęciami z tego samego serwera (CDN portalu)
MAX_REJECTED_FOR_TRAINING = 4000
REJECTED_RETRAIN_STEP = 500  # douczanie także po tylu nowych ofertach odrzuconych przez reguły
# Ukrywasz oferty z różnych powodów (cena, odległość). Ukryta oferta, którą model i tak uważa za telefon
# z taką pewnością, nie jest wskazówką „to nie telefon” — do tego służy przycisk „To nie jest telefon”.
HIDDEN_PHONE_SKIP = 0.90


@dataclass
class PhotoJob:
    source: str
    source_id: str
    url: str
    offer_id: int | None = None


@dataclass
class TrainingSet:
    examples: list[Example]
    hidden_skipped: int = 0


def build_training_set(conn: sqlite3.Connection, settings: Settings, *,
                       judge: TextClassifier | None = None) -> TrainingSet:
    """Zbiór startowy + odrzucone przez reguły + Twoje oznaczenia (+ ukryte, jeśli włączone).

    Ukryte oferty to słaba wskazówka: klasę („akcesorium”, „część”, „kupię”) wybiera model pomocniczy
    wytrenowany na pozostałych danych (bez ukrytych — żeby nie utrwalał własnych pomyłek), a oferty,
    które uważa za telefon, są pomijane.
    """
    examples = seed_training_set()
    for r in RejectedRepository(conn).list(limit=MAX_REJECTED_FOR_TRAINING):
        label = STAGE_TO_LABEL.get(r.stage)
        if label:
            examples.append(Example(r.title, label, "rejected"))
    hidden: list[str] = []
    for title, label, origin in LabelRepository(conn).all():
        if origin == "hidden":
            if settings.ml.learn_from_hidden:
                hidden.append(title)
            continue
        examples.append(Example(title, label, origin))
    skipped = 0
    if hidden:
        judge = judge or TextClassifier.quick(examples)
        for title, probs in zip(hidden, judge.predict(hidden), strict=True):
            if probs.get("phone", 0.0) >= HIDDEN_PHONE_SKIP:
                skipped += 1
                continue
            label = max((k for k in probs if k != "phone"), key=probs.get)
            examples.append(Example(title, label, "hidden"))
    return TrainingSet(examples, skipped)


def labels_count(conn: sqlite3.Connection, settings: Settings) -> int:
    """Twoje oznaczenia, które liczą się do automatycznego douczania."""
    labels = LabelRepository(conn)
    return labels.count("user") + (labels.count("hidden") if settings.ml.learn_from_hidden else 0)


def model_id(info: ModelInfo) -> str:
    return f"text/{info.trained_at}"


class AiService:
    def __init__(self, conn: sqlite3.Connection, settings: Settings, models_directory: Path | None = None):
        self.conn = conn
        self.settings = settings
        if models_directory is None:
            from ..paths import models_dir

            models_directory = models_dir()
        self.models_directory = models_directory
        self._last_host_hit: dict[str, float] = {}

    # ---------------------------------------------------------- tytuły ---

    def text_classifier(self, *, train_if_missing: bool = False) -> TextClassifier | None:
        """Model z dysku; przy pierwszym uruchomieniu (``train_if_missing``) trening na danych z bazy."""
        if not self.settings.ml.text_enabled:
            return None
        clf = get_classifier(self.models_directory)
        if clf is None and train_if_missing:
            self.retrain()
            clf = get_classifier(self.models_directory)
        return clf

    def needs_retraining(self) -> bool:
        """Po [N] nowych oznaczeniach (Ustawienia → AI lokalne) albo po 500 nowych odrzuconych ofertach."""
        clf = self.text_classifier()
        if clf is None:
            return False
        if labels_count(self.conn, self.settings) - clf.info.labels_seen >= max(1, self.settings.ml.retrain_after_labels):
            return True
        return RejectedRepository(self.conn).count() - clf.info.rejected_seen >= REJECTED_RETRAIN_STEP

    def retrain(self) -> ModelInfo:
        data = build_training_set(self.conn, self.settings)
        clf = TextClassifier.train(data.examples)
        clf.info.hidden_skipped = data.hidden_skipped
        clf.info.labels_seen = labels_count(self.conn, self.settings)
        clf.info.rejected_seen = RejectedRepository(self.conn).count()
        clf.save(self.models_directory)
        set_classifier(clf)
        self.update_text_predictions(force=True)
        return clf.info

    def predict_titles(self, items: list[tuple[str, str, str]]) -> int:
        """Zapisuje wynik klasyfikatora dla (source, source_id, tytuł). Zwraca liczbę ofert."""
        clf = self.text_classifier()
        if clf is None or not items:
            return 0
        probs = clf.predict([title for _, _, title in items])
        repo = AiRepository(self.conn)
        mid = model_id(clf.info)
        in_tx = self.conn.in_transaction
        if not in_tx:
            self.conn.execute("BEGIN")
        try:
            for (source, source_id, _), p in zip(items, probs, strict=True):
                repo.save_text(source, source_id, p, mid)
            if not in_tx:
                self.conn.execute("COMMIT")
        except Exception:
            if not in_tx:
                self.conn.execute("ROLLBACK")
            raise
        return len(items)

    def update_text_predictions(self, *, force: bool = False) -> int:
        """Wynik klasyfikatora dla aktywnych ofert, które go nie mają (albo wszystkich po douczeniu)."""
        clf = self.text_classifier()
        if clf is None:
            return 0
        items = AiRepository(self.conn).missing_text("" if force else model_id(clf.info))
        return self.predict_titles(items)

    # ---------------------------------------------------------- zdjęcia ---

    def _polite_wait(self, url: str) -> None:
        host = urlsplit(url).hostname or ""
        last = self._last_host_hit.get(host)
        if last is not None:
            wait = PHOTO_HOST_DELAY_S - (time.monotonic() - last)
            if wait > 0:
                time.sleep(wait)
        self._last_host_hit[host] = time.monotonic()

    def analyze_photos(self, jobs: list[PhotoJob], *, classifier=None, client=None,
                       stop: Callable[[], bool] | None = None,
                       progress: Callable[[int, int], None] | None = None) -> int:
        """Analizuje główne zdjęcia. Każda oferta raz (wynik, także błąd, zostaje w bazie). Zwraca liczbę."""
        import httpx

        from ..ml.photo_model import MODEL_ID, PhotoModelError, get_photo_classifier

        if not jobs:
            return 0
        clf = classifier or get_photo_classifier(self.models_directory)
        repo = AiRepository(self.conn)
        own = client is None
        client = client or httpx.Client(follow_redirects=True, timeout=PHOTO_TIMEOUT_S,
                                        headers={"User-Agent": "Mozilla/5.0 PhoneBot"})
        done = 0
        try:
            for i, job in enumerate(jobs):
                if stop and stop():
                    break
                self._polite_wait(job.url)
                probs, error = None, None
                try:
                    r = client.get(job.url)
                    if r.status_code != 200:
                        raise PhotoModelError(f"HTTP {r.status_code}")
                    probs = clf.classify(r.content)
                except (httpx.HTTPError, PhotoModelError) as e:
                    error = str(e) or e.__class__.__name__
                repo.save_photo(job.source, job.source_id, job.url, probs, MODEL_ID, error)
                done += 1
                if progress:
                    progress(i + 1, len(jobs))
        finally:
            if own:
                client.close()
        return done
