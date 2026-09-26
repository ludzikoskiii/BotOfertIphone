"""Przykłady tytułów: telefony, które MUSZĄ przejść, i ogłoszenia, które MUSZĄ zostać odrzucone."""
import pytest

from phonebot.core.listing_filter import ListingFilter, ListingFilterConfig
from phonebot.core.normalizer import parse_model
from phonebot.core.text import normalize

FILTER = ListingFilter()


def decide(title, category=None):
    model = parse_model(normalize(title), bare=False)
    return FILTER.check(title, model=model, category=category)


PHONES = [
    "iPhone 13 128GB + etui gratis",
    "iPhone 12 z pudełkiem i ładowarką",
    "Apple iPhone 13 Pro 256GB, stan idealny, etui, szkło",
    "iPhone 14 Pro 128GB z etui i szkłem",
    "iPhone 15 128GB w zestawie ładowarka i etui",
    "iPhone 13 mini 128GB komplet pudełko kabel",
    "iPhone SE 2020 64GB + 3 etui",
    "Etui gratis! iPhone 13 128GB",
    "iPhone 13 128GB etui",
    "iPhone 11 64GB zbity wyświetlacz",
    "iPhone 11 wyświetlacz do wymiany, reszta sprawna",
    "iPhone 13 zielony 128GB, pęknięta tylna szyba",
    "iPhone 12 Pro 256GB bateria 85%",
    "iPhone 11 128GB sprawny, nowa bateria",
    "iPhone 12 64GB wymieniona bateria",
    "iPhone 13 128GB, ekran bez rys",
    "iPhone 12 Pro 256GB, aparat 12MP, Face ID działa",
    "iPhone XR na części",
    "iPhone 11 nie włącza się, płyta?",
    "iPhone 11 Pro 64GB uszkodzony aparat",
    "Sprzedam lub zamienię iPhone 13 128GB",
    "Sprzedam iPhone 12 mini, etui w komplecie",
    "iPhone 14 128GB oraz ładowarka MagSafe",
    "iPhone 13 Pro Max 256GB wraz z oryginalnym pudełkiem",
    "iPhone 8 64GB plus kabel i zasilacz",
    "iPhone 12 128GB bateria 79% do wymiany",
    "iPhone X 64GB, słaba bateria",
    "iPhone 11 kompletny, płyta sprawna",
]

REJECTED = [
    ("Etui do iPhone 13", "accessory"),
    ("Szkło hartowane iPhone 12 Pro", "accessory"),
    ("Case iPhone 14 Pro Max silikonowy", "accessory"),
    ("iPhone 13 Pro Max etui silikonowe", "accessory"),
    ("Ładowarka do iPhone 20W", "accessory"),
    ("Kabel lightning iPhone 1m", "accessory"),
    ("Pudełko po iPhone 13 Pro", "accessory"),
    ("Atrapa iPhone 14 Pro", "accessory"),
    ("Obudowa iPhone 12 niebieska", "accessory"),
    ("Pasek do Apple Watch", "accessory"),
    ("Uchwyt samochodowy na iPhone", "accessory"),
    ("Folia ochronna na iPhone 11", "accessory"),
    ("Pokrowiec iPhone 13 mini skórzany", "accessory"),
    ("Sam wyświetlacz iPhone 11 oryginalny", "part"),
    ("Płyta główna iPhone 12 sprawna", "part"),
    ("Wyświetlacz iPhone 11 OLED", "part"),
    ("Bateria do iPhone X", "part"),
    ("iPhone 12 płyta główna", "part"),
    ("Tylna szyba iPhone 13 niebieska", "part"),
    ("Tylko płyta iPhone 11 Pro 64GB", "part"),
    ("Kupię iPhone 13 uszkodzony", "wanted"),
    ("Szukam iPhone 12", "wanted"),
    ("Zamienię iPhone 11 na Samsung", "wanted"),
    ("Skup telefonów iPhone", "wanted"),
    ("Samsung Galaxy S21 128GB", "model"),
    ("Telefon sprawny 128GB", "model"),
]


@pytest.mark.parametrize("title", PHONES)
def test_phone_passes(title):
    d = decide(title)
    assert d.accepted, f"{title!r} odrzucone: {d.stage} — {d.reason}"


@pytest.mark.parametrize("title, stage", REJECTED)
def test_non_phone_rejected(title, stage):
    d = decide(title)
    assert not d.accepted, f"{title!r} przepuszczone"
    assert d.stage == stage, f"{title!r}: etap {d.stage} ({d.reason}), oczekiwano {stage}"
    assert d.reason


def test_reason_names_the_keyword():
    d = decide("Etui do iPhone 13")
    assert d.keyword == "etui" and "etui" in d.reason


@pytest.mark.parametrize("category, accepted", [
    ("Elektronika > Telefony > Smartfony > iPhone", True),
    ("Elektronika > Telefony > Akcesoria GSM > Etui", False),
    ("Telefony i akcesoria > Części zamienne", False),
    ("Telefony i akcesoria", True),  # kategoria mieszana (Allegro Lokalnie, ID 4) — decyduje tytuł
    ("Elektronika > Telefony i akcesoria", True),
    ("Elektronika > Telefony i akcesoria > Akcesoria GSM", False),
    (None, True),
])
def test_category_stage(category, accepted):
    assert decide("iPhone 13 128GB", category=category).accepted is accepted


def test_whitelisted_offer_always_passes():
    f = ListingFilter(whitelist={("vinted", "42")})
    assert f.check("Etui do iPhone 13", model="iPhone 13", source="vinted", source_id="42").accepted
    assert not f.check("Etui do iPhone 13", model="iPhone 13", source="vinted", source_id="43").accepted


def test_keyword_lists_are_configurable():
    cfg = ListingFilterConfig(accessory_words=["skarpetka"])
    f = ListingFilter(cfg)
    assert f.check("Etui do iPhone 13", model="iPhone 13").accepted  # „etui” usunięte z listy
    assert not f.check("Skarpetka na iPhone 13", model="iPhone 13").accepted


# ------------------------------------------------------------ test ceny ---


def test_price_ok():
    assert FILTER.check_price(1200, 2000, "").accepted
    assert not FILTER.check_price(1200, 2000, "").suspicious


def test_unrealistic_price_is_marked_suspicious_not_a_bargain():
    d = FILTER.check_price(150, 2000, "Telefon sprawny, bateria 90%.")
    assert d.accepted and d.suspicious and "8%" in d.reason and "sprawdź" in d.reason


@pytest.mark.parametrize("description", [
    "Sprzedam etui do iPhone 13, stan bardzo dobry.",
    "Sama płyta, reszta nie jest na sprzedaż.",
    "To jest atrapa telefonu do ekspozycji.",
])
def test_unrealistic_price_with_accessory_description_is_rejected(description):
    d = FILTER.check_price(90, 2000, description)
    assert not d.accepted and d.stage == "price"


def test_price_check_without_market_data_is_skipped():
    assert FILTER.check_price(10, None, "").accepted
