"""Pasek sortowania nad tabelą: do trzech poziomów (np. werdykt, potem zysk), kierunek jednym kliknięciem.

To samo sortowanie ustawia kliknięcie nagłówka tabeli (Shift+klik dodaje kolejny poziom) — pasek
zawsze pokazuje aktualny stan.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QMenu, QSizePolicy, QToolButton, QWidget

from ..core.sorting import FIELDS, MAX_LEVELS, PRESETS, SortLevel, describe, level, normalize

_NONE = ""


class SortBar(QWidget):
    spec_changed = Signal(tuple)  # tuple[SortLevel, ...]

    def __init__(self, parent=None):
        super().__init__(parent)
        self._spec: tuple[SortLevel, ...] = ()
        self._updating = False
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 4)
        lay.setSpacing(6)
        lay.addWidget(QLabel("Sortuj:"))
        self.field_combos: list[QComboBox] = []
        self.dir_buttons: list[QToolButton] = []
        for i in range(MAX_LEVELS):
            if i:
                then = QLabel("potem")
                then.setObjectName("muted")
                lay.addWidget(then)
            combo = QComboBox(self)
            combo.setObjectName(f"sort_field_{i}")
            if i:
                combo.addItem("—", _NONE)
            for key, f in FIELDS.items():
                combo.addItem(f.label, key)
            combo.currentIndexChanged.connect(lambda _=0, i=i: self._field_changed(i))
            button = QToolButton(self)
            button.setObjectName(f"sort_dir_{i}")
            button.setToolTip("Kliknij, aby odwrócić kierunek (rosnąco / malejąco)")
            button.clicked.connect(lambda _=False, i=i: self._toggle(i))
            lay.addWidget(combo)
            lay.addWidget(button)
            self.field_combos.append(combo)
            self.dir_buttons.append(button)
        self.presets_btn = QToolButton(self)
        self.presets_btn.setText("Gotowe ▾")
        self.presets_btn.setToolTip("Gotowe zestawy sortowania")
        self.presets_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu = QMenu(self.presets_btn)
        for name, spec in PRESETS.items():
            action = QAction(name, menu)
            action.setToolTip(describe(spec))
            action.triggered.connect(lambda _=False, spec=spec: self.spec_changed.emit(spec))
            menu.addAction(action)
        self.presets_btn.setMenu(menu)
        lay.addWidget(self.presets_btn)
        hint = QLabel("Klik w nagłówek kolumny też sortuje; Shift+klik dodaje kolejny poziom.")
        hint.setObjectName("muted")
        # podpowiedź nie może poszerzać okna — przy wąskiej tabeli jest po prostu ucinana
        hint.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        hint.setMinimumWidth(0)
        lay.addWidget(hint, 1)

    @property
    def spec(self) -> tuple[SortLevel, ...]:
        return self._spec

    def set_spec(self, spec) -> None:
        """Pokazuje sortowanie (bez sygnału)."""
        self._spec = normalize(spec)
        self._updating = True
        try:
            for i, (combo, button) in enumerate(zip(self.field_combos, self.dir_buttons, strict=True)):
                lv = self._spec[i] if i < len(self._spec) else None
                combo.setCurrentIndex(combo.findData(lv.field if lv else _NONE))
                # kolejny poziom można wybrać dopiero po ustawieniu poprzedniego
                combo.setEnabled(i <= len(self._spec))
                button.setVisible(lv is not None)
                if lv is not None:
                    f = FIELDS[lv.field]
                    button.setText(("↓ " + f.desc_label) if lv.descending else ("↑ " + f.asc_label))
        finally:
            self._updating = False
        self.setToolTip("Sortowanie: " + describe(self._spec))

    def _field_changed(self, i: int) -> None:
        if self._updating:
            return
        spec = list(self._spec)
        key = self.field_combos[i].currentData()
        if not key:
            spec = spec[:i]  # „—” usuwa ten poziom i następne
        elif i < len(spec):
            spec[i] = level(key)
        else:
            spec.append(level(key))
        self._emit(spec)

    def _toggle(self, i: int) -> None:
        if i < len(self._spec):
            spec = list(self._spec)
            spec[i] = spec[i].toggled()
            self._emit(spec)

    def _emit(self, spec) -> None:
        spec = normalize(spec)
        self.set_spec(spec)
        self.spec_changed.emit(spec)
