"""Wspólny wygląd aplikacji: paleta Qt, czcionka i arkusz stylów dla motywu jasnego/ciemnego.

Arkusz stylów celowo nie zawiera reguł ``QTableView::item`` — styl komórek przez CSS
przełącza Qt na wolniejsze rysowanie; kolory tabeli idą przez paletę i model.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QPalette
from PySide6.QtWidgets import QApplication

from .theme import DARK, LIGHT, Palette, set_current

SPACING = 8  # odstęp między elementami
MARGIN = 12  # margines paneli


def system_prefers_dark() -> bool:
    hints = QGuiApplication.styleHints()
    scheme = getattr(hints, "colorScheme", None)
    return bool(scheme and scheme() == Qt.ColorScheme.Dark)


def resolve(theme: str) -> Palette:
    if theme == "dark":
        return DARK
    if theme == "light":
        return LIGHT
    return DARK if system_prefers_dark() else LIGHT


def qt_palette(p: Palette) -> QPalette:
    pal = QPalette()
    role = QPalette.ColorRole
    colors = {
        role.Window: p.window, role.WindowText: p.text, role.Base: p.surface, role.AlternateBase: p.surface_alt,
        role.ToolTipBase: p.surface_alt, role.ToolTipText: p.text, role.PlaceholderText: p.muted,
        role.Text: p.text, role.Button: p.surface_alt, role.ButtonText: p.text, role.BrightText: p.negative,
        role.Highlight: p.selection, role.HighlightedText: p.selection_text, role.Link: p.accent,
        role.Light: p.surface, role.Midlight: p.surface_alt, role.Mid: p.border, role.Dark: p.border,
        role.Shadow: "#000000",
    }
    for r, c in colors.items():
        pal.setColor(r, QColor(c))
    disabled = QPalette.ColorGroup.Disabled
    for r in (role.Text, role.WindowText, role.ButtonText):
        pal.setColor(disabled, r, QColor(p.muted))
    return pal


_CHECK_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 14 14">'
              '<path d="M3 7.5l2.6 2.6L11 4.6" fill="none" stroke="{color}" stroke-width="2"'
              ' stroke-linecap="round" stroke-linejoin="round"/></svg>')


def check_icon(p: Palette) -> str:
    """Znaczek „✓” dla pól wyboru (arkusz stylów Qt przyjmuje tylko ścieżkę do pliku)."""
    path = Path(tempfile.gettempdir()) / f"phonebot_check_{p.name}.svg"
    svg = _CHECK_SVG.format(color=p.surface)
    try:
        if not path.exists() or path.read_text(encoding="utf-8") != svg:
            path.write_text(svg, encoding="utf-8")
    except OSError:
        return ""
    return path.as_posix()


def stylesheet(p: Palette) -> str:
    return f"""
QToolBar {{ spacing: {SPACING // 2}px; padding: 4px {SPACING}px; border: none;
           border-bottom: 1px solid {p.border}; background: {p.window}; }}
QToolBar QToolButton {{ padding: 5px 10px; border-radius: 6px; }}
QToolBar QToolButton:hover {{ background: {p.surface_alt}; }}
QToolBar QToolButton:checked {{ background: {p.selection}; color: {p.selection_text}; }}
QPushButton {{ padding: 5px 12px; border: 1px solid {p.border}; border-radius: 6px; background: {p.surface_alt}; }}
QPushButton:hover {{ border-color: {p.accent}; }}
QPushButton:default {{ border-color: {p.accent}; }}
QPushButton:disabled {{ color: {p.muted}; }}
QGroupBox {{ border: 1px solid {p.border}; border-radius: 8px; margin-top: 14px; padding: {SPACING}px;
            background: {p.surface}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {p.muted}; font-weight: bold; }}
QHeaderView::section {{ background: {p.surface_alt}; color: {p.muted}; padding: 6px 8px; border: none;
                        border-bottom: 1px solid {p.border}; border-right: 1px solid {p.border}; font-weight: bold; }}
QHeaderView::up-arrow, QHeaderView::down-arrow {{ subcontrol-origin: margin; subcontrol-position: top center;
                                               width: 8px; height: 5px; top: 1px; }}
QTableView {{ border: 1px solid {p.border}; border-radius: 6px; gridline-color: {p.border};
             background: {p.surface}; selection-background-color: {p.selection};
             selection-color: {p.selection_text}; }}
QSplitter::handle {{ background: {p.window}; }}
QSplitter::handle:hover {{ background: {p.border}; }}
QStatusBar {{ border-top: 1px solid {p.border}; background: {p.window}; }}
QStatusBar::item {{ border: none; }}
QScrollArea {{ border: none; background: {p.window}; }}
QScrollArea > QWidget > QWidget {{ background: {p.window}; }}
QTextBrowser {{ border: 1px solid {p.border}; border-radius: 6px; background: {p.surface}; }}
QToolTip {{ background: {p.surface_alt}; color: {p.text}; border: 1px solid {p.border}; padding: 4px; }}
QLabel#muted {{ color: {p.muted}; }}
QCheckBox::indicator, QListView::indicator {{ width: 14px; height: 14px; border: 1px solid {p.muted};
                                              border-radius: 3px; background: {p.surface}; }}
QCheckBox::indicator:hover, QListView::indicator:hover {{ border-color: {p.accent}; }}
QCheckBox::indicator:checked, QListView::indicator:checked {{ background: {p.accent}; border-color: {p.accent};
                                                              image: url({check_icon(p)}); }}
"""


def apply_theme(theme: str, font_pt: int = 10, app: QApplication | None = None) -> Palette:
    """Ustawia motyw całej aplikacji; zwraca użytą paletę."""
    app = app or QApplication.instance()
    palette = resolve(theme)
    set_current(palette)
    if app is None:
        return palette
    key = f"{palette.name}|{font_pt}"
    if app.property("phonebot_theme") == key:  # ponowne ustawienie arkusza przerysowuje wszystkie widżety
        return palette
    app.setProperty("phonebot_theme", key)
    app.setStyle("Fusion")
    app.setPalette(qt_palette(palette))
    font = QFont(app.font())
    font.setPointSize(max(8, min(16, int(font_pt))))
    app.setFont(font)
    app.setStyleSheet(stylesheet(palette))
    return palette
