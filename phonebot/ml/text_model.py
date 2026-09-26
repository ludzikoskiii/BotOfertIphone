"""Klasyfikator tytułów ogłoszeń: telefon / akcesorium / część / kupię — scikit-learn, lokalnie.

Model: TF-IDF na n-gramach znakowych (2–5, odporne na literówki i obce języki) + n-gramach
słownych (1–2) → regresja logistyczna. Uczy się na:

* zbiorze startowym (``seed_data``) — działa od pierwszego uruchomienia,
* ogłoszeniach odrzuconych przez filtr reguł (etap → klasa; waga niższa, bo to etykiety automatyczne),
* Twoich oznaczeniach: „To jest telefon”, „To nie jest telefon” (waga najwyższa),
* ofertach ukrytych ręcznie (słaba wskazówka „nie telefon”; można wyłączyć w ustawieniach).

Skuteczność mierzona jest na 20% danych odłożonych przed treningiem oraz na zestawie kontrolnym
prawdziwych tytułów, którego model nigdy nie widzi (``seed_data.BENCHMARK``).
"""
from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from ..core.text import normalize
from .seed_data import BENCHMARK, LABELS, seed_examples

log = logging.getLogger(__name__)

MODEL_VERSION = 1
MODEL_FILE = "text_classifier.joblib"
INFO_FILE = "text_classifier.json"

# waga przykładu wg pochodzenia: Twoje oznaczenia liczą się najbardziej
WEIGHTS = {"seed": 1.0, "rejected": 0.5, "user": 3.0, "hidden": 0.3}
# etap odrzucenia przez reguły → klasa (pozostałe etapy nie mówią, czym jest przedmiot)
STAGE_TO_LABEL = {"accessory": "accessory", "multi_model": "accessory", "category": "accessory",
                  "part": "part", "wanted": "wanted"}


@dataclass
class Example:
    title: str
    label: str
    origin: str = "seed"

    @property
    def weight(self) -> float:
        return WEIGHTS.get(self.origin, 1.0)


@dataclass
class ModelInfo:
    trained_at: str
    version: int = MODEL_VERSION
    n_train: int = 0
    n_test: int = 0
    accuracy: float = 0.0  # na odłożonych 20%
    per_class: dict[str, dict[str, float]] = field(default_factory=dict)
    benchmark_accuracy: float = 0.0  # na prawdziwych tytułach spoza treningu
    benchmark_n: int = 0
    benchmark_errors: list[str] = field(default_factory=list)
    sources: dict[str, int] = field(default_factory=dict)  # liczba przykładów wg pochodzenia
    user_labels: int = 0  # ile Twoich oznaczeń było w danych
    hidden_labels: int = 0  # ile ofert ukrytych ręcznie było w danych
    hidden_skipped: int = 0  # ukryte pominięte, bo model uznał je za telefony (ukryte z innego powodu)
    labels_seen: int = 0  # oznaczenia w bazie w chwili treningu — od tego liczy się automatyczne douczanie
    rejected_seen: int = 0  # odrzucone przez reguły w bazie w chwili treningu

    @classmethod
    def from_dict(cls, data: dict) -> ModelInfo:
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


def _prep(text: str) -> str:
    """Wspólne przygotowanie tekstu (musi być funkcją modułu — model zapisany na dysku się do niej odwołuje)."""
    return normalize(text).replace("|", " ")


def _pipeline():
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline, make_union

    features = make_union(
        TfidfVectorizer(preprocessor=_prep, analyzer="char_wb", ngram_range=(2, 5), sublinear_tf=True, min_df=1),
        TfidfVectorizer(preprocessor=_prep, analyzer="word", ngram_range=(1, 2), token_pattern=r"(?u)\b\w+\b",
                        sublinear_tf=True),
    )
    return make_pipeline(features, LogisticRegression(max_iter=3000, C=4.0, class_weight="balanced"))


def dedupe(examples: list[Example]) -> list[Example]:
    """Jeden przykład na tytuł; przy sprzecznych etykietach wygrywa większa waga (Twoje oznaczenie)."""
    best: dict[str, Example] = {}
    for ex in examples:
        key = _prep(ex.title)
        if not key:
            continue
        if key not in best or ex.weight > best[key].weight:
            best[key] = ex
    return list(best.values())


class TextClassifier:
    def __init__(self, pipeline, info: ModelInfo):
        self.pipeline = pipeline
        self.info = info

    @property
    def classes(self) -> list[str]:
        return [str(c) for c in self.pipeline.classes_]

    def predict(self, titles: list[str]) -> list[dict[str, float]]:
        """Rozkład prawdopodobieństwa klas dla każdego tytułu."""
        if not titles:
            return []
        probs = self.pipeline.predict_proba(titles)
        classes = self.classes
        return [{c: float(p) for c, p in zip(classes, row, strict=True)} for row in probs]

    def predict_one(self, title: str) -> dict[str, float]:
        return self.predict([title])[0]

    # ------------------------------------------------------------ trening ---

    @classmethod
    def quick(cls, examples: list[Example]) -> TextClassifier:
        """Jeden trening bez pomiaru skuteczności (model pomocniczy, np. do oceny ofert ukrytych)."""
        data = dedupe(examples)
        pipe = _pipeline()
        pipe.fit([e.title for e in data], [e.label for e in data],
                 logisticregression__sample_weight=[e.weight for e in data])
        return cls(pipe, ModelInfo(trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                   n_train=len(data)))

    @classmethod
    def train(cls, examples: list[Example], *, test_size: float = 0.2, seed: int = 42) -> TextClassifier:
        from sklearn.metrics import accuracy_score, classification_report
        from sklearn.model_selection import train_test_split

        data = dedupe(examples)
        titles = [e.title for e in data]
        labels = [e.label for e in data]
        weights = [e.weight for e in data]
        counts = Counter(labels)
        stratify = labels if min(counts.values()) >= 2 else None
        x_tr, x_te, y_tr, y_te, w_tr, _ = train_test_split(titles, labels, weights, test_size=test_size,
                                                           random_state=seed, stratify=stratify)
        probe = _pipeline()
        probe.fit(x_tr, y_tr, logisticregression__sample_weight=w_tr)
        pred = probe.predict(x_te)
        report = classification_report(y_te, pred, output_dict=True, zero_division=0)
        per_class = {c: {"precision": round(report[c]["precision"], 3), "recall": round(report[c]["recall"], 3),
                         "f1": round(report[c]["f1-score"], 3), "support": int(report[c]["support"])}
                     for c in LABELS if c in report}

        final = _pipeline()  # model docelowy uczy się na wszystkich danych
        final.fit(titles, labels, logisticregression__sample_weight=weights)
        bench_titles = [t for t, _ in BENCHMARK]
        bench_pred = final.predict(bench_titles)
        errors = [f"{t} → {p} (powinno: {y})" for (t, y), p in zip(BENCHMARK, bench_pred, strict=True) if p != y]
        info = ModelInfo(
            trained_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            n_train=len(titles), n_test=len(x_te), accuracy=round(float(accuracy_score(y_te, pred)), 4),
            per_class=per_class,
            benchmark_accuracy=round(1 - len(errors) / len(BENCHMARK), 4), benchmark_n=len(BENCHMARK),
            benchmark_errors=errors[:20], sources=dict(Counter(e.origin for e in data)),
            user_labels=sum(1 for e in examples if e.origin == "user"),
            hidden_labels=sum(1 for e in examples if e.origin == "hidden"),
        )
        log.info("Klasyfikator tytułów: %d przykładów, skuteczność %.1f%% (kontrola %.1f%%)", info.n_train,
                 info.accuracy * 100, info.benchmark_accuracy * 100)
        return cls(final, info)

    # --------------------------------------------------------- zapis/odczyt ---

    def save(self, directory: Path) -> None:
        import joblib

        directory.mkdir(parents=True, exist_ok=True)
        tmp = directory / (MODEL_FILE + ".tmp")
        joblib.dump(self.pipeline, tmp, compress=3)
        tmp.replace(directory / MODEL_FILE)
        (directory / INFO_FILE).write_text(json.dumps(asdict(self.info), ensure_ascii=False, indent=2),
                                           encoding="utf-8")

    @classmethod
    def load(cls, directory: Path) -> TextClassifier | None:
        import joblib

        model_path, info_path = directory / MODEL_FILE, directory / INFO_FILE
        if not model_path.exists() or not info_path.exists():
            return None
        try:
            info = ModelInfo.from_dict(json.loads(info_path.read_text(encoding="utf-8")))
            if info.version != MODEL_VERSION:
                return None
            return cls(joblib.load(model_path), info)
        except Exception:  # noqa: BLE001 — np. model zapisany inną wersją scikit-learn
            log.warning("Nie udało się wczytać klasyfikatora tytułów — zostanie wytrenowany od nowa", exc_info=True)
            return None


def seed_training_set() -> list[Example]:
    return [Example(t, label, "seed") for t, label in seed_examples()]


# --------------------------------------------- jeden model na cały program ---

_lock = threading.Lock()
_current: TextClassifier | None = None


def get_classifier(directory: Path | None = None, *, train_if_missing: bool = False) -> TextClassifier | None:
    """Model wczytany raz z dysku. Gdy go nie ma: ``None`` albo (``train_if_missing``) model ze zbioru startowego.

    Program trenuje pierwszy model na pełnych danych z bazy (``AiService.text_classifier``), a to jest
    wersja bez bazy — dla narzędzi i testów."""
    global _current
    with _lock:
        if _current is not None:
            return _current
        if directory is None:
            from ..paths import models_dir

            directory = models_dir()
        clf = TextClassifier.load(directory)
        if clf is None and train_if_missing:
            clf = TextClassifier.train(seed_training_set())
            try:
                clf.save(directory)
            except OSError:
                log.warning("Nie udało się zapisać klasyfikatora tytułów", exc_info=True)
        _current = clf
        return clf


def current_classifier() -> TextClassifier | None:
    """Model już wczytany — bez czytania dysku i bez czekania na trening (dla wątku okna)."""
    return _current


def set_classifier(clf: TextClassifier | None) -> None:
    global _current
    with _lock:
        _current = clf


def top(probs: dict[str, float]) -> tuple[str, float]:
    label = max(probs, key=probs.get)
    return label, probs[label]
