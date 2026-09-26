"""Etap 1 — zabezpieczenia regułowe. Przykłady z zaobserwowanych problemów (zrzut ekranu z Vinted):

1. oferty z innych krajów / w innych językach („13 14 15 terakota skal obal”),
2. tytuły z kilkoma generacjami („13 14 15”, „12/13/14”),
3. nieznana pamięć i cena 66–170 zł za iPhone'a 13 → KUPUJ,
4. flagi ostrzegawcze nie obniżały werdyktu,
5. zysk 552% bez ostrzeżenia,
6. wiele niemal identycznych tanich ofert jednego sprzedawcy.
"""
import asyncio
import os

import httpx
import pytest

from phonebot.core.language import detect_language
from phonebot.core.listing_filter import ListingFilter, ListingFilterConfig, multi_generation
from phonebot.core.market import estimate_market_value, trim_low
from phonebot.core.models import (
    Condition,
    MarketEstimate,
    MarketObservation,
    Mode,
    OfferStatus,
    RawOffer,
    RedFlag,
    RowColor,
    Verdict,
)
from phonebot.core.normalizer import parse_offer
from phonebot.core.parts import PartsCatalog, default_parts
from phonebot.core.sanity import SanityConfig
from phonebot.core.settings import Settings
from phonebot.core.valuation import evaluate
from phonebot.net.http import HostRateLimiter, HttpClient
from phonebot.services.offer_guard import OfferGuard
from phonebot.services.scanner import Scanner
from phonebot.storage.db import open_database
from phonebot.storage.repositories import OfferRepository, PartsRepository, RejectedRepository, SellerRepository

from .conftest import make_offer

PARTS = PartsCatalog(default_parts())


def market(value):
    return MarketEstimate(value, 12, "test", "wysoka", value)


# ------------------------------------------------ 1. kraj / język (Vinted) ---

@pytest.mark.parametrize("title, lang", [
    ("13 14 15 terakota skal obal", "cs/sk"),  # przykład ze zrzutu ekranu
    ("Prodám iPhone 11 64GB", "cs/sk"),
    ("iPhone 12 mini 64GB modrá", "cs/sk"),
    ("Hülle iPhone 13", "de"),
    ("Parduodu iPhone 12", "lt/lv"),
    ("Apple iPhone 16e 128GB Black AT&T Only Locked Smartphone Great Condition", "en"),
    ("Coque iPhone 14", "fr/es/it"),
])
def test_foreign_titles_detected(title, lang):
    guess = detect_language(title)
    assert guess.foreign and guess.language == lang, guess


@pytest.mark.parametrize("title", [
    "iPhone 13 128GB", "iPhone 13 128 GB, Stan idealny", "iPhone 11 bateria 88%", "iPhone 13 zbity ekran",
    "iPhone XR na części", "iPhone 12 Pro Graphite 128GB", "iPhone 13 Midnight", "iPhone 15 Pro new sealed",
    "iPhone 12 defekt", "iPhone 11 jako nowy", "iPhone 13 bez blokad", "Iphone 14 na gwarancji",
])
def test_polish_or_neutral_titles_are_not_foreign(title):
    assert not detect_language(title).foreign


# ------------------------------------------------- 2. kilka generacji ---

@pytest.mark.parametrize("title", [
    "13 14 15 terakota skal obal", "Etui iPhone 12/13/14", "iPhone 11, 12, 13", "iPhone 7 8 SE",
    "iPhone X XS XR szkło", "Case iPhone 11 Pro/12 Pro/13 Pro", "iPhone 13 i 14", "iPhone 14 plus 13 pro max obal",
])
def test_multiple_generations_rejected_as_accessory(title):
    f = ListingFilter()
    d = f.check(title, model=parse_offer(RawOffer("x", "1", "u", title, 100)).model)
    assert not d.accepted and d.stage in ("multi_model", "accessory"), d
    assert multi_generation(title)


@pytest.mark.parametrize("title", [
    "iPhone 11, 8 GB", "iPhone 12, 11 miesięcy gwarancji", "iPhone 12 stan 8/10", "iPhone 11, 8/10",
    "IPHONE APPLE 8  IOS 16.7.16", "iPhone 15 / 15 Pro", "iPhone SE 2020 64GB", "iPhone 14 Pro 128GB 5 szt",
    "iPhone 12 64 GB 2 lata gwarancji", "iPhone 16e 128GB", "Iphone 18 pro 256 burgund",
])
def test_single_generation_titles_pass(title):
    assert multi_generation(title) is None
    f = ListingFilter()
    assert f.check(title, model=parse_offer(RawOffer("x", "1", "u", title, 1000)).model).accepted


def test_multi_generation_rule_can_be_disabled():
    f = ListingFilter(ListingFilterConfig(multi_model_reject=False))
    assert f.check("iPhone 13 i 14", model="iPhone 13").accepted


@pytest.mark.parametrize("title", [
    "Hülle iPhone 13", "Dėklas iPhone 12", "Kryt na iPhone 13", "Pouzdro iPhone 11 Pro", "Coque iPhone 14",
    "Funda iPhone 12", "Panzerglas iPhone 15", "Obal iPhone 13 mini", "iPhone 13 Pro Max case",
])
def test_foreign_accessory_words(title):
    d = ListingFilter().check(title, model=parse_offer(RawOffer("x", "1", "u", title, 100)).model)
    assert not d.accepted and d.stage == "accessory", d


@pytest.mark.parametrize("title", ["iPhone 13 128GB with case", "iPhone 12 64GB mit Hülle", "iPhone 13 128GB + obal"])
def test_foreign_addons_do_not_reject_phone(title):
    assert ListingFilter().check(title, model=parse_offer(RawOffer("x", "1", "u", title, 1000)).model).accepted


def test_saved_word_lists_get_new_foreign_words_once():
    import json

    from phonebot.core.listing_filter import FOREIGN_ACCESSORIES

    data = json.loads(Settings().to_json())
    lf = data["listing_filter"]
    lf["accessory_words"] = [w for w in lf["accessory_words"] if w not in FOREIGN_ACCESSORIES and w != "adapter"]
    del lf["defaults_version"]  # ustawienia zapisane przez wersję 1.1
    upgraded = Settings.from_json(json.dumps(data))
    assert "obal" in upgraded.listing_filter.accessory_words and "Hülle" in upgraded.listing_filter.accessory_words
    assert "adapter" not in upgraded.listing_filter.accessory_words  # słowo usunięte przez użytkownika nie wraca


# ------------------------------------- 3–5. werdykt: testy sensowności ---

def test_problem3_unknown_storage_and_cheap_price_is_never_buy():
    offer = make_offer("iPhone 13", price=83)  # jak na zrzucie: „iPhone 13 ?” za 83 zł
    v = evaluate(offer, market(1500), PARTS, Settings(), Mode.REPAIR)
    assert v.verdict is Verdict.VERIFY
    assert RedFlag.PRICE_UNREALISTIC in v.flags and RedFlag.STORAGE_UNKNOWN in v.flags
    assert v.color is not RowColor.GREEN


def test_unknown_storage_alone_caps_at_verify_and_is_configurable():
    offer = make_offer("iPhone 13", price=1000)
    assert evaluate(offer, market(2000), PARTS, Settings(), Mode.RESELL).verdict is Verdict.VERIFY
    s = Settings(sanity=SanityConfig(unknown_storage_verify=False))
    assert evaluate(make_offer("iPhone 13", price=1000), market(2000), PARTS, s, Mode.RESELL).verdict is Verdict.BUY


def test_price_sanity_threshold_is_editable():
    offer = make_offer("iPhone 13 128GB", price=500)  # 25% wartości rynkowej
    v = evaluate(offer, market(2000), PARTS, Settings(), Mode.RESELL)
    assert v.verdict is Verdict.VERIFY and RedFlag.PRICE_UNREALISTIC in v.flags
    s = Settings(sanity=SanityConfig(price_min_ratio_working=0.20, profit_max_pct=1000))  # oba progi edytowalne
    v = evaluate(make_offer("iPhone 13 128GB", price=500), market(2000), PARTS, s, Mode.RESELL)
    assert RedFlag.PRICE_UNREALISTIC not in v.flags and v.verdict is Verdict.NEGOTIATE  # została „podejrzanie tanio”


def test_damaged_phone_has_lower_price_threshold():
    offer = make_offer("iPhone 13 128GB zbity ekran", price=400)  # 20% — normalna okazja do naprawy
    v = evaluate(offer, market(2000), PARTS, Settings(), Mode.REPAIR)
    assert RedFlag.PRICE_UNREALISTIC not in v.flags


def test_problem4_every_flag_caps_the_verdict():
    s = Settings()
    no_photos = make_offer("iPhone 13 128GB", price=1000, photos=[])  # miękka flaga
    v = evaluate(no_photos, market(2000), PARTS, s, Mode.RESELL)
    assert RedFlag.NO_PHOTOS in v.flags and v.verdict is Verdict.NEGOTIATE
    assert "ostrzeżenie" in v.negotiation.note
    locked = make_offer("iPhone 13 128GB", price=1000, description="blokada icloud")  # poważna flaga
    assert evaluate(locked, market(2000), PARTS, s, Mode.RESELL).verdict is Verdict.VERIFY
    s.sanity.soft_flag_cap = Verdict.BUY.value  # limit edytowalny
    v = evaluate(make_offer("iPhone 13 128GB", price=1000, photos=[]), market(2000), PARTS, s, Mode.RESELL)
    assert v.verdict is Verdict.BUY


def test_problem5_huge_profit_is_verify():
    offer = make_offer("iPhone 13 128GB zbity ekran", price=350)  # zysk ~190%
    v = evaluate(offer, market(2000), PARTS, Settings(), Mode.REPAIR)
    assert v.roi_pct > 150
    assert RedFlag.PROFIT_UNREALISTIC in v.flags and v.verdict is Verdict.VERIFY
    assert any("obniżony" in r for r in v.reasons)
    s = Settings(sanity=SanityConfig(profit_max_pct=1000))
    v = evaluate(make_offer("iPhone 13 128GB zbity ekran", price=350), market(2000), PARTS, s, Mode.REPAIR)
    assert RedFlag.PROFIT_UNREALISTIC not in v.flags and v.verdict is Verdict.BUY


def test_verdict_order():
    assert Verdict.BUY.rank > Verdict.NEGOTIATE.rank > Verdict.VERIFY.rank > Verdict.SKIP.rank


# --------------------------------------------------- czyste dane rynkowe ---

def _obs(price, storage=128, cond=Condition.GOOD, i=0):
    return MarketObservation(price, storage, cond, i)


def test_unknown_storage_offers_do_not_poison_market_value():
    junk = [_obs(p, None, i=i) for i, p in enumerate([66, 80, 83, 83, 83, 120, 170])]  # akcesoria bez „GB”
    phones = [_obs(p, 128, i=100 + i) for i, p in enumerate([1500, 1550, 1600, 1650, 1700])]
    est = estimate_market_value("iPhone 13", None, "used", junk + phones, Settings())
    assert est.value is not None and est.value > 1200  # wcześniej ~690 zł (mediana śmieci)
    assert est.confidence == "niska" and "pamięć nieznana" in est.method


def test_cheap_minority_trimmed_from_market():
    assert trim_low([80, 90, 1500, 1550, 1600, 1650, 1700], 0.3) == [1500, 1550, 1600, 1650, 1700]
    est = estimate_market_value("iPhone 13", 128, "used",
                                [_obs(p, i=i) for i, p in enumerate([80, 90, 1500, 1550, 1600, 1650, 1700])],
                                Settings())
    assert est.raw_median >= 1500


# ------------------------------------ 1 + 6. skaner: kraj i sprzedawcy seryjni ---

def _item(i, title, price, user_id):
    return {"id": i, "title": title, "price": {"amount": f"{price:.2f}", "currency_code": "PLN"},
            "url": f"/items/{i}", "user": {"id": user_id, "login": f"u{user_id}", "business": False},
            "photos": [{"url": f"https://images.vinted.net/{i}.jpg"}]}


def _vinted(items, countries, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        host, path = request.url.host, request.url.path
        if host == "www.vinted.pl" and path.startswith("/api/v2/users/"):
            uid = path.rsplit("/", 1)[-1]
            calls.append(uid)
            return httpx.Response(200, json={"user": {"id": int(uid), "login": f"u{uid}",
                                                      "country_code": countries.get(uid, "PL")}})
        if host == "www.vinted.pl":
            return httpx.Response(200, text="", headers={"set-cookie": "access_token_web=tok; Path=/; Domain=.vinted.pl"})
        if host == "api.vinted.pl":
            return httpx.Response(200, json={"items": items, "pagination": {"total_pages": 1}})
        return httpx.Response(404)
    return handler


def _scan(conn, settings, handler):
    limiter = HostRateLimiter(0)
    transport = httpx.MockTransport(handler)
    scanner = Scanner(conn, settings, limiter,
                      http_factory=lambda: HttpClient(limiter, transport=transport, wait=lambda s: 0))
    return asyncio.run(scanner.run())


def _vinted_settings(**kw):
    return Settings(enabled_sources={"vinted": True, "allegro_lokalnie": False, "sprzedajemy": False},
                    max_pages_per_query=1, **kw)


@pytest.fixture
def conn(tmp_path):
    c = open_database(tmp_path / "s.sqlite3")
    PartsRepository(c).seed_defaults_if_empty()
    repo = OfferRepository(c)  # dane rynkowe: iPhone 13 128GB ok. 1500 zł
    for i, p in enumerate([1450, 1500, 1520, 1550, 1600, 1480]):
        raw = RawOffer("allegro_lokalnie", f"m{i}", "https://x", "iPhone 13 128GB", p, photos=["x"])
        repo.upsert(raw, parse_offer(raw))
    yield c
    c.close()


def _rejected(conn):
    return {r.title: r for r in RejectedRepository(conn).list()}


def test_problem1_vinted_poland_only(conn):
    items = [_item(1, "iPhone 13 128GB", 1400, 11), _item(2, "iPhone 12 64GB", 900, 22),
             _item(3, "13 14 15 terakota skal obal", 83, 33), _item(4, "iPhone 11 64GB modrá", 600, 44)]
    calls: list[str] = []
    report = _scan(conn, _vinted_settings(), _vinted(items, {"22": "CZ"}, calls))
    vinted = next(s for s in report.sources if s.key == "vinted")
    assert vinted.saved == 1 and vinted.foreign == 2
    rej = _rejected(conn)
    assert rej["iPhone 12 64GB"].stage == "country" and "CZ" in rej["iPhone 12 64GB"].reason
    assert rej["iPhone 11 64GB modrá"].stage == "country" and "cs/sk" in rej["iPhone 11 64GB modrá"].reason
    assert rej["13 14 15 terakota skal obal"].stage == "multi_model"
    # profil sprawdzony tylko dla ofert, które przeszły filtr tekstu i nie są w obcym języku
    assert sorted(calls) == ["11", "22"]
    assert SellerRepository(conn).get_many("vinted", ["22"])["22"].country_code == "CZ"
    calls.clear()
    _scan(conn, _vinted_settings(), _vinted(items, {"22": "CZ"}, calls))
    assert calls == []  # kraj zapamiętany — bez ponownych zapytań


def test_vinted_ship_to_poland_mode_flags_instead_of_rejecting(conn):
    items = [_item(2, "iPhone 12 64GB", 900, 22), _item(4, "iPhone 11 64GB modrá", 600, 44)]
    _scan(conn, _vinted_settings(vinted_country_mode="ship"), _vinted(items, {"22": "CZ"}, []))
    offers = {o.raw.title: o for o in OfferRepository(conn).list() if o.raw.source == "vinted"}
    assert set(offers) == {"iPhone 12 64GB", "iPhone 11 64GB modrá"}
    assert all(RedFlag.FOREIGN_SELLER in o.parsed.flags for o in offers.values())


def test_seller_lookups_are_limited_per_scan(conn):
    items = [_item(i, f"iPhone 13 128GB nr {i}", 1400 + i, 100 + i) for i in range(5)]
    calls: list[str] = []
    _scan(conn, _vinted_settings(seller_lookups_per_scan=2), _vinted(items, {}, calls))
    assert len(calls) == 2


def test_problem6_serial_seller_hidden_and_can_be_trusted(conn):
    items = [_item(10 + i, "iPhone 13 128GB", p, 777) for i, p in enumerate([80, 83, 83, 95])]
    items.append(_item(20, "iPhone 13 128GB", 1450, 888))  # zwykły sprzedawca
    report = _scan(conn, _vinted_settings(), _vinted(items, {}, []))
    vinted = next(s for s in report.sources if s.key == "vinted")
    assert vinted.serial == 4 and vinted.saved == 1
    serial = SellerRepository(conn).serial_sellers("vinted")
    assert ("vinted", "777") in serial and "4 ofert" in serial[("vinted", "777")].serial_reason
    # kolejne ogłoszenie tego sprzedawcy też jest ukryte, nawet w normalnej cenie
    _scan(conn, _vinted_settings(), _vinted([_item(30, "iPhone 13 128GB", 1500, 777)], {}, []))
    rej = [r for r in RejectedRepository(conn).list() if r.stage == "seller"]
    assert len(rej) == 5

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    widgets.QApplication.instance() or widgets.QApplication([])
    from phonebot.ui.rejected_dialog import RejectedDialog

    dialog = RejectedDialog(RejectedRepository(conn))
    dialog.stage_combo.setCurrentIndex(dialog.stage_combo.findData("seller"))
    dialog.table.selectRow(0)
    assert dialog.seller_ok_btn.isEnabled()
    assert dialog.trust_selected_seller() == 5
    assert ("vinted", "777") not in SellerRepository(conn).serial_sellers("vinted")
    assert sum(1 for o in OfferRepository(conn).list() if o.raw.params.get("seller_id") == "777") == 5
    dialog.close()


# ---------------------------------------- stare oferty w bazie po aktualizacji ---

def test_stored_junk_is_refiltered_once(conn):
    repo = OfferRepository(conn)
    junk = RawOffer("vinted", "j1", "https://x", "13 14 15 terakota skal obal", 83, photos=["x"])
    watched = RawOffer("vinted", "j2", "https://x", "Etui iPhone 12/13/14", 49, photos=["x"])
    repo.upsert(junk, parse_offer(junk))
    wid = repo.upsert(watched, parse_offer(watched)).offer_id
    repo.set_status(wid, OfferStatus.WATCHED)  # obserwowanych nie ruszamy
    guard = OfferGuard(conn, Settings())
    assert guard.refilter_stored() == 1
    assert _rejected(conn)["13 14 15 terakota skal obal"].stage == "multi_model"
    assert OfferGuard(conn, Settings()).refilter_stored() == 0  # reguły bez zmian → bez ponownego przeglądu
    assert repo.get(wid) is not None


def test_flagged_offers_excluded_from_market_data(conn):
    repo = OfferRepository(conn)
    raw = RawOffer("vinted", "cheap", "https://x", "iPhone 13 128GB", 90, photos=["x"])
    parsed = parse_offer(raw)
    parsed.flags.append(RedFlag.PRICE_UNREALISTIC)
    repo.upsert(raw, parsed)
    prices = [o.price for o in repo.market_observations("iPhone 13", 30)]
    assert 90 not in prices and len(prices) == 6


def test_profile_without_country_is_not_rechecked(conn):
    sellers = SellerRepository(conn)
    sellers.save_country("vinted", "5", None)  # profil bez kraju
    sellers.mark_serial("vinted", "6", "test")  # oznaczony, ale kraju nie sprawdzano
    assert sellers.needs_country("vinted", ["5", "6", "7"]) == ["6", "7"]
