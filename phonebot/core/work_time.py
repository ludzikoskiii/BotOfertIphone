"""Czas pracy w wycenie: naprawa (z tabeli części) + stały czas obsługi, zysk na godzinę i próg.

* Naprawa: każda pozycja tabeli części ma czas pracy w minutach (edytowalny). Brak wpisu → czas domyślny
  dla rodzaju naprawy; usterka o nieznanym zakresie (Face ID, zalanie, „nie włącza się”, „na części”)
  → czas ryzyka z ustawień.
* Obsługa (każda transakcja): odbiór (paczka albo dojazd — z odległości), sprawdzenie telefonu,
  wystawienie ogłoszenia, sprzedaż (rozmowy, pakowanie, nadanie).
* Zysk na godzinę = przewidywany zysk / czas. Poniżej progu z ustawień werdykt spada: maksymalna cena
  zakupu jest liczona tak, żeby zysk na godzinę sięgnął progu.
* Koszt czasu (czas × stawka godzinowa) pokazywany osobno — nie jest odejmowany od zysku.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from .models import Condition, Defect, Mode, Offer, TimeItem

# orientacyjne czasy samodzielnej naprawy (minuty) — popraw je w tabeli części według własnej wprawy
DEFAULT_REPAIR_MINUTES: dict[Defect, int] = {
    Defect.SCREEN: 45,
    Defect.BATTERY: 30,
    Defect.CHARGING_PORT: 60,
    Defect.BACK_GLASS: 90,
    Defect.CAMERA: 40,
    Defect.CAMERA_LENS: 20,
    Defect.SPEAKER: 30,
    Defect.MICROPHONE: 45,
    Defect.BUTTONS: 45,
    Defect.HOUSING: 150,
}


@dataclass
class WorkTimeConfig:
    """Ustawienia → Czas pracy."""

    enabled: bool = True
    hourly_rate: float = 50.0  # stawka za godzinę Twojej pracy (koszt czasu pokazywany osobno)
    min_profit_per_hour: float = 40.0  # poniżej — niższy werdykt (0 = bez progu)
    parcel_minutes: int = 10  # zakup z wysyłką: odbiór paczki
    meeting_minutes: int = 15  # odbiór osobisty: spotkanie ze sprzedającym (plus dojazd)
    drive_kmh: float = 50.0  # średnia prędkość dojazdu (tam i z powrotem)
    pickup_unknown_minutes: int = 60  # odbiór osobisty, gdy odległość nieznana
    check_minutes: int = 20  # sprawdzenie telefonu (iCloud, IMEI, funkcje)
    listing_minutes: int = 20  # zdjęcia i wystawienie ogłoszenia
    selling_minutes: int = 30  # rozmowy z kupującymi, pakowanie, nadanie
    unknown_repair_minutes: int = 90  # naprawa o nieznanym zakresie


def repair_minutes(defect: Defect, table_minutes: int | None, cfg: WorkTimeConfig) -> int:
    if table_minutes is not None:
        return int(table_minutes)
    return DEFAULT_REPAIR_MINUTES.get(defect, cfg.unknown_repair_minutes)


# poprawka z faktycznych czasów z transakcji: (model, usterka, minuty) → (minuty, opis) albo None
Adjust = Callable[[str | None, Defect, int], "tuple[int, str] | None"]


def estimate(offer: Offer, parts, cfg: WorkTimeConfig, mode: Mode, adjust: Adjust | None = None) -> list[TimeItem]:
    """Pozycje czasu pracy dla oferty (naprawa + obsługa)."""
    items: list[TimeItem] = []
    model = offer.parsed.model
    if mode is Mode.REPAIR:
        for defect in offer.parsed.defects:
            row = parts.lookup(model, defect)
            if row is None and defect not in DEFAULT_REPAIR_MINUTES:
                items.append(TimeItem(f"Naprawa: {defect.label} (zakres nieznany)", cfg.unknown_repair_minutes,
                                      True))
                continue
            minutes = repair_minutes(defect, getattr(row, "minutes", None), cfg)
            label = f"Naprawa: {defect.label}"
            fixed = adjust(model, defect, minutes) if adjust else None
            if fixed is not None:
                minutes, note = fixed
                label += f" ({note})"
            items.append(TimeItem(label, minutes, True))
        if offer.parsed.condition is Condition.FOR_PARTS and not offer.parsed.defects:
            items.append(TimeItem("Naprawa: usterka „na części” (zakres nieznany)", cfg.unknown_repair_minutes,
                                  True))
    items.extend(handling(offer, cfg))
    return [i for i in items if i.minutes > 0]


def handling(offer: Offer, cfg: WorkTimeConfig) -> list[TimeItem]:
    """Stały czas obsługi: odbiór, sprawdzenie, wystawienie, sprzedaż."""
    if offer.raw.shipping_available is False:
        if offer.distance_km is not None and cfg.drive_kmh > 0:
            drive = round(2 * offer.distance_km / cfg.drive_kmh * 60)
            pickup = TimeItem(f"Dojazd i odbiór ({offer.distance_km:.0f} km × 2)", drive + cfg.meeting_minutes)
        else:
            pickup = TimeItem("Odbiór osobisty (odległość nieznana)", cfg.pickup_unknown_minutes)
    else:
        pickup = TimeItem("Odbiór paczki", cfg.parcel_minutes)
    return [pickup, TimeItem("Sprawdzenie telefonu", cfg.check_minutes),
            TimeItem("Wystawienie ogłoszenia", cfg.listing_minutes),
            TimeItem("Sprzedaż (rozmowy, pakowanie, nadanie)", cfg.selling_minutes)]


def per_hour(profit: float | None, minutes: int | None) -> float | None:
    if profit is None or not minutes:
        return None
    return round(profit / (minutes / 60), 2)


def format_minutes(minutes: int | None) -> str:
    """125 → „2 h 5 min”, 45 → „45 min”, 180 → „3 h”."""
    if minutes is None:
        return "—"
    h, m = divmod(int(round(minutes)), 60)
    if not h:
        return f"{m} min"
    return f"{h} h" + (f" {m} min" if m else "")


def summary(profit: float, minutes: int, rate: float | None) -> str:
    """„Zysk 150 zł, czas 3 h, czyli 50 zł/h.”"""
    return f"Zysk {profit:.0f} zł, czas {format_minutes(minutes)}, czyli {rate:.0f} zł/h."
