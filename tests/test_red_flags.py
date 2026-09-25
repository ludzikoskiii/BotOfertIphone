import pytest

from phonebot.core.models import RedFlag, Severity

from .conftest import make_offer


@pytest.mark.parametrize(
    "description, flag",
    [
        ("Blokada iCloud, na części", RedFlag.ICLOUD_LOCK),
        ("zablokowany na koncie apple", RedFlag.ICLOUD_LOCK),
        ("iCloud locked", RedFlag.ICLOUD_LOCK),
        ("blokada aktywacji", RedFlag.ICLOUD_LOCK),
        ("Nie znam hasła do iCloud", RedFlag.ICLOUD_LOCK),
        ("IMEI zablokowany", RedFlag.IMEI_BLOCKED),
        ("telefon na czarnej liście", RedFlag.IMEI_BLOCKED),
        ("zgłoszony jako kradziony", RedFlag.IMEI_BLOCKED),
        ("profil MDM firmowy", RedFlag.MDM),
        ("replika iPhone", RedFlag.REPLICA),
        ("simlock na Play", RedFlag.SIMLOCK),
        ("brak zasięgu", RedFlag.NO_SIGNAL),
        ("ekran zamiennik", RedFlag.NON_ORIGINAL_PARTS),
        ("nie sprawdzałem, sprzedaję jak jest", RedFlag.UNTESTED),
    ],
)
def test_detects_flag(description, flag):
    assert flag in make_offer("iPhone 12 128GB", description=description).parsed.flags


@pytest.mark.parametrize(
    "description",
    [
        "Bez blokad iCloud, wylogowany",
        "brak blokady icloud",
        "nie ma blokady iCloud",
        "wolny od blokad icloud i simlocka",
        "IMEI czysty, nie jest zablokowany",
        "Oryginał, nie podróbka",
        "bez simlocka",
        "ekran oryginalny, nie zamiennik",
        "Telefon sprawny, wszystko działa",
    ],
)
def test_no_false_flags(description):
    flags = make_offer("iPhone 12 128GB", description=description).parsed.flags
    assert flags == []


def test_no_photos_flag():
    assert RedFlag.NO_PHOTOS in make_offer("iPhone 12", photos=[]).parsed.flags


def test_for_parts_without_explanation():
    offer = make_offer("iPhone 12 na części", description="Sprzedam.")
    assert RedFlag.FOR_PARTS_UNEXPLAINED in offer.parsed.flags


def test_for_parts_with_defect_is_explained():
    offer = make_offer("iPhone 12 na części", description="Zbity ekran i tył.")
    assert RedFlag.FOR_PARTS_UNEXPLAINED not in offer.parsed.flags


def test_severity():
    assert RedFlag.ICLOUD_LOCK.severity is Severity.HARD
    assert RedFlag.NO_PHOTOS.severity is Severity.SOFT
