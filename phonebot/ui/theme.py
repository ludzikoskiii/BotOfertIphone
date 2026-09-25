"""Kolory i etykiety wspólne dla tabeli i okna szczegółów (bez zależności od Qt)."""
from __future__ import annotations

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
    Verdict.SKIP: "#c92a2a",
}
COLOR_LABEL = {
    RowColor.GREEN: "warta uwagi",
    RowColor.YELLOW: "przeciętna",
    RowColor.RED: "nieatrakcyjna",
}
FLAG_MARK = "⚑"
WATCHED_MARK = "★"
