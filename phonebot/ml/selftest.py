"""Self-test lokalnego AI w spakowanym programie (``PhoneBot.exe --self-test``), bez internetu.

Sprawdza, czy w pliku exe są działające scikit-learn, joblib, numpy, Pillow i onnxruntime: trenuje
klasyfikator tytułów na zbiorze startowym, zapisuje go i wczytuje, a zdjęcie „analizuje” maleńkim modelem
ONNX o tym samym wejściu i wyjściu co CLIP (zawsze zwraca kierunek „smartfon”) — prawdziwego modelu
(176 MB) nie trzeba do tego pobierać.
"""
from __future__ import annotations

import base64
import io
import tempfile
from pathlib import Path

# ReduceMean(pixel_values)·0 + wektor „smartfon” → image_embeds [N, 512]; opset 13, 2,4 kB
TINY_VISION_ONNX = (
    "CAcSCHBob25lYm90OrgSCj8KDHBpeGVsX3ZhbHVlcxIBbSIKUmVkdWNlTWVhbioPCgRheGVzQAFAAkADoAEHKg8KCGtlZXBkaW1zGAGg"
    "AQIKFwoBbQoFc2hhcGUSAm0yIgdSZXNoYXBlChIKAm0yCgR6ZXJvEgF6IgNNdWwKGwoBegoDZW1iEgxpbWFnZV9lbWJlZHMiA0FkZBIR"
    "cGhvbmVib3Rfc2VsZnRlc3QqHQgCEAdCBXNoYXBlShD//////////wEAAAAAAAAAKhIIAQgBEAFCBHplcm9KBAAAAAAqjxAIAQiABBAB"
    "QgNlbWJKgBBtYPW7aiBtPWYgZb1mwOQ8ZADgPD3giDxngGa8UMCyvFLAODw/YIw8VKA8PFygz7xZwMc8XQBQO1zATjxKQCe9QoCTuV6A"
    "0rtewFM8SaAkvV0AUD1WYMG8biD4PGQg4DprYO+8SKCgPEnApLxgYNc8VSC/O0Ygnjo84Aa8amDuPD6ACrxAoA88V8BDvGEg2bw6IIG7"
    "QSASvFPgubxlwOM7SACivFWAvrs9IIi7ZcBhPEogpjxt4HU8PsAKPUSglzxwIPw7cMD7unAg/DtIoCE7XYDRPFXAP7xXIMO8UaC2vFYg"
    "QrtQ4DK8USA2vW/geTxX4MI7SqCmvG6Adrxj4F48aMBoPEfAnrtXQMS8bkB2PGpAbjw/YAy9WwBMvFDgM709wAi7VUC+PGTg4Dtx4H28"
    "SaCjvECgDrxEoBm8OkCDvFWAvjtu4Pe8X2DVPECgD71oAOk8YaBZvG6gdz1h4Fi8RiAcPDsAhTxNgKw8WQDIuzpgAr5AAA89SyApvGng"
    "7DtcoM08PgCLO1wATzw7QAS8PuAKPWdAZzxF4Jk8QACPPGFg2bxdIFG8UMAzvENgFrpfQNU8V+DCu0XgG705oIC9PCCGPFbAwbw6wIG8"
    "QGAPPUHAEj1jIF88VyBEvHAg+7tbYMw8aKBovUwAKzxZIMi8P+AMPFigxbtGQJ28VMC8vEOgFb1ooGk9XQDQPENAlrw/QA69Q2AXP3Hg"
    "/DtWIEK8a6BwO0rgJr1HQB88XIDOvEsAqbxV4L88PiCMvEQgGD1CIBS9TkAvuzxABzxogGm9X4DUPFFANbxvQPk7VyDEO05ArzxWYEA8"
    "OQCBvD7Ai7xcgM48SmAmvWIAXbxFIJs8PCAHvWngbDw6AIO8baD1vGoAbb1m4OQ7O4ADPUKglLxVwL48YmDbOj+gDDw8YIa8XcDRuWAA"
    "2DtWYMG8P2CMvEGgETtC4JQ8PyAOPWTA3zxIYKG8P0AOPFFgtbw7AAW7QWARvUQAmDxiQNy8PAAGvT5ACz1SYLc8SUAkvD9gjjxEgJk8"
    "TCCrPEWgGz1C4JQ8QACPvEbAHDxRgLY7PUCJvFbgQTw8oIW8P+AMPEfgHj0/wIw8VKC9u1EAtztNgKy8Q8CVPETgmLtbYEw7TCArPWog"
    "bztBgBE8awDxu1nAR7xCoJM8auDtvFkAx7s94Am8TmAvPVtAS71qgG28X0BUuzyABjxDYJa8TkCvu0qgprxbAEw9cCD7vD+gDbxwwHs8"
    "XgDTvFBgNLxQYDS8ZqBku2VgYjtOAC88QOCOvG1A9TxQoLK8UGAzPTwAh7xnQOi8VeC+O2QAYTxMAKo8S+CoPGzg8bxggFe6ZSDivEJA"
    "E71wIHy7ZgDkPF6AUrxmQOW7PcAIvUuAqLthAFk7PcCJPG+g+LxcgM87aKBovHIA/zxcgE48V4DCvEGgkTxlAGM8SsAlvD8AjTtFABu9"
    "S4CnPFwATjxL4Ci8PuALvVUgv7s9wIg8ciB/PUjgITxeYNM7VKA7PUKAEz1UYL07bIDxPEpApbxKICW8PcCJuzvABLtl4OI8PAAGvGNg"
    "Xj0/wAw9XWBQvEYgHTxLoKe7S6CovGqA7Ttd4E+8YcBavG5g9zpkQOC6U4A5uj0AiDxJAKQ5PwCNO2KgXL1qgO68SgAmO12A0Txq4G28"
    "RKCZvEeAnrhEoBc/YEDYvD4gCj1vYHg8cGB7PEmAIztAoA89SqAlO1EANTw6QAM9U+C6PEAgjzxDIBe9WiBKvDmAgLxYYMW8UOCyu0rg"
    "pb1GAB07RWAbPTsABTxMgCq7X8DVO1yAzrtrQHA9ROAXukDAjzxggNi5SSAkvFbAQbxOQC49U6A5PFtgTLtkoN+8awBwvENgFjw9AIm7"
    "S0CpvGhA6bxBwJK7PWCJvDzgBj1ooGm9UWA2vWYg5LpsoHM8POCFO26g97tJoCM6YmBcvUIgFDxpYGu7cYB+PDxgBzxaoEo8aSDrukdg"
    "ILw6AAK8cMB7PFFgNjxywP+7PMAFPVJguLw64AG9RsCdOz/AjbxGQB29UuA4vE3gKz1LQKg7XaBQPUeAHrxuwHa7XSBQOl4A0ztW4EG8"
    "W2DLvGzg8rtnYOi8bGDyPDwAB7xrAPE6RYAbPGCAVzs8AIa7WsDJu2Cg17xdgNA7T2CxvDpgAjxEQJg8ZyDnPEigITxS4Lg6bMDxvEkA"
    "Jb1o4Gg7YaDZvF2AUTtiwNs8SUCjO0eAoDw+gAu8ZABhOWqA7rpkQOC8UyA7PGdgZrxFgJu8WoBKvVKAOD1kYGG9ZyBnPGjgaLtd4NC8"
    "ZcBjvF4g07tHwB87T4CwulWgP7xLYCm9QCCQuz8ADbxOIK+8O4AEvD1gCblR4DQ8PqCKvDuAA71BgBG9cgB/O29g+rxjQF+8QkCTPFyA"
    "T7xBgJK9TeAtPGYg5jtj4F68ViBBvEOglTxN4Ks7S8CnPFngR7tHoB+8VQA+O0ugqLxWwEG9RqCcPGXAYjw5AAC8aIDqPD/gjDxJQCM7"
    "XiDTul3A0btKwCU8Q6AWu2bg5DpTALu5YmDbPEOgFbxgoFg8RKAZO1HAtTlvoPm8X+DVvDzAhzxP4LA7SUAkvWOgXTxlAGM8XIDOPE5A"
    "L7tuwHe8S8CovFNAujxKAKc7aIBqvFcAQ7xJgKM7PwAOPmig6TtboEs8UGA0PUcAoDpWQEA7WQBHPFTgvDxlYGM7WgBKuExAqzw9QIg9"
    "ToAvPD1AiTxpoOy7ZeDiu0MAF71ZAMe8XyDWvFotCgxwaXhlbF92YWx1ZXMSHQobCAESFwoHEgViYXRjaAoCCAMKAwjgAQoDCOABYiQK"
    "DGltYWdlX2VtYmVkcxIUChIIARIOCgcSBWJhdGNoCgMIgARCBAoAEA0="
)


def tiny_vision_model(directory: Path) -> Path:
    path = directory / "tiny_vision.onnx"
    path.write_bytes(base64.b64decode(TINY_VISION_ONNX))
    return path


def sample_jpeg(color: tuple[int, int, int] = (200, 30, 30), size: tuple[int, int] = (320, 240)) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", size, color).save(buf, "JPEG", quality=85)
    return buf.getvalue()


def run() -> str:
    """Rzuca wyjątek, gdy coś nie działa; zwraca krótkie podsumowanie."""
    from .photo_model import PhotoClassifier
    from .text_model import TextClassifier, seed_training_set

    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        clf = TextClassifier.train(seed_training_set())
        clf.save(directory)
        loaded = TextClassifier.load(directory)
        if loaded is None:
            raise RuntimeError("zapisany klasyfikator tytułów nie daje się wczytać")
        probs = loaded.predict_one("Etui silikonowe do iPhone 13")
        if max(probs, key=probs.get) != "accessory":
            raise RuntimeError(f"klasyfikator tytułów działa niepoprawnie: {probs}")
        if clf.info.benchmark_accuracy < 0.85:
            raise RuntimeError(f"klasyfikator tytułów: za niska skuteczność {clf.info.benchmark_accuracy:.0%}")
        photo = PhotoClassifier.load(tiny_vision_model(directory))
        result = photo.classify(sample_jpeg())
        if max(result, key=result.get) != "smartphone":
            raise RuntimeError(f"analiza zdjęć działa niepoprawnie: {result}")
        del photo  # zwolnij plik modelu przed usunięciem katalogu (Windows)
    return (f"AI OK: tytuły {clf.info.accuracy:.0%} / kontrola {clf.info.benchmark_accuracy:.0%}, "
            "zdjęcia (onnxruntime) OK")
