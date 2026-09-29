"""Magazyn części: partie (ilość, cena i data zakupu), zgodność części między modelami, FIFO, niski stan.

* Partia = jeden zakup: rodzaj części (ekran, bateria…), pasujące modele, jakość, ilość, cena za sztukę,
  data zakupu, dostawca. Zużycie zdejmuje sztuki z najstarszej pasującej partii (FIFO).
* Zgodność: część z partii pasuje do modeli z jej listy oraz do modeli z tej samej grupy zgodności
  (edytowalna tabela: np. ekran iPhone XR = iPhone 11). Domyślne grupy są orientacyjne — sprawdź przed naprawą.
* Wycena naprawy: część na stanie → Twoja cena zakupu (najstarsza sztuka); brak → tabela cen części.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .models import Defect

QUALITIES = {"original": "oryginał", "replacement": "zamiennik"}


def default_compat() -> dict[str, list[list[str]]]:
    """Grupy modeli ze wspólną częścią (orientacyjnie — sprawdź u dostawcy przed zakupem)."""
    return {
        Defect.SCREEN.value: [["iPhone 12", "iPhone 12 Pro"], ["iPhone XR", "iPhone 11"],
                              ["iPhone 6s", "iPhone 7"], ["iPhone 8", "iPhone SE (2020)", "iPhone SE (2022)"]],
        Defect.BATTERY.value: [["iPhone 12", "iPhone 12 Pro"], ["iPhone 8", "iPhone SE (2020)", "iPhone SE (2022)"]],
        Defect.CHARGING_PORT.value: [["iPhone 12", "iPhone 12 Pro"],
                                     ["iPhone 8", "iPhone SE (2020)", "iPhone SE (2022)"]],
        Defect.BACK_GLASS.value: [],
        Defect.CAMERA_LENS.value: [["iPhone 12", "iPhone 12 mini"], ["iPhone 13", "iPhone 13 mini"]],
    }


@dataclass
class InventoryConfig:
    """Ustawienia magazynu (Ustawienia → Zakup i naprawa)."""

    enabled: bool = True
    score_bonus: int = 10  # premia do oceny oferty, gdy masz wszystkie potrzebne części
    low_stock_qty: int = 1  # ostrzeżenie, gdy zostało tyle sztuk albo mniej…
    frequent_uses: int = 2  # …a część zużywasz często: tyle razy…
    frequent_days: int = 60  # …w tylu ostatnich dniach
    compat: dict[str, list[list[str]]] = field(default_factory=default_compat)


@dataclass
class Lot:
    id: int | None
    part: Defect
    models: list[str]
    quality: str  # original | replacement
    qty: int
    unit_price: float
    bought_at: datetime | None = None
    supplier: str = ""
    note: str = ""

    @property
    def label(self) -> str:
        return f"{self.part.label} ({QUALITIES.get(self.quality, self.quality)})"


def compatible_models(model: str, part: Defect, compat: dict[str, list[list[str]]]) -> set[str]:
    """Model + modele z tej samej grupy zgodności dla danej części."""
    out = {model}
    for group in compat.get(part.value, []):
        if model in group:
            out.update(group)
    return out


def fits(lot: Lot, model: str | None, part: Defect, compat: dict[str, list[list[str]]]) -> bool:
    if model is None or lot.part is not part or lot.qty <= 0:
        return False
    return bool(compatible_models(model, part, compat) & set(lot.models))


class Stock:
    """Stan magazynu do wyceny (bez bazy): najstarsza pasująca sztuka i czy masz część."""

    def __init__(self, lots: list[Lot], cfg: InventoryConfig):
        self.cfg = cfg
        self.lots = sorted([lot for lot in lots if lot.qty > 0], key=lambda lot: _normalized_age(lot))

    def oldest(self, model: str | None, part: Defect) -> Lot | None:
        return next((lot for lot in self.lots if fits(lot, model, part, self.cfg.compat)), None)

    def available(self, model: str | None, part: Defect) -> int:
        return sum(lot.qty for lot in self.lots if fits(lot, model, part, self.cfg.compat))


def _normalized_age(lot: Lot) -> tuple:
    when = lot.bought_at
    stamp = when.timestamp() if when is not None else 0.0
    return (stamp, lot.id or 0)


def fifo_pick(lots: list[Lot], model: str | None, part: Defect, qty: int,
              compat: dict[str, list[list[str]]]) -> list[tuple[Lot, int]]:
    """Z których partii zdjąć ``qty`` sztuk (najstarsze najpierw). Pusta lista = za mało na stanie."""
    need = qty
    out: list[tuple[Lot, int]] = []
    for lot in sorted((lt for lt in lots if fits(lt, model, part, compat)), key=_normalized_age):
        take = min(lot.qty, need)
        out.append((lot, take))
        need -= take
        if need <= 0:
            return out
    return []


@dataclass
class LowStock:
    part: Defect
    models: list[str]
    qty: int
    uses: int

    def message(self, days: int) -> str:
        who = ", ".join(self.models[:3]) + ("…" if len(self.models) > 3 else "")
        left = "brak na stanie" if self.qty <= 0 else f"zostało {self.qty} szt."
        return f"{self.part.label} — {who}: {left} (zużyte {self.uses}× w ostatnich {days} dniach)"


def low_stock(lots: list[Lot], usage: list[tuple[Defect, str, datetime]], cfg: InventoryConfig,
              now: datetime) -> list[LowStock]:
    """Części zużywane często (``frequent_uses`` w ``frequent_days`` dni), których zostało mało.

    ``usage`` — (część, model, kiedy) z historii zużycia."""
    since = now - timedelta(days=cfg.frequent_days)
    counts: dict[tuple[Defect, str], int] = {}
    for part, model, when in usage:
        if when >= since:
            counts[(part, model)] = counts.get((part, model), 0) + 1
    out: list[LowStock] = []
    seen: set[tuple[Defect, frozenset[str]]] = set()
    for (part, model), uses in sorted(counts.items(), key=lambda kv: -kv[1]):
        if uses < cfg.frequent_uses:
            continue
        group = compatible_models(model, part, cfg.compat)
        key = (part, frozenset(group))
        if key in seen:
            continue
        seen.add(key)
        qty = sum(lot.qty for lot in lots if lot.part is part and group & set(lot.models))
        if qty <= cfg.low_stock_qty:
            out.append(LowStock(part, sorted(group), qty, uses))
    return out
