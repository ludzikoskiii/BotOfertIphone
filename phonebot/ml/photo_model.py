"""Analiza głównego zdjęcia oferty modelem CLIP (zero-shot), lokalnie na CPU.

* Model: openai/clip-vit-base-patch32, koder obrazu w formacie ONNX (eksport Xenova, fp16, ~176 MB),
  pobierany raz z Hugging Face przy pierwszym użyciu i sprawdzany sumą SHA-256.
* Uruchamiany przez ``onnxruntime`` (kilkanaście MB) — bez torcha (~500 MB), więc program zostaje lekki.
* Opisy klas (smartfon / etui / szkło ochronne / pudełko) zostały zamienione na wektory raz, oryginalnym
  modelem (skrypt ``scripts/clip_prepare.py`` w GitHub Actions) — w programie są gotowe liczby.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

MODEL_URL = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/onnx/vision_model_fp16.onnx"
MODEL_SHA256 = "35c4e0fb0aeee527dcde1693520b214a34424a786babd530f35366bad5844efd"
MODEL_FILE = "clip-vit-b32-vision-fp16.onnx"
MODEL_SIZE_MB = 176
MODEL_ID = "clip-vit-b32-fp16/v1"

CLASSES = ("smartphone", "case", "screen_protector", "box")
LOGIT_SCALE = 100.0
# wektory opisów klas (fp16, 4×512) — policzone przez scripts/clip_prepare.py (zestaw C) i sprawdzone sumą
_EMB_B64 = (
    "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"
    "GKh9my6pPyDzp3ClUazyoIWg9STgmfef36XsJsYdqCS4qLchXqTlo+UjdaFGIv0okZxDl2okKaG5KkulPCVrIy0mEh36JQClgJ0opJOg"
    "5J9zKTCoKqELGzSaqR2xIISmbSpbow6gdaLSDJYldaJtJoMkRhCgpEYpcalFKC+fjh0PI5EdByIOJGKlWKTvpBmoxB8nKZqnOh/zl90Y"
    "jZUEIDWp0ifdqNsqPR5NIrqb1aAVp34ol6jgIQEkRCZ5qPujoxevKbqd1yWOKAooLqEUJSSqBqRDoR8fZSr7H7gnUSexEW6dFpMdmOiS"
    "myVMmbonnBpFGhUqnqZbJDqqVqh4IRckzqbIIpAidjhxqFUoNIs2Hn6kmilYIm8kHCTvK34dlaz6ISGmMaktoTCv+JT6nEqliB5NJOIk"
    "ZyjiGgQoZqADnP2o/ShLJVSaZ6XjJgokvSEvppElKKVloRUjwarVqwyjDit3I5eq6J+hpTYYlJT/IQiemCH6KGKkMajiHTkiXx7kKP6k"
    "nKvDn9ehraShmL0oLiUZKOYl+KErHj2e3KF6praWfaz6JzAgYyWcIo2nyJ/PnCWo9yG7qBAkVCS+pEeoaqCZqIWneZj3FkgljCTOKJoi"
    "RqbCKMWcsKW5JAai4JEep+ckVaxUHJooV5z1pEqaPCeUHyGoH6l5kfSmyKCwoaMpBqJ9FC+lwKpipgenEKWvJ36lW62uHziXOqJJI5go"
    "KycZJOih8CWEjAakvaxhnt0jLKliJ+EluiEVp1mY+CCQgEYjy6Xeo06kpyIsI1SkVBqcqMshzSkZjqSWHpyKJTekwiRxqF2hZiLlH6yk"
    "cJ4aMF4niygmJZqhXyRdIlCm7J6OJjYl7ixJnDQoFac2ow+rp6njqQ=="
)
EMB_SHA256 = "e87d99b3dbb9e5adb96b925f4fbee75395657c5ae27d2ca9c09e42cf5a6a9056"

_MEAN = (0.48145466, 0.4578275, 0.40821073)
_STD = (0.26862954, 0.26130258, 0.27577711)
SIZE = 224


class PhotoModelError(Exception):
    pass


def class_embeddings():
    import numpy as np

    data = base64.b64decode(_EMB_B64)
    if hashlib.sha256(data).hexdigest() != EMB_SHA256:
        raise PhotoModelError("uszkodzone wektory opisów klas")
    emb = np.frombuffer(data, dtype=np.float16).astype(np.float32).reshape(len(CLASSES), -1)
    return emb / np.linalg.norm(emb, axis=1, keepdims=True)


def preprocess(data: bytes):
    """Zdjęcie → tensor 1×3×224×224 jak w CLIP (skalowanie krótszego boku, środek, normalizacja)."""
    import numpy as np
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(data)).convert("RGB")
    except Exception as e:  # noqa: BLE001 — uszkodzony albo nieobsługiwany plik
        raise PhotoModelError(f"nie da się odczytać zdjęcia: {e}") from e
    w, h = img.size
    scale = SIZE / min(w, h)
    img = img.resize((max(SIZE, round(w * scale)), max(SIZE, round(h * scale))), Image.BICUBIC)
    w, h = img.size
    left, top = (w - SIZE) // 2, (h - SIZE) // 2
    img = img.crop((left, top, left + SIZE, top + SIZE))
    arr = (np.asarray(img, dtype=np.float32) / 255.0 - np.array(_MEAN, dtype=np.float32)) / np.array(_STD,
                                                                                                    dtype=np.float32)
    return arr.transpose(2, 0, 1)[None]


class PhotoClassifier:
    def __init__(self, session):
        self.session = session
        inputs = session.get_inputs()
        self._input = inputs[0].name
        self._fp16_input = "float16" in (inputs[0].type or "")
        names = [o.name for o in session.get_outputs()]
        self._output = names.index("image_embeds") if "image_embeds" in names else 0
        self._emb = class_embeddings()

    @classmethod
    def load(cls, path: Path) -> PhotoClassifier:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        # analiza w tle — zostaw rdzenie dla okna programu
        opts.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
        session = ort.InferenceSession(str(path), sess_options=opts, providers=["CPUExecutionProvider"])
        return cls(session)

    def classify(self, data: bytes) -> dict[str, float]:
        import numpy as np

        pix = preprocess(data)
        if self._fp16_input:
            pix = pix.astype(np.float16)
        vec = np.asarray(self.session.run(None, {self._input: pix})[self._output], dtype=np.float32).reshape(-1)
        vec = vec / (np.linalg.norm(vec) or 1.0)
        logits = LOGIT_SCALE * (self._emb @ vec)
        e = np.exp(logits - logits.max())
        p = e / e.sum()
        return {c: float(x) for c, x in zip(CLASSES, p, strict=True)}


def model_path(directory: Path) -> Path:
    return directory / MODEL_FILE


def model_ready(directory: Path) -> bool:
    return model_path(directory).exists()


def download_model(directory: Path, *, client=None, progress: Callable[[int, int], None] | None = None,
                   url: str = MODEL_URL, sha256: str = MODEL_SHA256) -> Path:
    """Pobiera model (raz) i sprawdza sumę kontrolną. Plik trafia na miejsce dopiero po weryfikacji."""
    import httpx

    directory.mkdir(parents=True, exist_ok=True)
    target = model_path(directory)
    if target.exists():
        return target
    tmp = target.with_suffix(".part")
    own = client is None
    client = client or httpx.Client(follow_redirects=True, timeout=httpx.Timeout(60, read=120))
    digest = hashlib.sha256()
    try:
        with client.stream("GET", url) as r:
            if r.status_code != 200:
                raise PhotoModelError(f"pobieranie modelu zdjęć: HTTP {r.status_code}")
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with tmp.open("wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    digest.update(chunk)
                    done += len(chunk)
                    if progress:
                        progress(done, total)  # może przerwać pobieranie wyjątkiem (zamykanie programu)
    except httpx.HTTPError as e:
        tmp.unlink(missing_ok=True)
        raise PhotoModelError(f"pobieranie modelu zdjęć nie powiodło się: {e}") from e
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        if own:
            client.close()
    if digest.hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        raise PhotoModelError("pobrany model zdjęć ma złą sumę kontrolną — plik odrzucony")
    tmp.replace(target)
    return target


# ------------------------------------------ jeden model na cały program ---

_lock = threading.Lock()
_current: PhotoClassifier | None = None


def get_photo_classifier(directory: Path | None = None, *, download: bool = True,
                         progress: Callable[[int, int], None] | None = None) -> PhotoClassifier:
    """Model ładowany raz (pobierany przy pierwszym użyciu). Rzuca ``PhotoModelError``, gdy się nie da."""
    global _current
    with _lock:
        if _current is not None:
            return _current
        if directory is None:
            from ..paths import models_dir

            directory = models_dir()
        path = model_path(directory)
        if not path.exists():
            if not download:
                raise PhotoModelError("model zdjęć nie jest jeszcze pobrany")
            download_model(directory, progress=progress)
        try:
            _current = PhotoClassifier.load(path)
        except Exception as e:  # noqa: BLE001 — np. uszkodzony plik
            raise PhotoModelError(f"nie da się wczytać modelu zdjęć: {e}") from e
        return _current


def set_photo_classifier(clf: PhotoClassifier | None) -> None:
    global _current
    with _lock:
        _current = clf
