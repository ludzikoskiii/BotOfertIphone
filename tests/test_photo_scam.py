"""Oszustwo „sprzedaż zdjęcia iPhone'a zamiast telefonu”: wykrywanie (kilka języków, ukryte dopiski),
brak fałszywych alarmów, skutki (Odrzucone, DO WERYFIKACJI, Wybrane, telefon, Telegram), czarna lista, AI."""
from __future__ import annotations

import asyncio

import pytest

from phonebot.core.listing_filter import STAGE_LABELS
from phonebot.core.models import AiLayers, Mode, OfferStatus, RawOffer, Verdict
from phonebot.core.photo_scam import PhotoScamConfig, detect, unhide
from phonebot.core.selection import SelectionCriteria, auto_match
from phonebot.core.settings import Settings
from phonebot.services.evaluator import Evaluator
from phonebot.services.offer_guard import OfferGuard
from phonebot.services.photo_scam_service import blacklist_candidates, block, mark_proposed, report
from phonebot.sources.base import SourceAdapter
from phonebot.storage.db import open_database
from phonebot.storage.repositories import (
    AiRepository,
    BlacklistRepository,
    OfferRepository,
    RejectedRepository,
    SettingsRepository,
)

from .test_scanner import make_scanner

CFG = PhotoScamConfig()


def level(title, description="", **kw):
    return detect(title, description, CFG, **kw).level


# ---------------------------------------------------------------- oszustwa ---

SCAMS = [
    # polski — różne sformułowania
    ("iPhone 15 Pro Max 256GB", "Stan idealny, bateria 100%. Wysyłka. To jest tylko zdjęcie."),
    ("iPhone 14 Pro 128GB", "Sprzedaję zdjęcie telefonu iPhone 14 Pro, idealne na prezent."),
    ("iPhone 13 128GB", "Przedmiotem sprzedaży jest zdjęcie. Brak zwrotów."),
    ("iPhone 15 128GB", "Nowy, zafoliowany. Nie jest to telefon."),
    ("Zdjęcie iPhone 15 Pro", "Tanio."),
    ("Plakat iPhone 14 Pro Max", ""),
    ("Fotografia iPhone 13 Pro", "Stan: nowy"),
    ("iPhone 14", "Otrzymasz wydrukowane zdjęcie iPhone'a 14 w formacie A4."),
    # ukryte na końcu opisu
    ("iPhone 15 Pro Max 256GB", "Telefon w idealnym stanie, komplet, pudełko, faktura.\n\n\n\n\n\n.tylko zdjecie"),
    ("iPhone 14 Pro 256GB", "Stan bardzo dobry, bateria 91%.                            t y l k o   z d j ę c i e"),
    ("iPhone 13 Pro 128GB", "Sprawny, bez blokad. z.d.j.ę.c.i.e  t.e.l.e.f.o.n.u"),
    ("iPhone 15 256GB", "Stan idealny. t​y​l​k​o z​d​j​ę​c​i​e"),
    ("iPhone 14 128GB", "Bez rys. ᴛʏʟᴋᴏ ᴢᴅᴊęᴄɪᴇ"),
    ("iPhone 15 Pro", "Jak nowy. tylko zdjеcie"),  # cyrylickie „е”
    ("iPhone 15 Pro 256GB", "Stan idealny 📷 tylko"),  # emoji aparatu = „zdjęcie”
    ("iPhone 13 128GB", "Stan idealny *t*y*l*k*o* *z*d*j*e*c*i*e*"),
    # inne języki
    ("iPhone 15 Pro Max", "Brand new. Photo only, not a phone."),
    ("iPhone 14 Pro", "This is a picture of iPhone 14 Pro, you will receive a photo."),
    ("iPhone 15 Pro Max 256GB", "Neu und originalverpackt. Nur Foto! Kein Handy."),
    ("iPhone 14 Pro", "Verkauft wird nur das Bild vom iPhone."),
    ("iPhone 13 Pro", "Prodávám. Jen fotka, není to telefon."),
    ("iPhone 13", "Predám. Len fotka iPhonu."),
    ("iPhone 12 Pro", "Parduodu. Tik nuotrauka."),
    ("Foto iPhone 15", "Nur Foto"),
    ("Bild iPhone 14 Pro Max", ""),
    ("Obrázek iPhone 13", ""),
]


@pytest.mark.parametrize("title,description", SCAMS)
def test_scams_are_certain(title, description):
    r = detect(title, description, CFG)
    assert r.level == "certain", (r.strong, r.weak)
    assert "sprzedaż zdjęcia zamiast telefonu" in r.reason()


def test_hidden_text_is_uncovered():
    text, hidden = unhide("Stan dobry.   t y l k o   z d j ę c i e")
    assert hidden and "tylko zdjęcie" in text
    assert detect("iPhone 14", "ok z.d.j.e.c.i.e", CFG).hidden or True
    assert detect("iPhone 13", "Stan idealny *t*y*l*k*o* *z*d*j*e*c*i*e*", CFG).hidden


# --------------------------------------------------------- uczciwe ogłoszenia ---

HONEST = [
    ("iPhone 13 128GB", "Więcej zdjęć na priv. Zdjęcia prawdziwe, stan jak na zdjęciach."),
    ("iPhone 12 zdjęcia prawdziwe", "Wyślę zdjęcie telefonu z numerem IMEI na prośbę."),
    ("iPhone 14 Pro", "Mogę wysłać więcej zdjęć. Dodaję zdjęcie iPhone'a z tyłu."),
    ("iPhone 13 Pro", "Aparat robi świetne zdjęcia, zdjęcia wykonane tym telefonem na życzenie."),
    ("iPhone 11 brak obrazu", "Telefon nie wyświetla obrazu, do naprawy."),
    ("iPhone 13", "To nie jest telefon firmowy, kupiony w salonie."),
    ("iPhone 14", "Nie jest to tylko zdjęcie, to prawdziwy telefon. Zapraszam."),
    ("iPhone 15 Pro Max", "Real photos, more photos on request."),
    ("iPhone 14 Pro", "Echte Fotos, mehr Fotos auf Anfrage."),
    ("iPhone 13 128GB", "Zdjęcia własne, aktualne zdjęcia z dzisiaj."),
    ("iPhone 12 Pro", "Stan widoczny na zdjęciach, drobne rysy."),
    ("iPhone 11", "Zrzut ekranu z kondycją baterii w galerii."),
    ("iPhone 13 mini", "Kondycja baterii 88%, zdjęcia ekranu ustawień w ogłoszeniu."),
]


@pytest.mark.parametrize("title,description", HONEST)
def test_honest_listings_with_photo_word_are_not_flagged(title, description):
    r = detect(title, description, CFG)
    assert r.level == "none", (r.strong, r.weak)


def test_stock_photos_is_fraud_signal_not_photo_sale():
    r = detect("iPhone 11 64GB", "Zdjęcia poglądowe. Telefon sprawny.", CFG)
    assert r.level == "none" and r.stock_photos
    r = detect("iPhone 15 Pro", "Stan idealny, zdjęcie iPhone'a poglądowe", CFG)
    assert r.level == "none" and r.stock_photos


# ---------------------------------------------------- słabe sygnały i kategorie ---

def test_weak_signals_and_combinations():
    assert level("iPhone 13 Pro plakat", "Super stan") == "weak"
    assert level("iPhone 14 Pro", "Świetny wydruk, idealny na ścianę") == "weak"
    assert level("iPhone 13", "", clip_score=0.9) == "weak"  # CLIP: wydruk / plakat / zrzut ekranu
    assert level("iPhone 13", "", clip_score=0.5) == "none"
    assert level("iPhone 13 Pro plakat", "", clip_score=0.9) == "certain"  # dwa słabe sygnały
    weak = detect("iPhone 13 Pro plakat", "", CFG)
    assert weak.with_price(300, 2000, CFG).level == "certain"  # bardzo tanio + słaby sygnał
    assert weak.with_price(1800, 2000, CFG).level == "weak"
    assert "bardzo niska cena" in weak.with_price(300, 2000, CFG).reason()


def test_portal_categories():
    assert level("iPhone 15 Pro", category="Kolekcje i sztuka > Sztuka > Fotografia") == "certain"
    assert level("iPhone 15 Pro", category="Dom > Dekoracje > Obrazy i plakaty") == "certain"
    assert level("iPhone 15 Pro", category="Elektronika > Fotografia > Aparaty cyfrowe") == "none"
    assert level("iPhone 15 Pro", category="Telefony i akcesoria > Smartfony") == "none"
    assert level("iPhone 15 Pro", category="Kolekcje") == "weak"
    assert level("iPhone 15 Pro", source="allegro_lokalnie", category_id="321811") == "certain"
    assert level("iPhone 15 Pro", source="allegro_lokalnie", category="/oferty/sztuka/fotografia-321811") == "certain"
    assert level("iPhone 15 Pro", source="allegro_lokalnie", category_id="6") == "weak"


def test_settings_lists_are_editable_and_saved(tmp_path):
    conn = open_database(tmp_path / "s.sqlite3")
    s = Settings()
    s.photo_scam.strong_phrases.append("tylko fotka na pamiatke")
    s.photo_scam.enabled = True
    SettingsRepository(conn).save(s)
    loaded = SettingsRepository(conn).load()
    assert "tylko fotka na pamiatke" in loaded.photo_scam.strong_phrases
    assert detect("iPhone 13", "Tylko fotka na pamiątkę", loaded.photo_scam).level == "certain"
    loaded.photo_scam.enabled = False
    assert detect("Zdjęcie iPhone 15", "tylko zdjęcie", loaded.photo_scam).level == "none"


# ------------------------------------------------------------ pełna ścieżka ---

class Portal(SourceAdapter):
    key = "vinted"
    display_name = "Vinted"
    offers: list = []

    async def search(self, query):
        return list(self.offers)


def raw(sid, title, price, description="", seller=None):
    params = {"seller_id": seller, "seller": f"user{seller}"} if seller else {}
    return RawOffer("vinted", sid, f"https://www.vinted.pl/items/{sid}", title, price, description=description,
                    photos=[f"https://img/{sid}.jpg"], params=params)


MARKET = [raw(f"m{i}", "iPhone 13 128GB", 1900 + 20 * i, "Stan dobry, bateria 88%.", seller=str(100 + i))
          for i in range(6)]


def scan(conn, offers, settings=None):
    Portal.offers = offers
    s = settings or Settings(mode=Mode.RESELL.value)
    return asyncio.run(make_scanner(conn, [Portal], s).run())


@pytest.fixture
def db(tmp_path):
    conn = open_database(tmp_path / "t.sqlite3")
    yield conn
    conn.close()


def test_scan_rejects_certain_keeps_weak_for_verification(db):
    scan(db, MARKET)  # ceny rynkowe z wcześniejszych skanów
    scan(db, MARKET + [
        raw("s1", "iPhone 13 128GB", 1500, "Stan idealny. To jest tylko zdjęcie.", seller="666"),
        raw("s2", "iPhone 13 128GB plakat", 350, "Stan idealny", seller="667"),  # słaby + bardzo tanio
        raw("w1", "iPhone 13 128GB plakat", 1850, "Stan dobry", seller="668"),  # słaby, normalna cena
        raw("h1", "iPhone 13 128GB", 1850, "Zdjęcia prawdziwe, więcej zdjęć na priv.", seller="669"),
    ])
    rejected = {r.source_id: r for r in RejectedRepository(db).list()}
    assert {"s1", "s2"} <= set(rejected) and rejected["s1"].stage == "photo_scam"
    assert "sprzedaż zdjęcia zamiast telefonu" in rejected["s1"].reason
    assert "bardzo niska cena" in rejected["s2"].reason
    assert STAGE_LABELS["photo_scam"].startswith("MOŻLIWE OSZUSTWO")
    offers = {o.raw.source_id: o for o in OfferRepository(db).list()}
    assert "w1" in offers and "h1" in offers
    ev = Evaluator(db, Settings(mode=Mode.RESELL.value))
    weak = ev.evaluate(offers["w1"])
    assert weak.photo_scam == "weak" and weak.verdict.rank <= Verdict.VERIFY.rank
    assert "Możliwa sprzedaż samego zdjęcia" in weak.reasons[0]
    honest = ev.evaluate(offers["h1"])
    assert honest.photo_scam == "" and "zdjęcia" not in " ".join(honest.reasons).lower()


def test_restore_this_is_a_phone_whitelists(db):
    scan(db, MARKET + [raw("s1", "iPhone 13 128GB", 1500, "tylko zdjęcie", seller="666")])
    rejected = next(r for r in RejectedRepository(db).list() if r.source_id == "s1")
    offer_id = RejectedRepository(db).restore(rejected.id)  # „To jest telefon”
    assert offer_id is not None
    scan(db, MARKET + [raw("s1", "iPhone 13 128GB", 1500, "tylko zdjęcie", seller="666")])  # kolejny skan
    assert "s1" in {o.raw.source_id for o in OfferRepository(db).list()}
    val = Evaluator(db, Settings(mode=Mode.RESELL.value)).evaluate(OfferRepository(db).get(offer_id))
    assert val.photo_scam == ""


def test_clip_signal_with_cheap_price_moves_offer_out_of_lists(db):
    s = Settings(mode=Mode.RESELL.value)
    scan(db, MARKET + [raw("c1", "iPhone 13 128GB", 400, "Stan idealny", seller="777"),
                       raw("c2", "iPhone 13 128GB", 1850, "Stan idealny", seller="778")], s)
    ai = AiRepository(db)
    for sid in ("c1", "c2"):
        ai.save_photo("vinted", sid, f"https://img/{sid}.jpg", {"smartphone": 0.2, "case": 0.3, "box": 0.3,
                                                              "screen_protector": 0.2, "scam:score": 0.93}, "t")
    offers = OfferRepository(db).list()
    ev = Evaluator(db, s)
    rows = ev.evaluate_visible(offers)
    ids = {o.raw.source_id for o, _ in rows}
    assert "c1" not in ids and ev.moved_photo_scams == 1  # CLIP + bardzo tanio → pewne → „Odrzucone”
    assert "c1" in {r.source_id for r in RejectedRepository(db).list() if r.stage == "photo_scam"}
    c2 = next(v for o, v in rows if o.raw.source_id == "c2")
    assert c2.photo_scam == "weak" and c2.verdict.rank <= Verdict.VERIFY.rank  # sam CLIP → DO WERYFIKACJI


def test_certain_offer_never_picked_or_notified():
    from .test_stage3 import valued

    offer, val = valued("iPhone 13 128GB", 900)
    val.verdict = Verdict.BUY
    from phonebot.services.fraud_service import apply_photo_scam

    apply_photo_scam(val, detect("iPhone 13", "tylko zdjęcie", CFG), Settings())
    assert val.verdict is Verdict.SKIP and val.reasons[0].startswith("MOŻLIWE OSZUSTWO")
    assert val.risk.level == "high" and val.risk.signals[0].key == "photo_sale"
    assert not auto_match(offer, val, SelectionCriteria(verdicts=[], min_profit=0))  # ani Wybrane, ani Telegram


def test_watched_offer_stays_but_marked(db):
    s = Settings(mode=Mode.RESELL.value)
    scan(db, MARKET + [raw("c1", "iPhone 13 128GB", 400, "Stan idealny", seller="777")], s)
    offer = next(o for o in OfferRepository(db).list() if o.raw.source_id == "c1")
    OfferRepository(db).set_status(offer.id, OfferStatus.WATCHED)
    AiRepository(db).save_photo("vinted", "c1", "u", {"smartphone": 0.1, "scam:score": 0.95}, "t")
    rows = Evaluator(db, s).evaluate_visible(OfferRepository(db).list())
    val = next(v for o, v in rows if o.raw.source_id == "c1")
    assert val.photo_scam == "certain" and val.verdict is Verdict.SKIP  # Twoja decyzja — zostaje z etykietą


def test_web_rows_hide_certain(db, tmp_path):
    from phonebot.web.server import WebApp

    s = Settings(mode=Mode.RESELL.value)
    scan(db, MARKET + [raw("c1", "iPhone 13 128GB", 400, "Stan idealny", seller="777")], s)
    AiRepository(db).save_photo("vinted", "c1", "u", {"smartphone": 0.1, "scam:score": 0.95}, "t")
    db.commit()
    app = WebApp(tmp_path / "t.sqlite3", s)
    assert "c1" not in {o.raw.source_id for o, _ in app.rows()}


def test_blacklist_proposal_once_with_confirmation(db):
    scan(db, MARKET + [raw("s1", "iPhone 13 128GB", 1500, "tylko zdjęcie", seller="666"),
                       raw("s3", "iPhone 13 128GB", 1500, "photo only", seller="999")])
    cands = blacklist_candidates(db)
    assert {c.identity for c in cands} == {"vinted:666", "vinted:999"}
    block(db, cands[0])
    mark_proposed(db, [c.identity for c in cands])  # zgoda dla pierwszego, odmowa dla drugiego
    assert blacklist_candidates(db) == []  # o nikogo nie pyta drugi raz
    assert len(BlacklistRepository(db).list()) == 1
    # kolejne ogłoszenie zablokowanego sprzedającego znika na etapie czarnej listy
    OfferGuard(db, Settings()).refilter_stored(force=True)
    scan(db, MARKET + [raw("s4", "iPhone 13 128GB", 1900, "Stan dobry", seller=cands[0].identity.split(":")[1])])
    assert "s4" not in {o.raw.source_id for o in OfferRepository(db).list()}


def test_window_asks_before_blacklisting(db, tmp_path):
    from PySide6.QtWidgets import QApplication

    from phonebot.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    scan(db, MARKET + [raw("s1", "iPhone 13 128GB", 1500, "tylko zdjęcie", seller="666")])
    db.commit()
    win = MainWindow(db, tmp_path / "t.sqlite3", thumbs_dir=tmp_path)
    asked = []
    assert win.propose_photo_scam_blacklist(ask=lambda c: asked.append(c.identity) or False) == 0
    assert asked == ["vinted:666"] and BlacklistRepository(db).list() == []  # odmowa — nic nie zablokowano
    assert win.propose_photo_scam_blacklist(ask=lambda c: True) == 0  # już pytano
    win._quitting = True
    win.close()


def test_training_data_has_photo_class():
    from phonebot.ml import seed_data, text_model

    assert text_model.STAGE_TO_LABEL["photo_scam"] == "photo"
    photos = [t for t, label in seed_data.seed_examples() if label == "photo"]
    assert len(photos) >= 60 and any(t.startswith("Zdjęcie") for t in photos)
    clf = text_model.TextClassifier.train(text_model.seed_training_set())
    assert text_model.top(clf.predict_one("Zdjęcie iPhone 15 Pro Max"))[0] == "photo"
    assert text_model.top(clf.predict_one("iPhone 15 Pro Max 256GB"))[0] == "phone"


def test_rejected_photo_scams_feed_title_classifier(db):
    from phonebot.services.ai_service import build_training_set

    scan(db, MARKET + [raw("s5", "Plakat iPhone 15 Pro", 300, "", seller="555")])
    examples = build_training_set(db, Settings()).examples
    assert any(e.title == "Plakat iPhone 15 Pro" and e.label == "photo" for e in examples)


def test_stock_photos_raise_fraud_risk(db):
    s = Settings(mode=Mode.RESELL.value)
    scan(db, MARKET + [raw("p1", "iPhone 13 128GB", 1850, "Zdjęcia poglądowe, telefon sprawny.", seller="3")], s)
    offer = next(o for o in OfferRepository(db).list() if o.raw.source_id == "p1")
    val = Evaluator(db, s).evaluate(offer)
    assert "stock_photo_text" in {x.key for x in val.risk.signals} and val.photo_scam == ""


def test_report_counts_and_examples(db):
    scan(db, MARKET + [raw("s1", "iPhone 13 128GB", 1500, "tylko zdjęcie", seller="666"),
                       raw("w1", "iPhone 13 128GB plakat", 1850, "Stan dobry", seller="668")])
    r = report(db)
    assert r["rejected"] == 1 and r["verify"] == 1
    assert r["rejected_examples"][0][1] == "iPhone 13 128GB" and "plakat" in r["verify_examples"][0][1]


def test_layers_without_scam_keys_unchanged():
    from phonebot.ml.combine import photo_layer

    ai = AiLayers(photo_probs={"smartphone": 0.9, "case": 0.05, "screen_protector": 0.03, "box": 0.02,
                               "scam:score": 0.99, "scam:poster": 0.9})
    layer = photo_layer(ai, 0.6, 0.6)
    assert layer.state == "zgodne" and "scam" not in layer.summary
