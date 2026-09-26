"""Wbudowana lista miejscowości (szybki wybór bez internetu).

Dowolną inną miejscowość znajdziesz wyszukiwarką w oknie „Lokalizacja”
(OpenStreetMap) albo wpiszesz jej współrzędne ręcznie.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Place:
    name: str
    lat: float
    lon: float
    description: str = ""


PLACES: tuple[Place, ...] = (
    # Podhale i okolice
    Place("Kacwin", 49.3494, 20.3019, "gm. Łapsze Niżne, małopolskie"),
    Place("Łapsze Niżne", 49.3997, 20.2466, "małopolskie"),
    Place("Niedzica", 49.4167, 20.3167, "małopolskie"),
    Place("Bukowina Tatrzańska", 49.3436, 20.1111, "małopolskie"),
    Place("Szczawnica", 49.4264, 20.4870, "małopolskie"),
    Place("Nowy Targ", 49.4775, 20.0327, "małopolskie"),
    Place("Zakopane", 49.2992, 19.9496, "małopolskie"),
    Place("Czarny Dunajec", 49.4394, 19.8511, "małopolskie"),
    Place("Jabłonka", 49.4833, 19.6931, "małopolskie"),
    Place("Rabka-Zdrój", 49.6089, 19.9667, "małopolskie"),
    Place("Limanowa", 49.7059, 20.4213, "małopolskie"),
    Place("Nowy Sącz", 49.6218, 20.6970, "małopolskie"),
    Place("Myślenice", 49.8336, 19.9386, "małopolskie"),
    Place("Gorlice", 49.6553, 21.1596, "małopolskie"),
    Place("Tarnów", 50.0121, 20.9858, "małopolskie"),
    Place("Kraków", 50.0647, 19.9450, "małopolskie"),
    # większe miasta
    Place("Białystok", 53.1325, 23.1688, "podlaskie"),
    Place("Bielsko-Biała", 49.8224, 19.0584, "śląskie"),
    Place("Bydgoszcz", 53.1235, 18.0084, "kujawsko-pomorskie"),
    Place("Częstochowa", 50.8118, 19.1203, "śląskie"),
    Place("Gdańsk", 54.3520, 18.6466, "pomorskie"),
    Place("Gliwice", 50.2945, 18.6714, "śląskie"),
    Place("Gorzów Wielkopolski", 52.7368, 15.2288, "lubuskie"),
    Place("Katowice", 50.2649, 19.0238, "śląskie"),
    Place("Kielce", 50.8661, 20.6286, "świętokrzyskie"),
    Place("Lublin", 51.2465, 22.5684, "lubelskie"),
    Place("Łódź", 51.7592, 19.4560, "łódzkie"),
    Place("Olsztyn", 53.7784, 20.4801, "warmińsko-mazurskie"),
    Place("Opole", 50.6751, 17.9213, "opolskie"),
    Place("Poznań", 52.4064, 16.9252, "wielkopolskie"),
    Place("Radom", 51.4027, 21.1471, "mazowieckie"),
    Place("Rzeszów", 50.0412, 21.9991, "podkarpackie"),
    Place("Szczecin", 53.4285, 14.5528, "zachodniopomorskie"),
    Place("Toruń", 53.0138, 18.5984, "kujawsko-pomorskie"),
    Place("Warszawa", 52.2297, 21.0122, "mazowieckie"),
    Place("Wrocław", 51.1079, 17.0385, "dolnośląskie"),
    Place("Zielona Góra", 51.9356, 15.5062, "lubuskie"),
)


def _key(name: str) -> str:
    from .text import normalize

    return normalize(name).replace("|", "").strip()


_BY_NAME = {_key(p.name): p for p in PLACES}


@lru_cache(maxsize=4096)  # wołane dla każdej oferty przy każdym przeliczeniu tabeli
def find_place(city: str | None) -> Place | None:
    """Współrzędne miejscowości z wbudowanej listy (dla ofert bez współrzędnych)."""
    return _BY_NAME.get(_key(city)) if city else None
