"""Skróty zdjęć (w tle) do wykrywania oszustw: to samo zdjęcie w kilku ogłoszeniach, zdjęcia katalogowe.

* **dHash** (64 bity, Pillow — bez dodatkowych bibliotek): zdjęcie pomniejszone do 9×8 w skali szarości,
  bit = czy piksel jest jaśniejszy od sąsiada. Te same lub prawie te same zdjęcia (inny rozmiar, lekka
  kompresja) różnią się o kilka bitów.
* **Zdjęcie katalogowe**: jednolite, jasne tło przy całych krawędziach obrazka (typowe dla zdjęć producenta),
  a nie zdjęcie zrobione telefonem na stole.

Wyniki zapisywane po ID ogłoszenia (``photo_hashes``), liczone raz; pobieranie z limitem zapytań.
"""
from __future__ import annotations

import io
import logging
import sqlite3
import time
from datetime import UTC, datetime

import httpx
from PIL import Image, ImageStat

from ..core.settings import Settings

log = logging.getLogger(__name__)

MAX_BYTES = 4_000_000
DELAY_S = 1.0  # między zdjęciami z tego samego serwera


def dhash(img: Image.Image) -> int:
    small = img.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    px = small.tobytes()  # 72 bajty jasności (tryb „L”)
    bits = 0
    for row in range(8):
        for col in range(8):
            bits = (bits << 1) | (px[row * 9 + col] > px[row * 9 + col + 1])
    return bits


def looks_like_stock(img: Image.Image) -> bool:
    """Białe/jednolite tło wzdłuż całej krawędzi obrazka = zdjęcie katalogowe (render producenta)."""
    rgb = img.convert("RGB").resize((96, 96))
    w, h = rgb.size
    border = [rgb.crop(box) for box in ((0, 0, w, 4), (0, h - 4, w, h), (0, 0, 4, h), (w - 4, 0, w, h))]
    for part in border:
        stat = ImageStat.Stat(part)
        if min(stat.mean) < 235 or max(stat.stddev) > 10:
            return False
    return True


def analyze(data: bytes) -> tuple[int, bool]:
    with Image.open(io.BytesIO(data)) as img:
        img.load()
        return dhash(img), looks_like_stock(img)


def pending(conn: sqlite3.Connection, limit: int) -> list[sqlite3.Row]:
    """Aktywne oferty ze zdjęciem, bez skrótu (najnowsze najpierw)."""
    return conn.execute(
        """SELECT o.source, o.source_id, o.photos FROM offers o
           LEFT JOIN photo_hashes h ON h.source = o.source AND h.source_id = o.source_id
           WHERE o.is_active = 1 AND o.status != 'hidden' AND o.photos != '[]' AND h.source IS NULL
           ORDER BY o.first_seen DESC LIMIT ?""", (limit,)).fetchall()


def hash_pending(conn: sqlite3.Connection, settings: Settings, *, client: httpx.Client | None = None,
                 sleep=time.sleep, stop=lambda: False) -> int:
    """Liczy skróty dla ``settings.fraud.photos_per_run`` zdjęć. Zwraca liczbę policzonych."""
    import json

    own = client is None
    client = client or httpx.Client(timeout=15, follow_redirects=True,
                                    headers={"User-Agent": "Mozilla/5.0 PhoneBot (analiza zdjęć)"})
    done = 0
    last: dict[str, float] = {}
    try:
        for row in pending(conn, settings.fraud.photos_per_run):
            if stop():
                break
            url = (json.loads(row["photos"]) or [None])[0]
            if not url:
                continue
            host = httpx.URL(url).host
            wait = DELAY_S - (time.monotonic() - last.get(host, 0))
            if wait > 0:
                sleep(wait)
            last[host] = time.monotonic()
            hv, stock, error = None, False, None
            try:
                r = client.get(url)
                if r.status_code != 200 or len(r.content) > MAX_BYTES:
                    error = f"HTTP {r.status_code}"
                else:
                    h, stock = analyze(r.content)
                    hv = f"{h:016x}"
            except (httpx.HTTPError, OSError, ValueError) as e:
                error = e.__class__.__name__
            conn.execute("INSERT OR REPLACE INTO photo_hashes (source, source_id, url, dhash, stock, computed_at, error) "
                         "VALUES (?, ?, ?, ?, ?, ?, ?)", (row["source"], row["source_id"], url, hv, int(stock),
                                                          datetime.now(UTC).isoformat(), error))
            done += 1
    finally:
        if own:
            client.close()
    return done


def hash_in_background(db_path, settings: Settings) -> int:
    from ..storage.db import connect

    conn = connect(db_path)
    try:
        return hash_pending(conn, settings)
    finally:
        conn.close()
