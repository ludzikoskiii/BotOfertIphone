"""Pomiar wydajności okna na dużej bazie (bez sieci): start, odświeżenie, przewijanie, pamięć, sort/filtr.

    QT_QPA_PLATFORM=offscreen python tools/benchmark.py --offers 3000 [--profile reload] [--json wynik.json]

Każdy pomiar w osobnym procesie (start liczony od zera, łącznie z importem modułów). Wyniki porównujemy
przed i po optymalizacji na tej samej bazie (ziarno losowania stałe).
"""
from __future__ import annotations

import argparse
import cProfile
import io
import json
import os
import pstats
import random
import resource
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCES = ["allegro_lokalnie", "vinted", "sprzedajemy", "lento", "allegro", "ebay"]
MODELS = ["iPhone 11", "iPhone 12", "iPhone 12 Pro", "iPhone 13", "iPhone 13 mini", "iPhone 13 Pro", "iPhone 14",
          "iPhone 14 Pro", "iPhone 15", "iPhone 15 Pro Max", "iPhone XR", "iPhone SE 2020"]
CITIES = ["Kraków", "Nowy Targ", "Zakopane", "Warszawa", "Gdańsk", "Wrocław", "Poznań", "Nowy Sącz", "Rzeszów", None]
EXTRAS = ["", " zbity ekran", " bateria 81%", " nie ładuje", " stan idealny", " pęknięty tył", " face id nie działa"]
DESCS = ["Sprzedam telefon w bardzo dobrym stanie, bateria 88%, bez blokad, komplet z pudełkiem i ładowarką. "
         "Możliwa wysyłka paczkomatem albo odbiór osobisty w centrum miasta.",
         "Telefon sprawny, drobne rysy na ramce. Kontakt przez portal.", "Stan dobry.", "",
         "Wymieniony ekran na zamiennik, działa wszystko poza Face ID. Bateria 79%. Cena do negocjacji."]


def build_db(path: Path, n: int) -> None:
    from phonebot.core.models import RawOffer
    from phonebot.core.normalizer import parse_offer
    from phonebot.core.settings import Settings
    from phonebot.storage.db import open_database
    from phonebot.storage.repositories import OfferRepository, PartsRepository, SettingsRepository

    conn = open_database(path)
    PartsRepository(conn).seed_defaults_if_empty()
    s = Settings()
    s.enabled_sources.update({k: True for k in SOURCES})
    SettingsRepository(conn).save(s)
    repo = OfferRepository(conn)
    rng = random.Random(7)
    conn.execute("BEGIN")
    for i in range(n):
        model = rng.choice(MODELS)
        storage = rng.choice([64, 128, 256, 512])
        title = f"{model} {storage}GB{rng.choice(EXTRAS)}"
        raw = RawOffer(rng.choice(SOURCES), f"id{i}", f"https://portal.test/{i}", title,
                       float(rng.randrange(300, 4500, 10)), description=rng.choice(DESCS), city=rng.choice(CITIES),
                       photos=[f"https://img.test/{i}.jpg"], shipping_available=rng.random() < 0.8,
                       params={"seller_id": str(rng.randrange(n // 3)), "seller": f"user{rng.randrange(n // 3)}"})
        res = repo.upsert(raw, parse_offer(raw))
        for k in range(rng.randrange(0, 3)):  # historia cen
            conn.execute("INSERT INTO price_history (offer_id, price, seen_at) VALUES (?, ?, ?)",
                         (res.offer_id, raw.price + 50 * (k + 1), "2026-09-01T10:00:00+00:00"))
        conn.execute("INSERT INTO photo_hashes (source, source_id, url, dhash, stock, computed_at) "
                     "VALUES (?, ?, ?, ?, 0, '2026-09-26T10:00:00+00:00')",
                     (raw.source, raw.source_id, raw.photos[0], f"{rng.getrandbits(64):016x}"))
    conn.execute("COMMIT")
    conn.close()


def rss_mb() -> float:
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # Linux: KB → MB


def measure(db: Path, profile: str | None) -> dict:
    t0 = time.perf_counter()
    from PySide6.QtWidgets import QApplication

    from phonebot.storage.db import connect
    from phonebot.ui.main_window import MainWindow
    from phonebot.ui.table_model import Col

    t_import = time.perf_counter() - t0
    app = QApplication.instance() or QApplication([])
    conn = connect(db)
    prof = cProfile.Profile() if profile == "start" else None
    t1 = time.perf_counter()
    if prof:
        prof.enable()
    import inspect

    deferred = "defer_load" in inspect.signature(MainWindow).parameters  # starsze wersje: wszystko od razu
    win = MainWindow(conn, db, thumbs_dir=db.parent, **({"defer_load": True} if deferred else {}))
    win.resize(1600, 900)
    win.show()
    win.repaint()  # pierwsze narysowane okno = moment, w którym widać program
    t_visible = time.perf_counter() - t1
    while deferred and not win.loaded:
        app.processEvents()
    app.processEvents()
    if prof:
        prof.disable()
    t_window = time.perf_counter() - t1
    out: dict = {"import_s": round(t_import, 3), "visible_s": round(t_import + t_visible, 3),
                 "window_s": round(t_window, 3), "start_s": round(t_import + t_window, 3),
                 "rows": win.model.rowCount()}

    def timed(name, fn, repeat=3):
        best = 1e9
        for _ in range(repeat):
            t = time.perf_counter()
            fn()
            app.processEvents()
            best = min(best, time.perf_counter() - t)
        out[name] = round(best * 1000, 1)

    prof2 = cProfile.Profile() if profile == "reload" else None
    if prof2:
        prof2.enable()
    timed("reload_ms", win.reload)
    if prof2:
        prof2.disable()
    from phonebot.core.sorting import SortLevel
    from phonebot.core.view_filter import ViewFilter

    timed("sort_ms", lambda: win.model.set_sort_spec([SortLevel("price", "asc"), SortLevel("model", "asc")]))
    timed("filter_ms", lambda: (win.filters.set_filter(ViewFilter(models=["iPhone 13"])),
                                win.filters.set_filter(ViewFilter())))
    timed("tab_switch_ms", lambda: (win.list_tabs.setCurrentIndex(1), win.list_tabs.setCurrentIndex(0)))
    # przewijanie: z kolumną zdjęć (miniatury) i bez
    for photos in (False, True):
        win.set_column_visible(Col.PHOTO, photos)
        app.processEvents()
        bar = win.table.verticalScrollBar()
        frames = []
        for v in range(0, bar.maximum(), max(1, bar.maximum() // 60)):
            t = time.perf_counter()
            bar.setValue(v)
            win.table.viewport().repaint()
            frames.append((time.perf_counter() - t) * 1000)
        key = "scroll_photos" if photos else "scroll"
        out[f"{key}_avg_ms"] = round(sum(frames) / len(frames), 2)
        out[f"{key}_max_ms"] = round(max(frames), 2)
    out["rss_mb"] = round(rss_mb(), 1)
    for p, name in ((prof, "start"), (prof2, "reload")):
        if p:
            s = io.StringIO()
            pstats.Stats(p, stream=s).sort_stats("cumulative").print_stats(28)
            out[f"profile_{name}"] = s.getvalue()
    win._quitting = True
    win.close()
    conn.close()
    return out


def measure_scan(db: Path, n: int) -> dict:
    """Zapis wyników skanu (bez sieci): n ofert, w tym połowa nowych — filtr, sprzedawcy, upsert, AI tytułów."""
    import asyncio

    from phonebot.core.models import RawOffer
    from phonebot.core.settings import Settings
    from phonebot.net.http import HostRateLimiter
    from phonebot.services.post_scan import run_post_scan
    from phonebot.services.scanner import Scanner, ScanReport, SourceReport
    from phonebot.storage.db import connect

    conn = connect(db)
    rng = random.Random(11)
    raws = [RawOffer("vinted", f"id{i}" if i % 2 else f"new{i}", f"https://portal.test/{i}",
                     f"{rng.choice(MODELS)} 128GB", float(rng.randrange(300, 4500, 10)), description=rng.choice(DESCS),
                     photos=[f"https://img.test/{i}.jpg"], params={"seller_id": str(i % 97)}) for i in range(n)]
    scanner = Scanner(conn, Settings(), HostRateLimiter(0))
    rep = SourceReport("vinted", "Vinted")
    report = ScanReport()
    t = time.perf_counter()
    scanner._store(raws, rep, report)
    t_store = time.perf_counter() - t
    t = time.perf_counter()
    run_post_scan(conn, Settings(), report)
    t_post = time.perf_counter() - t
    conn.close()
    del asyncio
    return {"scan_store_ms": round(t_store * 1000, 1), "post_scan_ms": round(t_post * 1000, 1),
            "scan_new": rep.new}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offers", type=int, default=3000)
    ap.add_argument("--profile", choices=["start", "reload"])
    ap.add_argument("--json")
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--db", help=argparse.SUPPRESS)
    args = ap.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if args.child == "window":
        print(json.dumps(measure(Path(args.db), args.profile)))
        return 0
    if args.child == "scan":
        print(json.dumps(measure_scan(Path(args.db), min(args.offers, 1000))))
        return 0
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "bench.sqlite3"
    t = time.perf_counter()
    build_db(db, args.offers)
    print(f"baza: {args.offers} ofert ({time.perf_counter() - t:.1f} s)", file=sys.stderr)
    result: dict = {"offers": args.offers}
    # pierwszy start na nowej bazie sprawdza jednorazowo zapisane oferty nowymi regułami — drugi to typowy start
    for run, child in (("first", "window"), ("", "window"), ("", "scan")):
        cmd = [sys.executable, __file__, "--child", child, "--db", str(db), "--offers", str(args.offers)]
        if args.profile and child == "window" and not run:
            cmd += ["--profile", args.profile]
        proc = subprocess.run(cmd, capture_output=True, text=True, env={**os.environ, "PYTHONPATH": str(ROOT)})
        if proc.returncode:
            print(proc.stderr, file=sys.stderr)
            return proc.returncode
        data = json.loads(proc.stdout.strip().splitlines()[-1])
        if run:
            result.update({"first_visible_s": data["visible_s"], "first_start_s": data["start_s"]})
        else:
            result.update(data)
    for k, v in result.items():
        if not k.startswith("profile_"):
            print(f"{k:22} {v}")
    for k, v in result.items():
        if k.startswith("profile_"):
            print(f"\n==== {k} ====\n{v}")
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
