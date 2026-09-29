"""Syntetyczny rynek z 90 dni (testy statystyk i zrzuty zakładki „Rynek”).

* iPhone 13 128 GB (sprawne): ceny spadają ~8% w 90 dni, ok. 6 nowych ogłoszeń dziennie,
* iPhone 12 (sprawne, 64/128 GB): ceny stabilne,
* iPhone 11 (uszkodzone): kilka ofert — za mało na część statystyk.
Okazje (KUPUJ / NEGOCJUJ) częściej we wtorki i wieczorem; oferty z niedzieli tańsze o ~5%.
"""
from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from phonebot.storage.repositories import _iso


def fill_market(conn, now: datetime | None = None, seed: int = 7) -> int:
    rnd = random.Random(seed)
    now = now or datetime.now(UTC)
    rows, history = [], []
    n = 0
    for day in range(90, -1, -1):
        base_day = (now - timedelta(days=day)).replace(minute=0, second=0, microsecond=0)
        for model, storage, cond, per_day, base, drift in (
                ("iPhone 13", 128, "good", 6, 1900.0, -0.08),
                ("iPhone 12", 64, "good", 2, 1300.0, 0.0),
                ("iPhone 12", 128, "good", 2, 1400.0, 0.0),
                ("iPhone 11", 64, "damaged", 0.3, 500.0, 0.0)):
            count = int(per_day) + (1 if rnd.random() < per_day - int(per_day) else 0)
            for _ in range(count):
                n += 1
                hour = rnd.choice([9, 12, 15, 18, 19, 20, 21, 21, 22])
                created = base_day.replace(hour=hour)
                if created > now:
                    created = now - timedelta(minutes=5)
                weekday = created.astimezone().weekday()
                level = base * (1 + drift * (90 - day) / 90)
                price = round(level * rnd.uniform(0.85, 1.15) * (0.95 if weekday == 6 else 1.0), -1)
                good = price < level * 0.93 or (weekday == 1 and rnd.random() < 0.5)
                life = rnd.randint(2, 20)
                last = min(now, created + timedelta(days=life))
                active = last >= now - timedelta(hours=1)
                rows.append(("test", f"m{n}", f"https://example.com/{n}", f"{model} {storage}GB", price, cond, model,
                             storage, _iso(created), _iso(created), _iso(last), int(active),
                             ("KUPUJ" if good else "ODPUŚĆ")))
                if rnd.random() < 0.3 and life > 5:  # obniżka ceny w trakcie
                    history.append((n, round(price * 0.95, -1), _iso(created + timedelta(days=3))))
    conn.executemany(
        "INSERT INTO offers (source, source_id, url, title, price, condition, model, storage_gb, created_at, "
        "first_seen, last_seen, is_active, first_verdict) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    ids = {r["source_id"]: r["id"] for r in conn.execute("SELECT id, source_id FROM offers WHERE source = 'test'")}
    conn.executemany("INSERT INTO price_history (offer_id, price, seen_at) VALUES (?, ?, ?)",
                     [(ids[f"m{i}"], r[4], r[9]) for i, r in enumerate(rows, start=1)]
                     + [(ids[f"m{i}"], p, when) for i, p, when in history])
    for i, p, _when in history:  # aktualna cena = po obniżce
        conn.execute("UPDATE offers SET price = ? WHERE id = ?", (p, ids[f"m{i}"]))
    return n
