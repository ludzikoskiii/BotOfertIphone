"""Oferty z zagranicy: kraj oferty, filtr „Pochodzenie”, kolumna „Kraj”, profile powiadomień, telefon, Telegram."""
from __future__ import annotations

import json
import re

from phonebot.core.models import RedFlag, Verdict
from phonebot.core.notify_profiles import NotifyProfile, describe, reject_reason
from phonebot.core.origin import is_foreign, offer_origin
from phonebot.core.settings import Settings
from phonebot.core.sorting import level, sort_rows
from phonebot.core.view_filter import ViewFilter, matches
from phonebot.services.telegram_queue import format_offer
from phonebot.storage.repositories import OfferRepository, SellerRepository
from phonebot.web import pages

from .conftest import make_offer
from .test_sorting import _val


def _polish():
    return make_offer("iPhone 13 128GB", 1500, source="allegro_lokalnie", city="Nowy Targ")


def _ebay(country: str | None, market: str = "EBAY_DE"):
    params = {"market": market, "remote_purchase": "1", "original_price": "300.00", "original_currency": "EUR"}
    if country is not None:
        params["item_country"] = country
    return make_offer("iPhone 13 128GB", 1300, source="ebay", params=params)


def _vinted(title: str = "iPhone 13 128GB", *, seller_country: str | None = None, flagged: bool = False):
    o = make_offer(title, 1400, source="vinted", params={"seller_id": "77"})
    o.seller_country = seller_country
    if flagged:
        o.parsed.flags.append(RedFlag.FOREIGN_SELLER)
    return o


def test_origin_of_offers_from_each_kind_of_source():
    pl = offer_origin(_polish())
    assert (pl.foreign, pl.country, pl.label) == (False, "PL", "Polska")

    de = offer_origin(_ebay("DE"))
    assert de.foreign and de.country == "DE" and de.label == "🌍 Niemcy"
    assert "lokalizacja przedmiotu" in de.describe()
    assert not offer_origin(_ebay("PL", "EBAY_PL")).foreign
    gb = offer_origin(_ebay(None, "EBAY_GB"))  # rynek zagraniczny bez lokalizacji przedmiotu
    assert gb.foreign and gb.country is None and gb.label == "🌍 zagranica"

    cz = offer_origin(_vinted(seller_country="CZ", flagged=True))
    assert cz.label == "🌍 Czechy" and "profilu sprzedawcy" in cz.describe()
    assert not offer_origin(_vinted(seller_country="PL")).foreign
    sk = offer_origin(_vinted("Predám iPhone 13 128GB", flagged=True))  # profil nieznany, tytuł po słowacku
    assert sk.foreign and sk.country is None and sk.language == "cs/sk"
    assert sk.label == "🌍 zagranica (czeski / słowacki)" and "język tytułu" in sk.describe()
    flag_only = offer_origin(_vinted(flagged=True))
    assert flag_only.foreign and flag_only.basis == "flag"
    assert not offer_origin(_vinted()).foreign  # Vinted bez flagi i bez kraju z profilu — jak dotąd: z Polski
    assert offer_origin(_vinted(seller_country="XK")).label == "🌍 XK"  # kraj spoza słownika — sam kod


def test_quick_check_matches_full_origin():
    offers = [_polish(), _ebay("DE"), _ebay("PL", "EBAY_PL"), _ebay(None, "EBAY_US"), _vinted(seller_country="LT"),
              _vinted(seller_country="PL"), _vinted("Predám iPhone 13", flagged=True), _vinted()]
    expected = [False, True, False, True, True, False, True, False]
    assert [is_foreign(o) for o in offers] == expected
    assert [offer_origin(o).foreign for o in offers] == expected


def test_view_filter_origin():
    rows = [(o, _val(Verdict.BUY, 300)) for o in (_polish(), _ebay("DE"), _vinted(seller_country="CZ"))]

    def shown(origins):
        return [o.raw.source for o, v in rows if matches(o, v, ViewFilter(origins=origins))]

    assert shown([]) == ["allegro_lokalnie", "ebay", "vinted"]
    assert shown(["pl"]) == ["allegro_lokalnie"]
    assert shown(["foreign"]) == ["ebay", "vinted"]
    assert shown(["pl", "foreign"]) == ["allegro_lokalnie", "ebay", "vinted"]
    assert ViewFilter(origins=["foreign"]).is_active() and not ViewFilter().is_active()


def test_saved_view_filter_without_origins_loads():
    data = json.loads(Settings().to_json())
    del data["view_filter"]["origins"]  # ustawienia zapisane przez starszą wersję
    s = Settings.from_json(json.dumps(data))
    assert s.view_filter.origins == []
    s.view_filter.origins = ["foreign"]
    assert Settings.from_json(s.to_json()).view_filter.origins == ["foreign"]


def test_seller_country_comes_from_profile_in_database(conn):
    repo = OfferRepository(conn)
    o = _vinted()
    repo.upsert(o.raw, o.parsed)
    first = repo.list()[0]
    assert first.seller_country is None and not is_foreign(first)
    SellerRepository(conn).save_country("vinted", "77", "sk")  # profil sprawdzony przy kolejnym odświeżeniu
    loaded = repo.list()[0]
    assert loaded.seller_country == "SK" and offer_origin(loaded).label == "🌍 Słowacja"
    assert repo.get(loaded.id).seller_country == "SK"


def test_sort_by_country_poland_first_then_countries_alphabetically():
    rows = [(o, _val()) for o in (_vinted(seller_country="SK"), _ebay(None, "EBAY_GB"), _polish(), _ebay("DE"),
                                  _vinted(seller_country="CZ"))]
    sort_rows(rows, [level("country", "asc")])
    assert [offer_origin(o).label for o, _ in rows] == ["Polska", "🌍 Czechy", "🌍 Niemcy", "🌍 Słowacja",
                                                        "🌍 zagranica"]
    sort_rows(rows, [level("country", "desc")])
    assert offer_origin(rows[-1][0]).label == "Polska"


def test_notify_profile_country_filter():
    buy = _val(Verdict.BUY, 300)
    p = NotifyProfile(verdicts=[], country="foreign", max_risk="high", skip_hard_flags=False)
    assert "tylko zagranica" in describe(p)
    assert reject_reason(_polish(), buy, p) == "z Polski"
    assert reject_reason(_ebay("DE"), buy, p) is None
    assert reject_reason(_vinted(seller_country="CZ"), buy, p) is None
    p.country = "pl"
    assert reject_reason(_ebay("DE"), buy, p) == "z zagranicy" and reject_reason(_polish(), buy, p) is None
    p.country = "all"
    assert reject_reason(_ebay("DE"), buy, p) is None and reject_reason(_polish(), buy, p) is None


def test_country_in_telegram_message_and_phone_card():
    s = Settings()
    val = _val(Verdict.BUY, 300)
    assert "🌍 Czechy" in format_offer(_vinted(seller_country="CZ"), val, s)
    assert "🌍" not in format_offer(_polish(), val, s)
    cz = _vinted(seller_country="CZ")
    cz.id = 5
    assert "🌍 Czechy" in pages.card(cz, val)
    pl = _polish()
    pl.id = 6
    assert "🌍" not in pages.card(pl, val)


def test_phone_list_has_origin_filter(tmp_path):
    from phonebot.web.server import WebServer

    from .sample_data import build_sample_db
    from .test_web import _client, _login

    conn, _ = build_sample_db(tmp_path / "w.sqlite3")
    vinted = [o for o in OfferRepository(conn).list() if o.raw.source == "vinted"]
    assert len(vinted) >= 2
    seller = vinted[0].raw.params["seller_id"]
    conn.execute("UPDATE sellers SET country_code = 'CZ' WHERE source = 'vinted' AND seller_id = ?", (seller,))
    conn.commit()
    from phonebot.web.auth import hash_pin

    settings = Settings(web_enabled=True, web_port=0, web_pin_hash=hash_pin("2468"))
    srv = WebServer(tmp_path / "w.sqlite3", settings)
    srv.start(settings)
    try:
        with _client(srv) as c:
            _login(c)
            everything = c.get("/")
            assert 'name="kraj"' in everything.text and "Pochodzenie" in everything.text
            foreign = c.get("/", params={"kraj": "zagranica"})
            cards = re.findall(r'<a class="card[^"]*"', foreign.text)
            assert cards and foreign.text.count("🌍 Czechy") == len(cards)
            polish = c.get("/", params={"kraj": "polska"})
            assert "🌍" not in polish.text.split("</details>", 1)[1]
            assert len(re.findall(r'<a class="card', polish.text)) + len(cards) == \
                len(re.findall(r'<a class="card', everything.text))
    finally:
        srv.stop()
        conn.close()


def test_filters_panel_and_country_column(tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])
    from phonebot.ui.filters_panel import FiltersPanel
    from phonebot.ui.images import ThumbnailCache
    from phonebot.ui.table_model import Col, OffersTableModel

    panel = FiltersPanel(ViewFilter(origins=["foreign"]), "Kacwin")
    got = []
    panel.changed.connect(got.append)
    assert panel.current().origins == ["foreign"]
    panel.origin_checks["pl"].setChecked(True)
    panel.origin_checks["foreign"].setChecked(False)
    assert panel.current().origins == ["pl"]
    panel.set_filter(ViewFilter())
    assert panel.current().origins == [] and got[-1].origins == []
    assert "Zabezpieczenia" in panel.origin_checks["foreign"].toolTip()

    model = OffersTableModel(ThumbnailCache(tmp_path))
    model.set_rows([(_polish(), _val()), (_vinted("Predám iPhone 13", flagged=True), _val()),
                    (_ebay("DE"), _val())])
    model.set_sort_spec([level("country", "asc")])
    texts = [model.data(model.index(r, Col.COUNTRY)) for r in range(3)]
    assert texts == ["Polska", "🌍 Niemcy", "🌍 zagranica (czeski / słowacki)"]
    tip = model.data(model.index(1, Col.COUNTRY), Qt.ItemDataRole.ToolTipRole)
    assert "Niemcy (DE)" in tip and model.headerData(Col.COUNTRY, Qt.Orientation.Horizontal) == "Kraj"


def test_message_to_foreign_seller_asks_for_shipping_to_poland():
    from phonebot.core.messages import pickup_line, questions, shipping_line

    val = _val(Verdict.NEGOTIATE, 300)
    cz = _vinted(seller_country="CZ")  # kraj z profilu, jeszcze bez flagi (oferta zapisana przed sprawdzeniem)
    assert "Czy wysyła Pan/Pani do Polski i ile kosztuje wysyłka?" in questions(cz, val)
    assert "paczkomat" not in shipping_line(cz) + pickup_line(cz, 0) and "do Polski" in shipping_line(cz)
    pl = _polish()
    assert "paczkomat" in shipping_line(pl) and not any("do Polski" in q for q in questions(pl, val))
