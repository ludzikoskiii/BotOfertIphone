"""Kolory i etykiety wspólne dla tabeli i szczegółów — motyw jasny i ciemny (bez zależności od Qt)."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import RowColor, Verdict

ROW_BACKGROUND = {
    RowColor.GREEN: "#d3f9d8",
    RowColor.YELLOW: "#fff3bf",
    RowColor.RED: "#ffe3e3",
}
ACCENT = {
    RowColor.GREEN: "#2b8a3e",
    RowColor.YELLOW: "#e67700",
    RowColor.RED: "#c92a2a",
}
VERDICT_COLOR = {
    Verdict.BUY: "#2b8a3e",
    Verdict.NEGOTIATE: "#e67700",
    Verdict.VERIFY: "#868e96",
    Verdict.SKIP: "#c92a2a",
}
COLOR_LABEL = {
    RowColor.GREEN: "warta uwagi",
    RowColor.YELLOW: "przeciętna",
    RowColor.RED: "nieatrakcyjna",
}
FLAG_MARK = "⚑"
WATCHED_MARK = "★"
OUTDATED_MARK = "⌛"  # oferta zniknęła z portalu (w „Wybrane”)

THEME_LABELS = {"system": "Systemowy", "light": "Jasny", "dark": "Ciemny"}


@dataclass(frozen=True)
class Palette:
    name: str
    dark: bool
    window: str  # tło okna
    surface: str  # tło tabeli, pól edycji
    surface_alt: str  # nagłówki, panele
    border: str
    text: str
    muted: str
    accent: str
    selection: str  # zaznaczony wiersz (tło)
    selection_text: str
    positive: str  # zysk dodatni
    negative: str  # zysk ujemny, poważne flagi
    warning: str
    watched: str  # tło obserwowanych ofert
    verdict_bg: dict[Verdict, str] = field(default_factory=dict)
    verdict_fg: dict[Verdict, str] = field(default_factory=dict)

    @property
    def accent_by_color(self) -> dict[RowColor, str]:
        return {RowColor.GREEN: self.positive, RowColor.YELLOW: self.warning, RowColor.RED: self.negative}


LIGHT = Palette(
    name="light", dark=False,
    window="#f4f5f7", surface="#ffffff", surface_alt="#f1f3f5", border="#dee2e6",
    text="#212529", muted="#6c757d", accent="#1c7ed6",
    selection="#d0ebff", selection_text="#212529",
    positive="#2b8a3e", negative="#c92a2a", warning="#d9480f", watched="#fff9db",
    verdict_bg={Verdict.BUY: "#d3f9d8", Verdict.NEGOTIATE: "#fff3bf", Verdict.VERIFY: "#e9ecef",
                Verdict.SKIP: "#ffe3e3"},
    verdict_fg={Verdict.BUY: "#2b8a3e", Verdict.NEGOTIATE: "#b35c00", Verdict.VERIFY: "#495057",
                Verdict.SKIP: "#c92a2a"},
)
DARK = Palette(
    name="dark", dark=True,
    window="#1a1b1e", surface="#232428", surface_alt="#2c2e33", border="#3a3d44",
    text="#e9ecef", muted="#9a9ea6", accent="#4dabf7",
    selection="#1d3b57", selection_text="#f1f3f5",
    positive="#69db7c", negative="#ff8787", warning="#ffc078", watched="#2e2a17",
    verdict_bg={Verdict.BUY: "#1e3a26", Verdict.NEGOTIATE: "#3b3119", Verdict.VERIFY: "#34363c",
                Verdict.SKIP: "#43201f"},
    verdict_fg={Verdict.BUY: "#69db7c", Verdict.NEGOTIATE: "#ffd43b", Verdict.VERIFY: "#c1c2c5",
                Verdict.SKIP: "#ff8787"},
)
PALETTES = {"light": LIGHT, "dark": DARK}

_current: Palette = LIGHT


def current() -> Palette:
    """Aktywny motyw (ustawiany przez ``style.apply_theme``)."""
    return _current


def set_current(palette: Palette) -> None:
    global _current
    _current = palette
