"""Sprawdzenie analizy zdjęć PROGRAMU (``phonebot.ml.photo_model``) na prawdziwym modelu i zdjęciach z Vinted.

W odróżnieniu od ``clip_prepare.py`` (torch + transformers) używa wyłącznie kodu programu: pobiera model tak
jak program (``download_model`` z kontrolą SHA-256), analizuje zdjęcia klasą ``PhotoClassifier`` i podaje:
skuteczność przy progach z ustawień oraz czas analizy jednego zdjęcia na procesorze.

Uruchamiane w GitHub Actions (``clip-check.yml``). Zapytania do Vinted są nieliczne i z przerwami.
"""
from __future__ import annotations

import os
import re
import statistics
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

import httpx

from phonebot.core.settings import MlConfig
from phonebot.ml.photo_model import CLASSES, PhotoClassifier, download_model, model_path

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
PER_CLASS = 20


def say(*a):
    print(*a, flush=True)


def rss_mb() -> float:
    """Pamięć zajmowana przez proces (MB) — Linux i Windows."""
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]

        c = Counters()
        c.cb = ctypes.sizeof(c)
        ctypes.windll.psapi.GetProcessMemoryInfo(ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.WorkingSetSize / 1e6
    with open("/proc/self/status", encoding="ascii") as f:
        return next(int(line.split()[1]) for line in f if line.startswith("VmRSS")) / 1e3


def collect_samples(client: httpx.Client) -> list[tuple[str, str, str]]:
    """(klasa z tytułu, tytuł, adres miniatury 310×430) — tak jak w clip_prepare.py."""
    client.head("https://www.vinted.pl/catalog")
    token = client.cookies.get("access_token_web")
    h = {"Accept": "application/json", "Authorization": f"Bearer {token}", "Referer": "https://www.vinted.pl/catalog"}
    phone_hint = re.compile(r"\b\d{2,4}\s?gb\b|unlocked|\blocked\b|at&t|verizon|t-mobile|cricket|includ", re.I)
    rules = [
        ("screen_protector", re.compile(r"screen protector|tempered glass|glass protector|protection écran", re.I)),
        ("box", re.compile(r"\bempty\b.*\bbox\b|\bbox only\b|\bonly box\b|boîte vide|original box only", re.I)),
        ("case", re.compile(r"\bcases?\b|\bcover\b|coque|hülle|etui|obal|funda|custodia", re.I)),
    ]
    queries = [("iphone 13 pro", 150), ("iphone 14", 150), ("iphone 12 unlocked", 100), ("iphone case", 0),
               ("iphone screen protector", 0), ("iphone empty box", 0), ("iphone 15 pro max", 200),
               ("iphone 11 64gb", 100), ("iphone box only", 0)]
    samples, seen = [], set()
    for q, pmin in queries:
        params = {"search_text": q, "per_page": 40, "order": "relevance"}
        if pmin:
            params["price_from"] = pmin
        r = client.get("https://api.vinted.pl/svc-catalogue/items", params=params, headers=h)
        time.sleep(2)
        for it in r.json().get("items", []) if r.status_code == 200 else []:
            title = it.get("title", "")
            if it["id"] in seen:
                continue
            price = float((it.get("price") or {}).get("amount") or 0)
            label = None if phone_hint.search(title) else next((lab for lab, rx in rules if rx.search(title)), None)
            if label is None and re.search(r"iphone\s?\d{1,2}", title, re.I) and price >= 100 and \
                    not any(rx.search(title) for _, rx in rules):
                label = "smartphone"
            if label is None:
                continue
            thumbs = (it.get("photo") or {}).get("thumbnails") or []
            url = next((t["url"] for t in thumbs if t.get("type") == "thumb310x430"), None)
            if url:
                seen.add(it["id"])
                samples.append((label, title, url))
    per_class = defaultdict(list)
    for s in samples:
        per_class[s[0]].append(s)
    return [s for c in CLASSES for s in per_class[c][:PER_CLASS]]


def main() -> int:
    directory = Path(tempfile.mkdtemp())
    t = time.time()
    download_model(directory)  # ten sam kod i ta sama suma SHA-256 co w programie
    say(f"MODEL pobrany i sprawdzony (SHA-256 OK): {model_path(directory).stat().st_size / 1e6:.0f} MB, "
        f"{time.time() - t:.0f} s")
    before = rss_mb()
    t = time.time()
    clf = PhotoClassifier.load(model_path(directory))
    say(f"MODEL wczytany w {time.time() - t:.2f} s, pamięć +{rss_mb() - before:.0f} MB; procesor: "
        f"{os.cpu_count()} wątków, onnxruntime używa {max(1, (os.cpu_count() or 2) // 2)}")

    client = httpx.Client(headers={"User-Agent": UA}, follow_redirects=True, timeout=30)
    picked = collect_samples(client)
    say("ZDJĘCIA", dict(Counter(s[0] for s in picked)))
    rows, times = [], []
    for label, title, url in picked:
        data = client.get(url).content
        time.sleep(0.5)
        t = time.perf_counter()
        try:
            probs = clf.classify(data)
        except Exception as e:  # noqa: BLE001
            say("  pominięte", title[:40], e)
            continue
        times.append(time.perf_counter() - t)
        rows.append((label, title, probs))
    warm = times[1:] or times
    say(f"PAMIĘĆ po analizie: {rss_mb() - before:.0f} MB więcej niż przed wczytaniem modelu")
    say(f"CZAS analizy jednego zdjęcia: mediana {statistics.median(warm) * 1000:.0f} ms, "
        f"najwolniejsze {max(warm) * 1000:.0f} ms (pierwsze, z rozgrzewką: {times[0] * 1000:.0f} ms)")

    cfg = MlConfig()
    phones = [p for lab, _, p in rows if lab == "smartphone"]
    others = [(lab, p) for lab, _, p in rows if lab != "smartphone"]
    top_ok = sum(1 for lab, _, p in rows if max(p, key=p.get) == lab)
    say(f"TRAFNOŚĆ (najwyższa klasa): {top_ok}/{len(rows)}")
    for c in CLASSES:
        sub = [p for lab, _, p in rows if lab == c]
        if sub:
            say(f"   {c:16} {sum(1 for p in sub if max(p, key=p.get) == c)}/{len(sub)}")
    for thr in sorted({0.7, cfg.photo_conflict_conf, 0.9}):
        false_conflict = sum(1 for p in phones if max(v for k, v in p.items() if k != "smartphone") >= thr)
        caught = defaultdict(int)
        for lab, p in others:
            if max(v for k, v in p.items() if k != "smartphone") >= thr:
                caught[lab] += 1
        say(f"   próg sprzeczności {thr:.0%}{' (domyślny)' if thr == cfg.photo_conflict_conf else ''}: "
            f"telefony uznane za akcesorium {false_conflict}/{len(phones)}, wykryte: "
            + ", ".join(f"{c} {caught[c]}/{sum(1 for lab, _ in others if lab == c)}" for c in CLASSES[1:]))
    confirmed = sum(1 for p in phones if p["smartphone"] >= cfg.photo_phone_conf)
    say(f"   telefony potwierdzone (smartfon ≥ {cfg.photo_phone_conf:.0%}): {confirmed}/{len(phones)}")
    for lab, title, p in rows:
        say(f"   [{lab:16}] {title[:48]:48} -> " + " ".join(f"{c}:{p[c]:.2f}" for c in CLASSES))
    return 0


if __name__ == "__main__":
    sys.exit(main())
