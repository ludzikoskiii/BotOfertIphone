"""Raport wydajności PhoneBot na pełnej bazie — bez sieci, bez Telegrama, bez zmian w Twoich danych.

    QT_QPA_PLATFORM=offscreen python tools/perf_report.py --json wynik.json            # baza symulowana (60 dni)
    QT_QPA_PLATFORM=offscreen python tools/perf_report.py --db kopia.sqlite3 --json w.json  # KOPIA Twojej bazy

Mierzy: start programu, jeden cykl odświeżania (co ~2 min), procesor między cyklami, przewijanie / filtr / sort,
pamięć po starcie i po wielu cyklach (wycieki), rozmiar bazy i miniatur oraz tempo wzrostu, czas klasyfikatora
tytułów, odpowiedzi wersji na telefon i bota Telegram, zapytania SQL (plan i czas), działania nocne.

Każdy pomiar w osobnym procesie. Sieć jest wyłączona (portal = makieta, miniatury nie są pobierane, Telegram =
atrapa), więc na kopii prawdziwej bazy nic nie trafia na zewnątrz. Na prawdziwej bazie pracuj ZAWSZE na kopii.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SOURCES = ["allegro_lokalnie", "vinted", "sprzedajemy", "lento"]
MODELS = ["iPhone 11", "iPhone 12", "iPhone 12 Pro", "iPhone 13", "iPhone 13 mini", "iPhone 13 Pro", "iPhone 14",
          "iPhone 14 Pro", "iPhone 15", "iPhone 15 Pro Max", "iPhone XR", "iPhone SE 2020", "iPhone 16"]
CITIES = [("Kraków", 50.06, 19.94), ("Nowy Targ", 49.48, 20.03), ("Zakopane", 49.30, 19.95),
          ("Warszawa", 52.23, 21.01), ("Gdańsk", 54.35, 18.65), ("Wrocław", 51.11, 17.03), ("Nowy Sącz", 49.62, 20.69),
          ("Rzeszów", 50.04, 22.00), ("Poznań", 52.41, 16.93), (None, None, None)]
EXTRAS = ["", "", " zbity ekran", " bateria 81%", " nie ładuje", " stan idealny", " pęknięty tył",
          " face id nie działa", " icloud", " na części"]
DESCS = ["Sprzedam telefon w bardzo dobrym stanie, bateria 88%, bez blokad, komplet z pudełkiem i ładowarką. "
         "Możliwa wysyłka paczkomatem albo odbiór osobisty w centrum miasta.",
         "Telefon sprawny, drobne rysy na ramce. Kontakt przez portal.", "Stan dobry.", "",
         "Wymieniony ekran na zamiennik, działa wszystko poza Face ID. Bateria 79%. Cena do negocjacji."]
REFRESH_MIN = 2  # szybkie odświeżanie co ~2 min na portal


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat()


def rss_mb() -> float:
    """Bieżąca pamięć procesu (RSS), nie szczytowa."""
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    try:
        import psutil  # Windows

        return psutil.Process().memory_info().rss / 2**20
    except ImportError:
        import resource

        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024


def cpu_s() -> float:
    return time.process_time()


# --------------------------------------------------------------- baza ---

def build_db(path: Path, days: int, per_day: int, seed: int = 7) -> dict:
    """Baza po ``days`` dniach pracy: ``per_day`` nowych ogłoszeń dziennie, sprzedaż po 1–20 dniach, historia cen,
    skróty zdjęć, wyniki AI, sprzedawcy, odrzucone, przebiegi pobierania co 2 min, powiadomienia, statystyki rynku."""
    from phonebot.core.models import RawOffer
    from phonebot.core.normalizer import parse_offer
    from phonebot.core.settings import Settings
    from phonebot.storage.db import open_database
    from phonebot.storage.repositories import OfferRepository, PartsRepository, SettingsRepository, raw_to_json

    rng = random.Random(seed)
    now = datetime.now(UTC)
    conn = open_database(path)
    PartsRepository(conn).seed_defaults_if_empty()
    s = Settings()
    s.enabled_sources.update({k: k in SOURCES for k in s.enabled_sources})
    SettingsRepository(conn).save(s)
    repo = OfferRepository(conn)
    conn.execute("BEGIN")
    n = days * per_day
    updates, history, hashes, ai, rejected = [], [], [], [], []
    for i in range(n):
        first = now - timedelta(days=days) + timedelta(seconds=rng.uniform(0, days * 86400))
        model = rng.choice(MODELS)
        city, lat, lon = rng.choice(CITIES)
        raw = RawOffer(rng.choice(SOURCES), f"id{i}", f"https://portal.test/{i}",
                       f"{model} {rng.choice([64, 128, 256, 512])}GB{rng.choice(EXTRAS)}",
                       float(rng.randrange(300, 4500, 10)), description=rng.choice(DESCS), city=city, lat=lat, lon=lon,
                       photos=[f"https://img.test/{i}.jpg", f"https://img.test/{i}b.jpg"],
                       shipping_available=rng.random() < 0.7,
                       params={"seller_id": str(rng.randrange(n // 3)), "seller": f"user{rng.randrange(n // 3)}"})
        oid = repo.upsert(raw, parse_offer(raw)).offer_id
        life = timedelta(days=rng.uniform(0.3, 20))
        last = min(now, first + life)
        active = first + life > now
        status = "hidden" if rng.random() < 0.03 else ("watched" if rng.random() < 0.01 else "new")
        verdict = rng.choice(["ODPUŚĆ"] * 6 + ["KUPUJ", "NEGOCJUJ", "NEGOCJUJ", "DO WERYFIKACJI"])
        updates.append((_iso(first), _iso(last), int(active), None if active else "missing", status, verdict, oid))
        for k in range(rng.choice([0, 0, 0, 1, 2])):
            history.append((oid, raw.price + 50 * (k + 1), _iso(first + timedelta(hours=k + 1))))
        hashes.append((raw.source, raw.source_id, raw.photos[0], f"{rng.getrandbits(64):016x}", _iso(first)))
        ai.append((raw.source, raw.source_id, "phone", 0.97, '{"phone": 0.97}', "text-v1"))
        if rng.random() < 0.6:
            rej = RawOffer(raw.source, f"r{i}", f"https://portal.test/r{i}", f"Etui {model}", 30.0)
            rejected.append((raw.source, rej.source_id, rej.url, rej.title, 30.0, "filter", "akcesorium", "etui",
                             raw_to_json(rej), _iso(first)))
    conn.executemany("UPDATE offers SET first_seen = ?, last_seen = ?, is_active = ?, inactive_reason = ?, "
                     "status = ?, first_verdict = ? WHERE id = ?", updates)
    conn.executemany("INSERT INTO price_history (offer_id, price, seen_at) VALUES (?, ?, ?)", history)
    conn.executemany("INSERT OR REPLACE INTO photo_hashes (source, source_id, url, dhash, stock, computed_at) "
                     "VALUES (?, ?, ?, ?, 0, ?)", hashes)
    conn.executemany("INSERT OR REPLACE INTO ai_results (source, source_id, text_label, text_conf, text_probs, "
                     "text_model) VALUES (?, ?, ?, ?, ?, ?)", ai)
    cutoff = _iso(now - timedelta(days=14))  # odrzucone starsze niż okno wyceny są usuwane przy pobieraniu
    conn.executemany("INSERT OR IGNORE INTO rejected_offers (source, source_id, url, title, price, stage, reason, "
                     "keyword, raw_json, rejected_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     [r for r in rejected if r[-1] >= cutoff])
    conn.executemany("INSERT OR IGNORE INTO sellers (source, seller_id, login, country_code, checked_at) "
                     "VALUES (?, ?, ?, 'PL', ?)", [(src, str(k), f"user{k}", _iso(now))
                                                   for k in range(n // 3) for src in SOURCES[:1]])
    # przebiegi pobierania: każdy portal co ~2 min przez cały okres (tak rośnie tabela w programie)
    runs = []
    t = now - timedelta(days=days)
    while t < now:
        for src in SOURCES:
            runs.append((src, _iso(t), _iso(t + timedelta(seconds=3)), "ok", 96, rng.choice([0, 0, 0, 1, 2]), None))
        t += timedelta(minutes=REFRESH_MIN)
    conn.executemany("INSERT INTO fetch_runs (source, started_at, finished_at, status, offers_found, new_offers, "
                     "error) VALUES (?, ?, ?, ?, ?, ?, ?)", runs)
    outbox = [(oid, "new", 1000.0 + oid, f"oferta {oid}", f"tekst {oid}", "sent", _iso(now - timedelta(days=d)),
               _iso(now - timedelta(days=d)), _iso(now - timedelta(days=d)))
              for d in range(days) for oid in rng.sample(range(1, n), 5)]
    conn.executemany("INSERT OR IGNORE INTO telegram_outbox (offer_id, kind, price, headline, text, status, next_try, "
                     "created_at, sent_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", outbox)
    conn.execute("COMMIT")
    archived = repo.archive_old(s.refresh.archive_days)
    from phonebot.services.market_stats import compute

    compute(conn, s)
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("offers", "price_history", "photo_hashes", "ai_results", "rejected_offers", "fetch_runs",
                        "telegram_outbox", "sellers", "market_daily")}
    conn.close()
    return {"rows": counts, "archived": archived}


# ------------------------------------------------------- okno (offscreen) ---

def _no_network() -> None:
    """Okno w pomiarach: bez pobierania miniatur, stron ofert, cen referencyjnych i bez wątku AI."""
    from phonebot.ui import images, main_window

    images.ThumbnailCache._pump = lambda self: None
    main_window.MainWindow._start_open_check = lambda self: None
    main_window.MainWindow.refresh_references = lambda self, *a, **k: False
    main_window.MainWindow.hash_photos = lambda self, *a, **k: False


def _window(db: Path, *, market: bool = False):
    from PySide6.QtWidgets import QApplication

    from phonebot.storage.db import connect
    from phonebot.ui import main_window

    _no_network()
    if not market:
        main_window.MainWindow.refresh_market_stats = lambda self, *a, **k: False
    app = QApplication.instance() or QApplication([])
    conn = connect(db)
    win = main_window.MainWindow(conn, db, thumbs_dir=db.parent / "thumbs", defer_load=True)
    win.resize(1600, 900)
    win.show()
    return app, conn, win


def child_startup(db: Path) -> dict:
    t0 = time.perf_counter()
    c0 = cpu_s()
    from PySide6.QtWidgets import QApplication  # noqa: F401

    from phonebot.ui import main_window  # noqa: F401

    t_import = time.perf_counter() - t0
    t1 = time.perf_counter()
    app, conn, win = _window(db)
    win.repaint()
    t_visible = time.perf_counter() - t1
    while not win.loaded:
        app.processEvents()
    app.processEvents()
    out = {"import_s": round(t_import, 2), "visible_s": round(t_import + t_visible, 2),
           "start_s": round(time.perf_counter() - t0, 2), "start_cpu_s": round(cpu_s() - c0, 2),
           "rows": win.model.rowCount(), "rss_after_start_mb": round(rss_mb(), 1)}

    def timed(name, fn, repeat=3):
        best = 1e9
        for _ in range(repeat):
            t = time.perf_counter()
            fn()
            app.processEvents()
            best = min(best, time.perf_counter() - t)
        out[name] = round(best * 1000, 1)

    from phonebot.core.sorting import SortLevel
    from phonebot.core.view_filter import ViewFilter
    from phonebot.ui.table_model import Col

    timed("reload_ms", win.reload)
    timed("sort_ms", lambda: win.model.set_sort_spec([SortLevel("price", "asc"), SortLevel("model", "asc")]))
    timed("filter_ms", lambda: (win.filters.set_filter(ViewFilter(models=["iPhone 13"])),
                                win.filters.set_filter(ViewFilter())))
    timed("filter_text_ms", lambda: (win.filters.set_filter(ViewFilter(text="pro max")),
                                     win.filters.set_filter(ViewFilter())))
    timed("tab_switch_ms", lambda: (win.list_tabs.setCurrentIndex(1), win.list_tabs.setCurrentIndex(0)))
    for photos in (False, True):
        win.set_column_visible(Col.PHOTO, photos)
        app.processEvents()
        bar = win.table.verticalScrollBar()
        frames = []
        for v in range(0, max(1, bar.maximum()), max(1, bar.maximum() // 80)):
            t = time.perf_counter()
            bar.setValue(v)
            win.table.viewport().repaint()
            frames.append((time.perf_counter() - t) * 1000)
        key = "scroll_photos" if photos else "scroll"
        out[f"{key}_avg_ms"] = round(sum(frames) / len(frames), 2)
        out[f"{key}_max_ms"] = round(max(frames), 2)
    win._quitting = True
    win.close()
    conn.close()
    return out


class _Portal:
    """Makieta wyników wyszukiwania (Vinted): najnowsze na górze, co cykl kilka nowych."""

    def __init__(self, seed: int = 3):
        self.rng = random.Random(seed)
        self.items: list[dict] = []
        self.next_id = 50_000_000
        self.add(200)

    def add(self, count: int) -> None:
        for _ in range(count):
            i = self.next_id
            self.next_id += 1
            model = self.rng.choice(MODELS)
            self.items.insert(0, {"id": i, "title": f"{model} 128GB{self.rng.choice(EXTRAS)}",
                                  "price": {"amount": str(self.rng.randrange(400, 4000, 10)), "currency_code": "PLN"},
                                  "url": f"/items/{i}", "user": {"id": i % 700, "login": f"u{i % 700}"},
                                  "photo": {"url": f"https://img.test/v{i}.jpg"}})

    def handler(self, request):
        import httpx

        from phonebot.sources.vinted import PER_PAGE

        if request.url.path.startswith("/api/v2/users/"):
            uid = int(request.url.path.rsplit("/", 1)[-1])
            return httpx.Response(200, json={"user": {"id": uid, "login": f"u{uid}", "country_code": "PL"}})
        if request.url.host == "www.vinted.pl":
            return httpx.Response(200, text="", headers={"set-cookie": "access_token_web=t; Path=/"})
        page = int(request.url.params.get("page", 1))
        chunk = self.items[(page - 1) * PER_PAGE: page * PER_PAGE]
        return httpx.Response(200, json={"items": chunk, "pagination": {"current_page": page, "total_pages": 30,
                                                                        "per_page": PER_PAGE}})


def _scan_once(db: Path, settings, portal: _Portal):
    """Jeden przebieg szybkiego odświeżania Vinted (jak ScanWorker w wątku: skan + kroki po skanie)."""
    import asyncio

    import httpx

    from phonebot.net.http import HostRateLimiter, HttpClient
    from phonebot.services.post_scan import run_post_scan
    from phonebot.services.scanner import Scanner
    from phonebot.sources.vinted import VintedAdapter
    from phonebot.storage.db import connect

    conn = connect(db)
    try:
        limiter = HostRateLimiter(0)
        scanner = Scanner(conn, settings, limiter,
                          http_factory=lambda: HttpClient(limiter, transport=httpx.MockTransport(portal.handler),
                                                          wait=lambda x: 0),
                          adapter_factory=lambda http, st: [VintedAdapter(http, st)])
        t, c = time.perf_counter(), cpu_s()
        report = asyncio.run(scanner.run(force=True, sources={"vinted"}, incremental=True))
        scan = (time.perf_counter() - t, cpu_s() - c)

        class FakeTelegram:
            def send(self, *a, **k):
                return {}

        t, c = time.perf_counter(), cpu_s()
        report.post = run_post_scan(conn, settings, report, telegram=FakeTelegram())
        post = (time.perf_counter() - t, cpu_s() - c)
        return report, scan, post
    finally:
        conn.close()


def _cycle_settings(conn):
    from phonebot.storage.repositories import SettingsRepository

    s = SettingsRepository(conn).load()
    s.enabled_sources = {k: k == "vinted" for k in s.enabled_sources}
    s.source_min_price = {}
    s.telegram_enabled, s.telegram_bot_token, s.telegram_chat_id = True, "perf", "1"
    s.telegram_quiet_enabled = False
    return s


def child_cycle(db: Path, cycles: int = 5) -> dict:
    """Szybkie odświeżanie z kilkoma nowymi ofertami: skan, kroki po skanie, odświeżenie tabeli w oknie i pierwsza
    strona listy na telefonie zaraz potem (serwer działa w oknie programu, jak u Ciebie)."""
    import httpx

    from phonebot.storage.db import connect
    from phonebot.storage.repositories import SettingsRepository
    from phonebot.web.auth import hash_pin

    c = connect(db)
    st = SettingsRepository(c).load()
    st.web_enabled, st.web_port, st.web_pin_hash = True, 0, hash_pin("2468")
    SettingsRepository(c).save(st)
    c.close()
    app, conn, win = _window(db)
    while not win.loaded:
        app.processEvents()
    phone = httpx.Client(base_url=win.web.local_url(), follow_redirects=True, trust_env=False, timeout=60)
    phone.post("/login", data={"pin": "2468", "next": "/health"})
    s = _cycle_settings(conn)
    portal = _Portal()
    _scan_once(db, s, portal)  # pierwsze pobranie z makiety (oferty już znane) — poza pomiarem
    rows = []
    for _ in range(cycles):
        portal.add(3)
        report, scan, post = _scan_once(db, s, portal)
        t, c = time.perf_counter(), cpu_s()
        win.reload()  # okno po nowych ofertach (_schedule_reload)
        app.processEvents()
        ui = (time.perf_counter() - t, cpu_s() - c)
        t = time.perf_counter()
        phone.get("/").raise_for_status()  # pierwsze otwarcie listy na telefonie po odświeżeniu
        web_ms = (time.perf_counter() - t) * 1000
        rows.append({"new": report.new_count, "scan_ms": scan[0] * 1000, "post_ms": post[0] * 1000,
                     "reload_ms": ui[0] * 1000, "cpu_ms": (scan[1] + post[1] + ui[1]) * 1000,
                     "phone_after_ms": web_ms})
    med = {k: round(sorted(r[k] for r in rows)[len(rows) // 2], 1) for k in rows[0]}
    med["total_ms"] = round(med["scan_ms"] + med["post_ms"] + med["reload_ms"], 1)
    phone.close()
    win._quitting = True
    win.close()
    conn.close()
    return {f"cycle_{k}": v for k, v in med.items()}


def child_idle(db: Path, seconds: int = 60) -> dict:
    """Procesor między cyklami: okno z zegarami jak w programie (harmonogram, telefon, Telegram), bez pobierania."""
    from phonebot.storage.repositories import SettingsRepository
    from phonebot.ui import main_window
    from phonebot.web.auth import hash_pin

    main_window.MainWindow._start_quick = lambda self, source: None  # termin odświeżenia — bez sieci
    main_window.MainWindow._run_check = lambda self, kind: None
    main_window.MainWindow.flush_telegram = lambda self: False
    from phonebot.storage.db import connect

    c = connect(db)
    s = SettingsRepository(c).load()
    s.web_enabled, s.web_port, s.web_pin_hash = True, 0, hash_pin("2468")
    SettingsRepository(c).save(s)
    c.close()
    app, conn, win = _window(db)
    while not win.loaded:
        app.processEvents()
    win.start_refresh_scheduler()
    for _ in range(50):
        app.processEvents()
        time.sleep(0.02)
    from PySide6.QtCore import QTimer

    c0, t0 = cpu_s(), time.perf_counter()
    QTimer.singleShot(seconds * 1000, app.quit)  # zwykła pętla zdarzeń, jak w programie (bez odpytywania)
    app.exec()
    seconds = time.perf_counter() - t0
    used = cpu_s() - c0
    out = {"idle_cpu_pct": round(used / seconds * 100, 2), "idle_cpu_ms_per_min": round(used / seconds * 60000, 0)}
    win._quitting = True
    win.close()
    conn.close()
    return out


def child_soak(db: Path, cycles: int) -> dict:
    """Wiele cykli odświeżania w jednym procesie (≈ godziny pracy) na niezmiennych danych (jak przy archiwum, które
    utrzymuje stałą wielkość tabeli) — stały przyrost pamięci między próbkami = wyciek."""
    import gc

    from PySide6.QtCore import QEvent

    app, conn, win = _window(db)
    while not win.loaded:
        app.processEvents()
    s = _cycle_settings(conn)
    portal = _Portal(seed=5)
    _scan_once(db, s, portal)
    def cycle(i: int) -> None:
        _scan_once(db, s, portal)
        win.reload()
        if i % 10 == 0:  # od czasu do czasu: szczegóły oferty i ustawienia (okna tworzone i zamykane)
            dlg = win.open_settings()
            app.processEvents()
            dlg.reject()
            win.table.selectRow(i % max(1, win.proxy.rowCount()))
        app.processEvents()
        app.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # jak pętla zdarzeń programu: zamknięte okna znikają

    for i in range(30):  # rozgrzewka: pamięci podręczne (Qt, SQLite, wyniki) dochodzą do swojej wielkości
        cycle(i)
    gc.collect()
    samples = [round(rss_mb(), 1)]
    for i in range(cycles):
        cycle(30 + i)
        if (i + 1) % max(1, cycles // 6) == 0:
            gc.collect()
            app.processEvents()
            samples.append(round(rss_mb(), 1))
    hours = cycles * REFRESH_MIN * len(SOURCES) / 60 / len(SOURCES)
    out = {"soak_cycles": cycles, "soak_hours_equiv": round(hours, 1), "soak_rss_samples_mb": samples,
           "soak_rss_growth_mb": round(samples[-1] - samples[0], 1),
           "soak_rss_growth_mb_per_h": round((samples[-1] - samples[0]) / max(hours, 0.1), 2)}
    win._quitting = True
    win.close()
    conn.close()
    return out


# ------------------------------------------------------- telefon i bot ---

def child_web(db: Path) -> dict:
    import httpx

    from phonebot.services.telegram_bot import TelegramBot
    from phonebot.storage.db import connect
    from phonebot.storage.repositories import NotifyProfileRepository, OfferRepository, SettingsRepository
    from phonebot.web.auth import hash_pin
    from phonebot.web.server import WebServer

    conn = connect(db)
    s = SettingsRepository(conn).load()
    s.web_enabled, s.web_port, s.web_pin_hash = True, 0, hash_pin("2468")
    s.telegram_enabled, s.telegram_bot_token, s.telegram_chat_id = True, "perf", "1"
    NotifyProfileRepository(conn).ensure_default(s)
    offer_id = OfferRepository(conn).list()[0].id
    pid = NotifyProfileRepository(conn).all()[0].id
    srv = WebServer(db, s)
    srv.start(s)
    out: dict = {}
    try:
        with httpx.Client(base_url=srv.local_url(), follow_redirects=True, trust_env=False, timeout=60) as c:
            c.post("/login", data={"pin": "2468", "next": "/health"})
            for name, path in (("list", "/"), ("picked", "/?lista=picked"), ("details", f"/oferta/{offer_id}"),
                               ("market", "/rynek"), ("inventory", "/magazyn"), ("profiles", "/powiadomienia"),
                               ("profile_edit", f"/powiadomienia/{pid}")):
                times = []
                for _ in range(3):
                    if name == "list":
                        srv.app.invalidate()  # zimna pamięć podręczna — jak po odświeżeniu ofert
                    t = time.perf_counter()
                    r = c.get(path)
                    times.append((time.perf_counter() - t) * 1000)
                    assert r.status_code == 200, (path, r.status_code)
                out[f"web_{name}_ms"] = round(times[0], 1)
                out[f"web_{name}_warm_ms"] = round(min(times[1:]), 1)
            t = time.perf_counter()
            c.get("/")
            out["web_list_cached_ms"] = round((time.perf_counter() - t) * 1000, 1)
    finally:
        srv.stop()

    class Fake:
        def send(self, text, *, preview_url=None, buttons=None):
            return {}

        def edit(self, *a, **k):
            pass

        def answer_button(self, *a, **k):
            pass

    bot = TelegramBot(conn, s, Fake(), app_state=lambda: {"green": 1})
    for cmd in ("/status", "/profile", "/pauza 1h", "/wznow"):
        t = time.perf_counter()
        bot.handle({"update_id": 1, "message": {"chat": {"id": 1}, "text": cmd}})
        out[f"bot_{cmd.split()[0].strip('/')}_ms"] = round((time.perf_counter() - t) * 1000, 1)
    t = time.perf_counter()
    bot.handle({"update_id": 2, "callback_query": {"id": "x", "data": f"p:{pid}",
                                                   "message": {"chat": {"id": 1}, "message_id": 1}}})
    out["bot_button_ms"] = round((time.perf_counter() - t) * 1000, 1)
    conn.close()
    return out


# ------------------------------------------- jednoczesny dostęp do bazy ---

def child_wal(db: Path, seconds: int = 20) -> dict:
    """Program, serwer na telefon i bot naraz na jednej bazie (WAL): pobieranie z zapisem, odświeżanie tabeli,
    strony na telefonie (także zmiany), komendy bota z przyciskami i porządki z VACUUM w trakcie. Zlicza błędy
    „database is locked” i najdłuższe czekanie każdej czynności."""
    import threading

    import httpx

    from phonebot.services.evaluator import Evaluator
    from phonebot.services.telegram_bot import TelegramBot
    from phonebot.storage.db import connect
    from phonebot.storage.repositories import NotifyProfileRepository, OfferRepository, SettingsRepository
    from phonebot.web.auth import hash_pin
    from phonebot.web.server import WebServer

    conn = connect(db)
    s = _cycle_settings(conn)
    s.web_enabled, s.web_port, s.web_pin_hash = True, 0, hash_pin("2468")
    NotifyProfileRepository(conn).ensure_default(s)
    pid = NotifyProfileRepository(conn).all()[0].id
    conn.close()
    srv = WebServer(db, s)
    srv.start(s)
    stop = threading.Event()
    stats: dict[str, list[float]] = {}
    errors: list[str] = []
    lock = threading.Lock()

    def timed(name, fn):
        t = time.perf_counter()
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            with lock:
                errors.append(f"{name}: {e.__class__.__name__}: {e}"[:160])
        with lock:
            stats.setdefault(name, []).append((time.perf_counter() - t) * 1000)

    def scanner():
        portal = _Portal(seed=9)
        while not stop.is_set():
            portal.add(3)
            timed("pobieranie (zapis)", lambda: _scan_once(db, s, portal))

    def phone():
        with httpx.Client(base_url=srv.local_url(), follow_redirects=True, trust_env=False, timeout=60) as c:
            c.post("/login", data={"pin": "2468", "next": "/health"})
            page = c.get("/powiadomienia").text
            import re

            csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
            while not stop.is_set():
                srv.app.invalidate()
                timed("telefon: lista", lambda: c.get("/").raise_for_status())
                timed("telefon: zmiana", lambda: c.post("/powiadomienia", data={"a": "toggle", "id": pid,
                                                                                 "csrf": csrf}).raise_for_status())

    def bot():
        class Fake:
            def send(self, *a, **k):
                return {}

            def edit(self, *a, **k):
                pass

            def answer_button(self, *a, **k):
                pass

        c = connect(db)
        b = TelegramBot(c, s, Fake())
        while not stop.is_set():
            timed("bot: /status", lambda: b.handle({"update_id": 1, "message": {"chat": {"id": 1},
                                                                                "text": "/status"}}))
            timed("bot: przycisk", lambda: b.handle({"update_id": 2, "callback_query": {
                "id": "x", "data": f"p:{pid}", "message": {"chat": {"id": 1}, "message_id": 1}}}))
            time.sleep(0.2)
        c.close()

    def window():
        c = connect(db)
        while not stop.is_set():
            def reload():
                repo = OfferRepository(c)
                rows = Evaluator(c, s).evaluate_visible(repo.list() + repo.list_picked_inactive())
                repo.mark_picked([o.id for o, _ in rows[:5]])
                SettingsRepository(c).set_value("perf_probe", str(time.time()))
            timed("okno: odświeżenie tabeli", reload)
            time.sleep(0.5)
        c.close()

    threads = [threading.Thread(target=f, daemon=True) for f in (scanner, phone, bot, window)]
    for t in threads:
        t.start()
    time.sleep(seconds / 2)
    c = connect(db)
    try:
        from phonebot.services.maintenance import nightly_maintenance

        timed("porządki z VACUUM", lambda: nightly_maintenance(c, s, force_vacuum=True))
    except ImportError:  # starsza wersja — sam VACUUM
        timed("porządki z VACUUM", lambda: c.execute("VACUUM"))
    c.close()
    time.sleep(seconds / 2)
    stop.set()
    for t in threads:
        t.join(60)
    srv.stop()
    out = {"wal_errors": len(errors), "wal_error_samples": errors[:3],
           "wal_ops": {k: len(v) for k, v in stats.items()},
           "wal_max_ms": {k: round(max(v), 0) for k, v in stats.items()},
           "wal_median_ms": {k: round(sorted(v)[len(v) // 2], 0) for k, v in stats.items()}}
    return out


# ------------------------------------------------------- AI, SQL, noc ---

def child_ai(db: Path) -> dict:
    """Klasyfikator tytułów: trening na danych z bazy, wczytanie z dysku, analiza tytułów (pojedynczo i paczkami)."""
    from phonebot.core.settings import Settings
    from phonebot.ml.text_model import TextClassifier
    from phonebot.services.ai_service import build_training_set
    from phonebot.storage.db import connect

    out: dict = {}
    conn = connect(db)
    titles = [r[0] for r in conn.execute("SELECT title FROM offers ORDER BY id DESC LIMIT 1000")]
    r0 = rss_mb()
    t = time.perf_counter()
    data = build_training_set(conn, Settings())
    examples = getattr(data, "examples", data)
    clf = TextClassifier.train(examples)
    out["ai_train_s"] = round(time.perf_counter() - t, 2)
    folder = db.parent / "models"
    folder.mkdir(exist_ok=True)
    clf.save(folder)
    t = time.perf_counter()
    loaded = TextClassifier.load(folder)
    out["ai_load_ms"] = round((time.perf_counter() - t) * 1000, 1)
    t = time.perf_counter()
    for title in titles[:200]:
        loaded.predict_one(title)
    out["ai_text_one_ms"] = round((time.perf_counter() - t) * 1000 / 200, 2)
    t = time.perf_counter()
    loaded.predict(titles)
    out["ai_text_batch_1000_ms"] = round((time.perf_counter() - t) * 1000, 1)
    out["ai_text_rss_mb"] = round(rss_mb() - r0, 1)
    conn.close()
    return out


QUERIES = {
    "list_table": ("SELECT o.* FROM offers o WHERE o.status != 'hidden' AND o.is_active = 1 AND o.archived_at IS NULL "
                   "ORDER BY o.id", ()),
    "picked_inactive": ("SELECT o.* FROM offers o WHERE o.is_active = 0 AND o.picked_at IS NOT NULL AND "
                        "o.pick_excluded = 0 AND o.status != 'hidden'", ()),
    "market_obs": ("SELECT price, condition, defects, flags, storage_gb, last_seen, is_active FROM offers "
                   "WHERE model = ? AND last_seen >= ?", ("iPhone 13", "{since30}")),
    "seen_24h": ("SELECT o.* FROM offers o WHERE o.first_seen >= ? AND o.status != 'hidden'", ("{since1}",)),
    "outbox_pending": ("SELECT * FROM telegram_outbox WHERE status = 'pending' AND next_try <= ?", ("{now}",)),
    "sent_last_hour": ("SELECT COUNT(*) FROM telegram_outbox WHERE status = 'sent' AND sent_at >= ?", ("{since_h}",)),
    "sent_week_profiles": ("SELECT p.profile_id, COUNT(*) FROM telegram_outbox_profiles p JOIN telegram_outbox o "
                           "ON o.id = p.outbox_id WHERE o.status IN ('sent', 'summarized') AND o.sent_at >= ? "
                           "GROUP BY p.profile_id", ("{since7}",)),
    "page_check": ("SELECT o.* FROM offers o WHERE o.is_active = 1 AND o.status != 'hidden' AND o.archived_at IS NULL "
                   "AND o.first_verdict IN ('KUPUJ', 'NEGOCJUJ') ORDER BY COALESCE(o.checked_at, '') LIMIT 40", ()),
    "archive_old": ("SELECT COUNT(*) FROM offers WHERE archived_at IS NULL AND first_seen < ? AND status != 'watched' "
                    "AND picked_at IS NULL", ("{since3}",)),
    "purge_inactive": ("SELECT COUNT(*) FROM offers WHERE is_active = 0 AND last_seen < ? AND status != 'watched'",
                       ("{since60}",)),
    "photo_dupes": ("SELECT dhash, COUNT(*) FROM photo_hashes WHERE dhash IS NOT NULL GROUP BY dhash "
                    "HAVING COUNT(*) > 1", ()),
}


def child_sql(db: Path) -> dict:
    from phonebot.storage.db import connect

    conn = connect(db)
    out: dict = {}
    now = datetime.now(UTC)
    subst = {"{now}": _iso(now), "{since1}": _iso(now - timedelta(days=1)), "{since_h}": _iso(now - timedelta(hours=1)),
             "{since3}": _iso(now - timedelta(days=3)), "{since7}": _iso(now - timedelta(days=7)),
             "{since30}": _iso(now - timedelta(days=30)), "{since60}": _iso(now - timedelta(days=60))}
    from phonebot.storage.repositories import FetchRunRepository

    best = 1e9
    for _ in range(3):  # status źródeł (co 5 s w oknie, /status w bocie) — metoda programu
        t = time.perf_counter()
        FetchRunRepository(conn).latest_by_source()
        best = min(best, time.perf_counter() - t)
    out["sql_latest_runs_ms"] = round(best * 1000, 2)
    for name, (sql, params) in QUERIES.items():
        p = tuple(subst.get(x, x) for x in params)
        plan = " | ".join(r[3] for r in conn.execute("EXPLAIN QUERY PLAN " + sql, p))
        best = 1e9
        for _ in range(3):
            t = time.perf_counter()
            conn.execute(sql, p).fetchall()
            best = min(best, time.perf_counter() - t)
        out[f"sql_{name}_ms"] = round(best * 1000, 2)
        out[f"plan_{name}"] = plan
    conn.close()
    return out


def child_night(db: Path) -> dict:
    """Prace w tle: statystyki rynku, archiwum, sprzątanie starych ofert (jak w pełnym pobraniu nocą)."""
    from phonebot.services.market_stats import compute
    from phonebot.storage.db import connect
    from phonebot.storage.repositories import OfferRepository, SettingsRepository

    conn = connect(db)
    s = SettingsRepository(conn).load()
    out: dict = {}
    t = time.perf_counter()
    compute(conn, s)
    out["market_stats_ms"] = round((time.perf_counter() - t) * 1000, 1)
    t = time.perf_counter()
    compute(conn, s)
    out["market_stats_again_ms"] = round((time.perf_counter() - t) * 1000, 1)
    repo = OfferRepository(conn)
    t = time.perf_counter()
    repo.archive_old(s.refresh.archive_days)
    out["archive_ms"] = round((time.perf_counter() - t) * 1000, 1)
    try:
        from phonebot.services.maintenance import nightly_maintenance

        t = time.perf_counter()
        res = nightly_maintenance(conn, s, force_vacuum=True)
        out["maintenance_ms"] = round((time.perf_counter() - t) * 1000, 1)
        out["maintenance"] = res
    except ImportError:
        out["maintenance"] = "brak (wersja bez sprzątania nocnego)"
    conn.close()
    return out


def db_stats(db: Path) -> dict:
    import sqlite3

    conn = sqlite3.connect(str(db))
    page = conn.execute("PRAGMA page_size").fetchone()[0]
    pages = conn.execute("PRAGMA page_count").fetchone()[0]
    free = conn.execute("PRAGMA freelist_count").fetchone()[0]
    out = {"db_mb": round(db.stat().st_size / 2**20, 2), "db_free_pct": round(100 * free / max(pages, 1), 1)}
    try:
        sizes = conn.execute("SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC LIMIT 8").fetchall()
        out["db_largest_mb"] = {n: round(b / 2**20, 2) for n, b in sizes}
    except sqlite3.OperationalError:
        pass
    out["db_rows"] = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                      for t in ("offers", "fetch_runs", "price_history", "photo_hashes", "ai_results",
                                "rejected_offers", "telegram_outbox", "market_daily")}
    out["table_rows"] = conn.execute("SELECT COUNT(*) FROM offers WHERE is_active = 1 AND archived_at IS NULL "
                                     "AND status != 'hidden'").fetchone()[0]
    out["active_rows"] = conn.execute("SELECT COUNT(*) FROM offers WHERE is_active = 1 AND status != 'hidden'"
                                      ).fetchone()[0]
    del page
    conn.close()
    return out


def growth(db: Path, days: int, per_day: int) -> dict:
    """Tempo wzrostu bazy i miniatur: dzień pracy dopisany do kopii bazy (oferty, historia, przebiegi co 2 min)."""
    from PySide6.QtCore import QSize
    from PySide6.QtGui import QColor, QPixmap
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    copy = db.parent / "growth.sqlite3"
    shutil.copy(db, copy)
    before = copy.stat().st_size
    build_extra_day(copy, per_day)
    import sqlite3

    c = sqlite3.connect(str(copy))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    after = copy.stat().st_size
    copy.unlink()
    for ext in ("-wal", "-shm"):
        Path(str(copy) + ext).unlink(missing_ok=True)
    # miniatura w tabeli (72×54 JPG) i zdjęcie w szczegółach — typowe rozmiary plików z pamięci podręcznej
    tmp = db.parent / "t.jpg"
    pm = QPixmap(QSize(72, 54))
    pm.fill(QColor(120, 130, 140))
    pm.save(str(tmp), "JPG", 85)
    thumb_kb = max(3.5, tmp.stat().st_size / 1024)  # prawdziwe zdjęcia mają więcej szczegółów niż jednolite tło
    tmp.unlink()
    return {"db_growth_mb_per_day": round((after - before) / 2**20, 2),
            "thumbs_mb_per_day": round(per_day * thumb_kb / 1024, 2),
            "thumbs_mb_after_30_days": round(per_day * 30 * thumb_kb / 1024, 1)}


def build_extra_day(path: Path, per_day: int) -> None:
    from phonebot.core.models import RawOffer
    from phonebot.core.normalizer import parse_offer
    from phonebot.storage.db import connect
    from phonebot.storage.repositories import OfferRepository

    rng = random.Random(99)
    conn = connect(path)
    repo = OfferRepository(conn)
    now = datetime.now(UTC)
    conn.execute("BEGIN")
    for i in range(per_day):
        model = rng.choice(MODELS)
        raw = RawOffer(rng.choice(SOURCES), f"x{i}", f"https://portal.test/x{i}", f"{model} 128GB", 1500.0,
                       description=rng.choice(DESCS), photos=[f"https://img.test/x{i}.jpg"],
                       params={"seller_id": str(i)})
        oid = repo.upsert(raw, parse_offer(raw)).offer_id
        conn.execute("INSERT INTO price_history (offer_id, price, seen_at) VALUES (?, 1550, ?)", (oid, _iso(now)))
        conn.execute("INSERT OR REPLACE INTO photo_hashes (source, source_id, url, dhash, stock, computed_at) "
                     "VALUES (?, ?, ?, 'ab', 0, ?)", (raw.source, raw.source_id, raw.photos[0], _iso(now)))
    t = now
    for _ in range(24 * 60 // REFRESH_MIN):
        for src in SOURCES:
            conn.execute("INSERT INTO fetch_runs (source, started_at, finished_at, status, offers_found, new_offers) "
                         "VALUES (?, ?, ?, 'ok', 96, 0)", (src, _iso(t), _iso(t)))
        t += timedelta(minutes=REFRESH_MIN)
    conn.execute("COMMIT")
    conn.close()


# ----------------------------------------------------------------- main ---

CHILDREN = {"startup": child_startup, "cycle": child_cycle, "idle": child_idle, "web": child_web, "ai": child_ai,
            "sql": child_sql, "night": child_night, "wal": child_wal}


def run_child(name: str, db: Path, extra: list[str] | None = None) -> dict:
    cmd = [sys.executable, __file__, "--child", name, "--db", str(db), *(extra or [])]
    env = {**os.environ, "PYTHONPATH": str(ROOT), "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen")}
    proc = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if proc.returncode:
        print(proc.stderr[-3000:], file=sys.stderr)
        return {f"{name}_error": proc.stderr.strip().splitlines()[-1] if proc.stderr.strip() else "błąd"}
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="KOPIA bazy (np. phonebot.sqlite3 skopiowana do innego folderu)")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--per-day", type=int, default=300)
    ap.add_argument("--soak", type=int, default=90, help="cykle odświeżania w teście pamięci (90 ≈ 3 h)")
    ap.add_argument("--idle", type=int, default=60, help="sekundy pomiaru procesora między cyklami")
    ap.add_argument("--only", help="tylko wybrane pomiary, np. startup,sql")
    ap.add_argument("--json")
    ap.add_argument("--child", help=argparse.SUPPRESS)
    ap.add_argument("--cycles", type=int, default=5, help=argparse.SUPPRESS)
    args = ap.parse_args()
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if args.child:
        db = Path(args.db)
        if args.child == "soak":
            print(json.dumps(child_soak(db, args.soak)))
        elif args.child == "idle":
            print(json.dumps(child_idle(db, args.idle)))
        else:
            print(json.dumps(CHILDREN[args.child](db)))
        return 0

    tmp = Path(tempfile.mkdtemp(prefix="phonebot_perf_"))
    os.environ["PHONEBOT_HOME"] = str(tmp / "home")  # ustawienia i sekrety pomiaru — nie Twoje
    db = tmp / "perf.sqlite3"
    result: dict = {"when": datetime.now().isoformat(timespec="minutes")}
    from phonebot import __version__

    result["version"] = __version__
    if args.db:
        shutil.copy(args.db, db)
        print(f"kopia bazy: {db}", file=sys.stderr)
    else:
        t = time.perf_counter()
        info = build_db(db, args.days, args.per_day)
        result["build"] = info
        print(f"baza: {args.days} dni × {args.per_day} ofert ({time.perf_counter() - t:.0f} s)", file=sys.stderr)
    import sqlite3

    c = sqlite3.connect(str(db))
    c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    c.close()
    pristine = tmp / "pristine.sqlite3"
    shutil.copy(db, pristine)
    result.update(db_stats(db))
    only = set(args.only.split(",")) if args.only else None

    def fresh() -> Path:  # każdy pomiar na tej samej bazie wyjściowej
        shutil.copy(pristine, db)
        for ext in ("-wal", "-shm"):
            Path(str(db) + ext).unlink(missing_ok=True)
        return db

    plan = [("startup", []), ("startup", []), ("cycle", []), ("idle", ["--idle", str(args.idle)]), ("web", []),
            ("sql", []), ("ai", []), ("wal", []), ("soak", ["--soak", str(args.soak)]), ("night", [])]
    first = True
    for name, extra in plan:
        if only and name not in only:
            continue
        print(f"… {name}", file=sys.stderr)
        data = run_child(name, fresh(), extra)
        if name == "startup" and first:  # pierwszy start (zimne pliki) — drugi to typowy
            first = False
            result["first_start_s"] = data.get("start_s")
            continue
        result.update(data)
    if not only or "growth" in only:
        result.update(growth(fresh(), args.days, args.per_day))
    for k, v in result.items():
        if not k.startswith("plan_"):
            print(f"{k:34} {v}")
    if args.json:
        Path(args.json).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
