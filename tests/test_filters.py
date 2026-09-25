import pytest

from phonebot.core.filters import listing_rejection_reason


@pytest.mark.parametrize("title", [
    "Etui do iPhone 13", "Szkło hartowane iPhone 12", "Wyświetlacz iPhone 11 oryginalny",
    "Bateria iPhone X", "Obudowa iPhone 13 Pro", "Ładowarka do iPhone", "Pudełko iPhone 14",
    "Kupię iPhone uszkodzony", "Skup iPhone Kraków", "Zamienię iPhone 11 na Samsung",
])
def test_rejected(title):
    assert listing_rejection_reason(title)


@pytest.mark.parametrize("title", [
    "iPhone 13 128GB zbity ekran", "Apple iPhone 12 + etui gratis", "iPhone 11 wyświetlacz do wymiany",
    "Sprzedam iPhone 13", "Nowy iPhone 15 zafoliowany",
])
def test_accepted(title):
    assert listing_rejection_reason(title) is None
