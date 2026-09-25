"""Wyszukiwanie miejscowości (współrzędnych) w OpenStreetMap Nominatim.

Wywoływane tylko ręcznie z okna „Lokalizacja” (pojedyncze zapytania),
zgodnie z zasadami korzystania z Nominatim (własny User-Agent, niski ruch).
"""
from __future__ import annotations

import httpx

from .. import __version__
from ..core.places import Place

NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = f"PhoneBot/{__version__} (aplikacja desktopowa, pojedyncze wyszukiwania lokalizacji)"


class GeocodeError(Exception):
    pass


def geocode(query: str, *, limit: int = 8, transport: httpx.BaseTransport | None = None) -> list[Place]:
    query = query.strip()
    if not query:
        return []
    params = {"q": query, "countrycodes": "pl", "format": "jsonv2", "limit": limit, "accept-language": "pl"}
    try:
        with httpx.Client(timeout=10, headers={"User-Agent": USER_AGENT}, transport=transport) as client:
            response = client.get(NOMINATIM_URL, params=params)
            response.raise_for_status()
            data = response.json()
    except (httpx.HTTPError, ValueError) as e:
        raise GeocodeError(f"Nie udało się wyszukać miejscowości: {e}") from e
    places = []
    for item in data if isinstance(data, list) else []:
        try:
            display = str(item.get("display_name") or "")
            name = item.get("name") or display.split(",")[0]
            desc = ", ".join(part.strip() for part in display.split(",")[1:4])
            places.append(Place(str(name), float(item["lat"]), float(item["lon"]), desc))
        except (KeyError, TypeError, ValueError):
            continue
    return places
