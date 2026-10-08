"""Pamięć i czas analizy zdjęć (CLIP w onnxruntime) przy różnych ustawieniach sesji — na prawdziwym modelu.

Każdy wariant w osobnym procesie: czas wczytania, pamięć po wczytaniu i po 30 zdjęciach, mediana czasu jednego
zdjęcia oraz wyniki (prawdopodobieństwa klas), porównane z wariantem obecnym w programie. Zdjęcia są generowane
lokalnie (bez pobierania z portali); jedyne pobranie to model z przypiętej wersji Hugging Face (jak w programie).

Uruchamiane w GitHub Actions (``perf-clip.yml``) na Linux i Windows.
"""
from __future__ import annotations

import io
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

VARIANTS = {
    "program": {},  # jak dotąd: domyślna sesja (z pulą pamięci onnxruntime)
    "bez_puli": {"enable_cpu_mem_arena": False},
    "bez_puli_i_wzorca": {"enable_cpu_mem_arena": False, "enable_mem_pattern": False},
}


def images(n: int = 30) -> list[bytes]:
    import random

    from PIL import Image, ImageDraw

    rng = random.Random(1)
    out = []
    for i in range(n):
        img = Image.new("RGB", (640 + 16 * (i % 5), 480), tuple(rng.randrange(256) for _ in range(3)))
        d = ImageDraw.Draw(img)
        for _ in range(12):
            x, y = rng.randrange(600), rng.randrange(440)
            d.rounded_rectangle((x, y, x + rng.randrange(40, 220), y + rng.randrange(60, 300)), radius=18,
                                fill=tuple(rng.randrange(256) for _ in range(3)))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
        out.append(buf.getvalue())
    return out


def child(model: Path, variant: str) -> dict:
    import onnxruntime as ort

    from phonebot.core.sysinfo import process_rss_mb
    from phonebot.ml.photo_model import PhotoClassifier

    pics = images()
    before = process_rss_mb() or 0
    t = time.perf_counter()
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = max(1, (os.cpu_count() or 2) // 2)
    for k, v in VARIANTS[variant].items():
        setattr(opts, k, v)
    clf = PhotoClassifier(ort.InferenceSession(str(model), sess_options=opts, providers=["CPUExecutionProvider"]))
    load_s = time.perf_counter() - t
    after_load = (process_rss_mb() or 0) - before
    times, probs = [], []
    for data in pics:
        t = time.perf_counter()
        probs.append(clf.classify(data))
        times.append(time.perf_counter() - t)
    return {"load_s": round(load_s, 2), "rss_load_mb": round(after_load), "rss_after_mb": round((process_rss_mb() or 0)
                                                                                                 - before),
            "ms": round(statistics.median(times[1:]) * 1000, 1), "probs": probs}


def main() -> int:
    if len(sys.argv) > 2 and sys.argv[1] == "--child":
        print(json.dumps(child(Path(sys.argv[2]), sys.argv[3])))
        return 0
    from phonebot.ml.photo_model import download_model, model_path

    folder = Path(tempfile.mkdtemp())
    download_model(folder)
    model = model_path(folder)
    results = {}
    for name in VARIANTS:
        proc = subprocess.run([sys.executable, __file__, "--child", str(model), name], capture_output=True, text=True,
                              env={**os.environ, "PYTHONPATH": os.getcwd()})
        if proc.returncode:
            print(name, "BŁĄD", proc.stderr[-2000:])
            return 1
        results[name] = json.loads(proc.stdout.strip().splitlines()[-1])
    base = results["program"]["probs"]
    print(f"{'wariant':20} {'wczytanie':>10} {'RAM po wczytaniu':>17} {'RAM po 30 zdj.':>15} {'1 zdjęcie':>10} "
          f"{'maks. różnica wyników':>22}")
    for name, r in results.items():
        diff = max(abs(a[k] - b[k]) for a, b in zip(r["probs"], base, strict=True) for k in a)
        print(f"{name:20} {r['load_s']:>9.2f}s {r['rss_load_mb']:>15} MB {r['rss_after_mb']:>12} MB "
              f"{r['ms']:>8.1f}ms {diff:>22.2e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
