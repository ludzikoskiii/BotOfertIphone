"""Obliczenia odległości."""
from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0
# Drogi są dłuższe niż odległość w linii prostej — przybliżony współczynnik.
ROAD_FACTOR = 1.3


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(a))


def road_distance_km(lat1: float | None, lon1: float | None, lat2: float | None, lon2: float | None) -> float | None:
    if None in (lat1, lon1, lat2, lon2):
        return None
    return round(haversine_km(lat1, lon1, lat2, lon2) * ROAD_FACTOR, 1)  # type: ignore[arg-type]
