import httpx
import pytest

from phonebot.core.geo import road_distance_km
from phonebot.core.places import PLACES
from phonebot.net.geocode import GeocodeError, geocode

NOMINATIM = [
    {"place_id": 1, "lat": "49.3493", "lon": "20.3017", "name": "Kacwin", "type": "village",
     "display_name": "Kacwin, gmina Łapsze Niżne, powiat nowotarski, województwo małopolskie, Polska"},
    {"place_id": 2, "lat": "bad", "lon": "x", "display_name": "zepsuty"},
]


def test_geocode_parses_results():
    seen = {}

    def handler(request):
        seen["req"] = request
        return httpx.Response(200, json=NOMINATIM)

    (place,) = geocode("Kacwin", transport=httpx.MockTransport(handler))
    assert place.name == "Kacwin" and place.lat == pytest.approx(49.3493)
    assert "powiat nowotarski" in place.description
    assert seen["req"].url.params["countrycodes"] == "pl"
    assert "PhoneBot" in seen["req"].headers["user-agent"]


def test_geocode_error():
    with pytest.raises(GeocodeError):
        geocode("Kacwin", transport=httpx.MockTransport(lambda r: httpx.Response(503)))


def test_empty_query():
    assert geocode("  ") == []


def test_builtin_places_are_in_poland():
    assert PLACES[0].name == "Kacwin"
    for p in PLACES:
        assert 49.0 < p.lat < 54.9 and 14.1 < p.lon < 24.2, p.name


def test_distance_kacwin_nowy_targ_is_reasonable():
    k, nt = PLACES[0], next(p for p in PLACES if p.name == "Nowy Targ")
    assert 25 < road_distance_km(k.lat, k.lon, nt.lat, nt.lon) < 35


def test_find_place_by_city_name():
    from phonebot.core.places import find_place

    assert find_place("Nowy Targ").lat == pytest.approx(49.4775)
    assert find_place("KRAKÓW").name == "Kraków"
    assert find_place("Pcim Dolny") is None
    assert find_place(None) is None
