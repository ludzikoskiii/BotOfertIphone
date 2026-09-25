"""Tabela cen części i usług (edytowalna w aplikacji).

Wartości domyślne to ORIENTACYJNE ceny dobrych zamienników w PLN przy
samodzielnej naprawie. Po pierwszym uruchomieniu trafiają do bazy, gdzie
możesz je dowolnie poprawiać. Wiersz z modelem ``*`` to cena domyślna dla
modeli, których nie ma w tabeli.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import Defect

ANY_MODEL = "*"


@dataclass
class PartPrice:
    model: str
    part: Defect
    price: float
    note: str = ""
    id: int | None = None


class PartsCatalog:
    def __init__(self, rows: list[PartPrice]):
        self._rows = {(r.model, r.part): r for r in rows}

    def lookup(self, model: str | None, part: Defect) -> PartPrice | None:
        if model and (model, part) in self._rows:
            return self._rows[(model, part)]
        return self._rows.get((ANY_MODEL, part))


# kolumny: ekran, bateria, port, tylna szyba, aparat, obudowa
_P = tuple[int, int, int, int, int, int]
_BASE: dict[str, _P] = {
    "iPhone 6s": (70, 45, 30, 0, 50, 70),
    "iPhone 6s Plus": (85, 50, 30, 0, 55, 80),
    "iPhone SE (2016)": (60, 40, 30, 0, 45, 70),
    "iPhone 7": (80, 50, 30, 0, 60, 80),
    "iPhone 7 Plus": (100, 60, 35, 0, 70, 90),
    "iPhone 8": (90, 60, 35, 40, 70, 90),
    "iPhone 8 Plus": (110, 65, 40, 45, 80, 100),
    "iPhone SE (2020)": (90, 60, 35, 40, 70, 90),
    "iPhone SE (2022)": (95, 65, 40, 40, 80, 100),
    "iPhone X": (250, 70, 40, 50, 120, 120),
    "iPhone XR": (130, 70, 45, 50, 100, 120),
    "iPhone XS": (260, 75, 45, 50, 130, 130),
    "iPhone XS Max": (320, 80, 50, 55, 150, 140),
    "iPhone 11": (130, 80, 50, 50, 150, 150),
    "iPhone 11 Pro": (250, 85, 55, 55, 200, 160),
    "iPhone 11 Pro Max": (300, 90, 55, 60, 220, 170),
    "iPhone 12 mini": (250, 85, 60, 60, 180, 170),
    "iPhone 12": (280, 90, 60, 60, 180, 180),
    "iPhone 12 Pro": (280, 90, 60, 60, 250, 190),
    "iPhone 12 Pro Max": (350, 95, 65, 65, 300, 200),
    "iPhone 13 mini": (280, 95, 70, 65, 200, 190),
    "iPhone 13": (300, 100, 70, 70, 200, 200),
    "iPhone 13 Pro": (450, 105, 75, 70, 300, 220),
    "iPhone 13 Pro Max": (500, 110, 75, 75, 350, 230),
    "iPhone 14": (320, 110, 80, 70, 250, 220),
    "iPhone 14 Plus": (380, 115, 80, 75, 250, 230),
    "iPhone 14 Pro": (550, 120, 85, 75, 400, 250),
    "iPhone 14 Pro Max": (650, 125, 85, 80, 450, 260),
    "iPhone 15": (400, 130, 100, 80, 300, 250),
    "iPhone 15 Plus": (450, 135, 100, 85, 300, 260),
    "iPhone 15 Pro": (650, 140, 110, 85, 450, 300),
    "iPhone 15 Pro Max": (750, 145, 110, 90, 500, 320),
    "iPhone 16": (480, 150, 120, 90, 350, 300),
    "iPhone 16 Plus": (550, 155, 120, 95, 350, 310),
    "iPhone 16 Pro": (800, 160, 130, 95, 550, 350),
    "iPhone 16 Pro Max": (900, 165, 130, 100, 600, 370),
    "iPhone 16e": (350, 140, 110, 85, 250, 280),
    "iPhone 17": (600, 170, 140, 100, 400, 350),
    "iPhone Air": (700, 180, 150, 110, 400, 400),
    "iPhone 17 Pro": (900, 180, 150, 110, 600, 400),
    "iPhone 17 Pro Max": (1000, 190, 150, 120, 650, 420),
    ANY_MODEL: (400, 120, 80, 80, 300, 250),
}
_CHEAP_PARTS = {Defect.CAMERA_LENS: 15, Defect.SPEAKER: 30, Defect.MICROPHONE: 35, Defect.BUTTONS: 35}
_NOTES = {
    Defect.SCREEN: "zamiennik dobrej jakości",
    Defect.BACK_GLASS: "szyba + klej",
    Defect.BATTERY: "bateria + taśma klejąca",
}


def default_parts() -> list[PartPrice]:
    """Domyślna tabela części. Face ID, zalanie i „nie włącza się" celowo pominięte —
    ich koszt jest nieprzewidywalny i liczony jako ryzyko (możesz je dopisać)."""
    rows: list[PartPrice] = []
    order = (Defect.SCREEN, Defect.BATTERY, Defect.CHARGING_PORT, Defect.BACK_GLASS, Defect.CAMERA, Defect.HOUSING)
    for model, prices in _BASE.items():
        for part, price in zip(order, prices, strict=True):
            if part is Defect.BACK_GLASS and price == 0:
                continue  # starsze modele mają aluminiowy tył
            rows.append(PartPrice(model, part, float(price), _NOTES.get(part, "")))
        for part, price in _CHEAP_PARTS.items():
            rows.append(PartPrice(model, part, float(price), ""))
    return rows
