from datetime import datetime, timedelta, timezone

from phonebot.core.models import Defect, Mode, OfferStatus, Verdict
from phonebot.core.normalizer import parse_offer
from phonebot.core.parts import PartPrice
from phonebot.core.settings import Settings
from phonebot.services.evaluator import Evaluator
from phonebot.storage.db import MIGRATIONS
from phonebot.storage.repositories import (
    FetchRunRepository,
    OfferRepository,
    PartsRepository,
    SettingsRepository,
)

from .conftest import make_raw


def save(repo, raw, seen=None):
    return repo.upsert(raw, parse_offer(raw), seen)


def test_schema_version(conn):
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_upsert_new_and_update_with_price_history(conn):
    repo = OfferRepository(conn)
    raw = make_raw("iPhone 13 128GB", 1500, source="allegro_lokalnie", source_id="A1", photos=["p.jpg"])
    first = save(repo, raw)
    assert first.is_new
    raw.price = 1400
    second = save(repo, raw)
    assert not second.is_new and second.price_changed and second.old_price == 1500
    assert second.offer_id == first.offer_id
    assert [p for _, p in repo.price_history(first.offer_id)] == [1500, 1400]
    third = save(repo, raw)
    assert not third.price_changed
    assert len(repo.list()) == 1


def test_roundtrip_offer(conn):
    repo = OfferRepository(conn)
    raw = make_raw("iPhone 13 128GB zbity ekran", 900, description="bateria 70%, cena do negocjacji",
                   city="Nowy Targ", params={"condition": "Uszkodzone"})
    res = save(repo, raw)
    offer = repo.get(res.offer_id)
    assert offer.parsed.model == "iPhone 13"
    assert set(offer.parsed.defects) == {Defect.SCREEN, Defect.BATTERY}
    assert offer.parsed.negotiable is True
    assert offer.raw.params == {"condition": "Uszkodzone"}
    assert offer.raw.shipping_available is True


def test_status_and_hidden_filter(conn):
    repo = OfferRepository(conn)
    a = save(repo, make_raw("iPhone 13", 1000)).offer_id
    b = save(repo, make_raw("iPhone 14", 1000)).offer_id
    repo.set_status(a, OfferStatus.HIDDEN)
    repo.set_status(b, OfferStatus.WATCHED)
    assert [o.id for o in repo.list()] == [b]
    assert len(repo.list(include_hidden=True)) == 2
    assert repo.get(b).status is OfferStatus.WATCHED


def test_notified(conn):
    repo = OfferRepository(conn)
    oid = save(repo, make_raw("iPhone 13", 1000)).offer_id
    assert not repo.was_notified(oid)
    repo.mark_notified(oid)
    assert repo.was_notified(oid)


def test_deactivate_missing(conn):
    repo = OfferRepository(conn)
    old = datetime.now(timezone.utc) - timedelta(days=2)
    save(repo, make_raw("iPhone 13", 1000, source="allegro_lokalnie"), seen=old)
    save(repo, make_raw("iPhone 14", 1000, source="allegro_lokalnie"))
    assert repo.deactivate_missing("allegro_lokalnie", datetime.now(timezone.utc) - timedelta(days=1)) == 1
    assert len(repo.list()) == 1


def test_market_observations_window_and_cross_portal_dedup(conn):
    repo = OfferRepository(conn)
    now = datetime.now(timezone.utc)
    save(repo, make_raw("iPhone 13 128GB", 1500, source="allegro_lokalnie", city="Kraków"))
    save(repo, make_raw("iPhone 13 128GB", 1500, source="vinted", city="Kraków"))  # ta sama sztuka
    save(repo, make_raw("iPhone 13 128GB", 1600, source="allegro_lokalnie", city="Zakopane"))
    save(repo, make_raw("iPhone 13 128GB", 900, source="allegro_lokalnie"), seen=now - timedelta(days=60))  # za stara
    save(repo, make_raw("iPhone 14 128GB", 2000, source="allegro_lokalnie"))
    obs = repo.market_observations("iPhone 13", window_days=30)
    assert sorted(o.price for o in obs) == [1500, 1600]


def test_parts_seed_and_edit(conn):
    repo = PartsRepository(conn)
    n = repo.seed_defaults_if_empty()
    assert n > 100
    assert repo.seed_defaults_if_empty() == 0
    repo.upsert(PartPrice("iPhone 13", Defect.SCREEN, 222.0, "mój dostawca"))
    row = next(r for r in repo.all() if r.model == "iPhone 13" and r.part is Defect.SCREEN)
    assert (row.price, row.note) == (222.0, "mój dostawca")
    repo.delete("iPhone 13", Defect.SCREEN)
    assert not any(r.model == "iPhone 13" and r.part is Defect.SCREEN for r in repo.all())


def test_settings_persist(conn):
    repo = SettingsRepository(conn)
    assert repo.load() == Settings()
    s = Settings(refresh_minutes=7)
    repo.save(s)
    repo.save(s)
    assert repo.load().refresh_minutes == 7


def test_fetch_runs(conn):
    repo = FetchRunRepository(conn)
    run = repo.start("allegro_lokalnie")
    repo.finish(run, found=10, new=3, error=None)
    err = repo.start("vinted")
    repo.finish(err, found=0, new=0, error="HTTP 403")
    rows = repo.last_runs()
    assert [(r["source"], r["status"]) for r in rows] == [("vinted", "error"), ("allegro_lokalnie", "ok")]


def test_evaluator_end_to_end(conn):
    PartsRepository(conn).seed_defaults_if_empty()
    repo = OfferRepository(conn)
    for i, price in enumerate([1800, 1850, 1900, 1950, 2000, 2050]):
        save(repo, make_raw("iPhone 13 128GB", price, city=f"Miasto{i}", description="sprawny"))
    target = save(repo, make_raw("iPhone 13 128GB zbity ekran", 800, city="Nowy Targ", lat=49.48, lon=20.03))
    ev = Evaluator(conn, Settings())
    offer = repo.get(target.offer_id)
    v = ev.evaluate(offer, Mode.REPAIR)
    assert v.market.sample_size == 6
    assert v.market.value == 1925 * 0.9
    assert v.verdict is Verdict.BUY
    assert offer.distance_km is not None and 20 < offer.distance_km < 40


def test_upgrade_from_v1_keeps_offers(tmp_path):
    import sqlite3

    from phonebot.storage.db import connect, migrate

    path = tmp_path / "old.sqlite3"
    old = sqlite3.connect(path)
    for stmt in (s.strip() for s in MIGRATIONS[0].split(";")):
        if stmt:
            old.execute(stmt)
    old.execute("PRAGMA user_version = 1")
    old.execute("INSERT INTO offers (source, source_id, url, title, price, condition, first_seen, last_seen) "
                "VALUES ('vinted', '1', 'u', 'iPhone 13', 1000, 'good', '2026-09-01T00:00:00+00:00', "
                "'2026-09-01T00:00:00+00:00')")
    old.commit()
    old.close()
    conn = connect(path)
    assert migrate(conn) == len(MIGRATIONS)
    offer = OfferRepository(conn).get(1)
    assert offer.raw.title == "iPhone 13" and offer.ai_note is None


def test_migration_v3_hides_old_olx_offers(tmp_path):
    import sqlite3

    from phonebot.storage.db import connect, migrate

    path = tmp_path / "v2.sqlite3"
    old = sqlite3.connect(path)
    for script in MIGRATIONS[:2]:
        for stmt in (s.strip() for s in script.split(";")):
            if stmt:
                old.execute(stmt)
    old.execute("PRAGMA user_version = 2")
    for src, sid in (("olx", "1"), ("vinted", "2")):
        old.execute("INSERT INTO offers (source, source_id, url, title, price, model, storage_gb, condition, "
                    "first_seen, last_seen) "
                    f"VALUES ('{src}', '{sid}', 'u', 'iPhone 13 128GB', 1000, 'iPhone 13', 128, 'good', "
                    "'2026-09-20T00:00:00+00:00', '2026-09-20T00:00:00+00:00')")
    old.commit()
    old.close()
    conn = connect(path)
    migrate(conn)
    repo = OfferRepository(conn)
    assert [o.raw.source for o in repo.list()] == ["vinted"]  # OLX znika z listy
    # ceny z OLX nadal zasilają wycenę rynkową
    now = datetime(2026, 9, 25, tzinfo=timezone.utc)
    assert len(repo.market_observations("iPhone 13", window_days=30, now=now)) == 2


def test_purge_inactive_removes_old_offers_and_history(conn):
    repo = OfferRepository(conn)
    old = datetime.now(timezone.utc) - timedelta(days=120)
    gone = save(repo, make_raw("iPhone 11 64GB", 700, source_id="OLD"), old).offer_id
    kept_watched = save(repo, make_raw("iPhone 12 64GB", 900, source_id="W"), old).offer_id
    fresh = save(repo, make_raw("iPhone 13 128GB", 1500, source_id="NEW")).offer_id
    repo.set_status(kept_watched, OfferStatus.WATCHED)
    repo.deactivate_missing("test", datetime.now(timezone.utc) - timedelta(days=7))
    assert repo.purge_inactive(60) == 1
    assert repo.get(gone) is None and repo.get(kept_watched) and repo.get(fresh)
    assert conn.execute("SELECT COUNT(*) FROM price_history WHERE offer_id = ?", (gone,)).fetchone()[0] == 0
