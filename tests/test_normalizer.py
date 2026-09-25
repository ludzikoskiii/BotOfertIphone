import pytest

from phonebot.core.models import Condition, Defect
from phonebot.core.normalizer import parse_battery_health, parse_model, parse_negotiable, parse_storage
from phonebot.core.text import normalize

from .conftest import make_offer


@pytest.mark.parametrize(
    "title, model",
    [
        ("Apple iPhone 13 Pro Max 256GB grafitowy", "iPhone 13 Pro Max"),
        ("iphone 11pro 64 gb", "iPhone 11 Pro"),
        ("iPhone 13promax", "iPhone 13 Pro Max"),
        ("IPHONE XS MAX 512", "iPhone XS Max"),
        ("iPhone XR czerwony", "iPhone XR"),
        ("iPhone X 64", "iPhone X"),
        ("iPhone SE 2020 64GB", "iPhone SE (2020)"),
        ("iphone se 3 gen 128gb", "iPhone SE (2022)"),
        ("iPhone SE 2 generacji", "iPhone SE (2020)"),
        ("iPhone SE", "iPhone SE"),
        ("iPhone 8+ 64gb", "iPhone 8 Plus"),
        ("iPhone 7 Plus", "iPhone 7 Plus"),
        ("iPhone 12 mini", "iPhone 12 mini"),
        ("iPhone 11 Max", "iPhone 11 Pro Max"),
        ("iPhone 16e 128", "iPhone 16e"),
        ("iPhone Air 256", "iPhone Air"),
        ("iPhone 17 Pro Max 2TB", "iPhone 17 Pro Max"),
        ("iPhone 18 Pro 256GB", "iPhone 18 Pro"),  # przyszły model spoza katalogu
        ("Ajfon 12 pro", "iPhone 12 Pro"),
        ("iPhone 6s", "iPhone 6s"),
    ],
)
def test_parse_model(title, model):
    assert parse_model(normalize(title)) == model


@pytest.mark.parametrize(
    "title", ["iPhone 12 Plus", "iPhone 9", "Samsung Galaxy S21", "Etui do iPhone", "iPhone 128GB"]
)
def test_parse_model_rejects_unknown(title):
    assert parse_model(normalize(title)) is None


def test_bare_title_only_when_allowed():
    assert parse_model(normalize("13 Pro 128GB")) is None
    assert parse_model(normalize("13 Pro 128GB"), bare=True) == "iPhone 13 Pro"


@pytest.mark.parametrize(
    "text, model, expected",
    [
        ("iphone 13 128gb", "iPhone 13", 128),
        ("iphone 13 pro 1 tb", "iPhone 13 Pro", 1024),
        ("iphone 13 256 gb", "iPhone 13", 256),
        ("iphone 13 64gb", "iPhone 13", None),  # iPhone 13 nie ma 64 GB
        ("iphone 11 5g 4gb ram 128gb", "iPhone 11", 128),
    ],
)
def test_parse_storage(text, model, expected):
    assert parse_storage(normalize(text), model) == expected


def test_parse_storage_bare_number_only_when_allowed():
    assert parse_storage(normalize("iPhone 12 128"), "iPhone 12") is None
    assert parse_storage(normalize("iPhone 12 128"), "iPhone 12", allow_bare=True) == 128


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Bateria 87%", 87),
        ("kondycja baterii: 79 %", 79),
        ("stan baterii 100%", 100),
        ("91% kondycji", 91),
        ("rabat 10%", None),
    ],
)
def test_battery_health(text, expected):
    assert parse_battery_health(normalize(text)) == expected


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Cena do negocjacji", True),
        ("Możliwa negocjacja ceny", True),
        ("Cena ostateczna", False),
        ("Bez negocjacji!", False),
        ("Nie negocjuję", False),
        ("Sprzedam telefon", None),
    ],
)
def test_negotiable(text, expected):
    assert parse_negotiable(normalize(text)) is expected


@pytest.mark.parametrize(
    "description, defect",
    [
        ("Zbity ekran, reszta działa", Defect.SCREEN),
        ("pęknięta szybka, dotyk działa", Defect.SCREEN),
        ("Wyświetlacz jest pęknięty w rogu", Defect.SCREEN),
        ("zielone paski na ekranie", Defect.SCREEN),
        ("dotyk nie działa w dolnej części", Defect.SCREEN),
        ("Zbity tył, przód cały", Defect.BACK_GLASS),
        ("pęknięta tylna szyba", Defect.BACK_GLASS),
        ("plecki popękane", Defect.BACK_GLASS),
        ("zbita szybka aparatu", Defect.CAMERA_LENS),
        ("Bateria 76%", Defect.BATTERY),
        ("słaba bateria, szybko się rozładowuje", Defect.BATTERY),
        ("Komunikat serwis baterii", Defect.BATTERY),
        ("Nie ładuje, port do wymiany", Defect.CHARGING_PORT),
        ("ładuje tylko bezprzewodowo", Defect.CHARGING_PORT),
        ("aparat tylny nie działa", Defect.CAMERA),
        ("Face ID nie działa", Defect.FACE_ID),
        ("brak Face ID", Defect.FACE_ID),
        ("głośnik górny nie działa", Defect.SPEAKER),
        ("nie słychać mnie podczas rozmowy", Defect.MICROPHONE),
        ("przycisk power nie działa", Defect.BUTTONS),
        ("lekkie wgniecenia na ramce", Defect.HOUSING),
        ("telefon się nie włącza", Defect.NO_POWER),
        ("wisi na jabłku", Defect.NO_POWER),
        ("po zalaniu", Defect.WATER_DAMAGE),
    ],
)
def test_detects_defect(description, defect):
    offer = make_offer("iPhone 12 128GB", description=description)
    assert defect in offer.parsed.defects


@pytest.mark.parametrize(
    "description",
    [
        "Ekran nie zbity, wszystko działa",
        "niezbity ekran, bez rys",
        "nie ma zbitego ekranu",
        "Nigdy nie zalany",
        "bez zalania, wszystko sprawne",
        "Face ID działa, brak rys",
        "Bateria 90%, ładuje bez problemu",
        "Wszystko działa, tylna szyba cała",
        "Nowa bateria, nowy ekran",
    ],
)
def test_negations_do_not_create_defects(description):
    offer = make_offer("iPhone 12 128GB", description=description)
    assert offer.parsed.defects == []


def test_zbita_szybka_aparatu_is_not_screen():
    offer = make_offer("iPhone 13", description="zbita szybka aparatu, ekran cały")
    assert Defect.SCREEN not in offer.parsed.defects


def test_back_glass_is_not_screen():
    offer = make_offer("iPhone 13", description="zbita tylna szyba")
    assert offer.parsed.defects == [Defect.BACK_GLASS]


def test_battery_threshold_is_configurable():
    from phonebot.core.models import RawOffer
    from phonebot.core.normalizer import parse_offer

    raw = RawOffer("t", "1", "u", "iPhone 13", 1000, description="bateria 84%")
    assert Defect.BATTERY in parse_offer(raw, battery_threshold=85).defects
    assert Defect.BATTERY not in parse_offer(raw, battery_threshold=80).defects


@pytest.mark.parametrize(
    "title, description, condition",
    [
        ("iPhone 13 128GB", "Sprawny, zwykłe ślady użytkowania", Condition.GOOD),
        ("iPhone 13 128GB", "Stan idealny, bez rys", Condition.LIKE_NEW),
        ("iPhone 15 128GB fabrycznie nowy", "zafoliowany", Condition.NEW),
        ("iPhone 13 128GB", "nowy ekran, nowa bateria", Condition.GOOD),
        ("iPhone 13 128GB", "Zbity ekran", Condition.DAMAGED),
        ("iPhone 13 128GB uszkodzony", "", Condition.DAMAGED),
        ("iPhone 13 128GB na części", "", Condition.FOR_PARTS),
        ("iPhone 13 128GB", "bateria 75%", Condition.GOOD),  # sama bateria nie czyni go uszkodzonym
        ("iPhone 13 128GB", "Telefon nieuszkodzony, sprawny", Condition.GOOD),
    ],
)
def test_condition(title, description, condition):
    assert make_offer(title, description=description).parsed.condition is condition


def test_condition_from_portal_param():
    offer = make_offer("iPhone 13 128GB", params={"condition": "Uszkodzone"})
    assert offer.parsed.condition is Condition.DAMAGED


def test_model_and_storage_from_params():
    offer = make_offer("Telefon Apple okazja", params={"model": "iPhone 14 Pro", "storage": "256 GB"})
    assert offer.parsed.model == "iPhone 14 Pro"
    assert offer.parsed.storage_gb == 256


def test_full_parse():
    offer = make_offer(
        "iPhone 12 Pro 256GB zbity ekran",
        description="Bateria 81%. Face ID działa. Cena do negocjacji.",
    )
    p = offer.parsed
    assert (p.model, p.storage_gb, p.battery_health, p.negotiable) == ("iPhone 12 Pro", 256, 81, True)
    assert p.defects == [Defect.SCREEN]
    assert p.condition is Condition.DAMAGED


def test_negation_does_not_leak_across_conjunction_after_verb():
    offer = make_offer("iPhone 12", description="nie włącza się i zbity ekran")
    assert {Defect.NO_POWER, Defect.SCREEN} <= set(offer.parsed.defects)
