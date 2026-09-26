"""Pasek statusu źródeł: czy każdy portal działa, zwraca oferty, czy jest zablokowany."""
from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

# kind → (ikona, tekst, kolor tła, kolor tekstu, podpowiedź co robić)
STATUS_STYLE: dict[str, tuple[str, str, str, str, str]] = {
    "ok": ("✔", "działa", "#d3f9d8", "#2b8a3e", ""),
    "empty": ("⚠", "brak ofert", "#fff3bf", "#e67700",
              "Portal odpowiada, ale nie zwrócił ofert — możliwa zmiana formatu strony. Uruchom Diagnostykę."),
    "network": ("✖", "brak połączenia", "#ffe8cc", "#d9480f",
                "Sprawdź połączenie z internetem / zaporę. Aplikacja ponowi próbę przy następnym odświeżeniu."),
    "timeout": ("✖", "przekroczony czas", "#ffe8cc", "#d9480f",
                "Portal odpowiadał zbyt wolno. Możesz zwiększyć limit czasu w Ustawieniach."),
    "blocked": ("⛔", "zablokowane", "#ffe3e3", "#c92a2a",
                "Portal blokuje automatyczne pobieranie (np. ochrona antybotowa). Zwiększ odstęp między "
                "zapytaniami i odśwież rzadziej; jeśli blokada trwa, wyłącz to źródło."),
    "changed": ("✖", "zmiana formatu", "#ffe3e3", "#c92a2a",
                "Portal zmienił adres API lub strukturę danych — adapter wymaga aktualizacji. "
                "Uruchom Diagnostykę i prześlij raport."),
    "error": ("✖", "błąd", "#ffe3e3", "#c92a2a", "Szczegóły w logu aplikacji. Uruchom Diagnostykę."),
    "never": ("–", "nie sprawdzano", "#f1f3f5", "#868e96", "Kliknij „Odśwież oferty”."),
    "disabled": ("○", "wyłączone", "#f1f3f5", "#adb5bd", "Włącz w Ustawieniach → Portale."),
}


def status_text(name: str, kind: str, found: int | None = None) -> str:
    icon, label, *_ = STATUS_STYLE.get(kind, STATUS_STYLE["error"])
    count = f" ({found})" if kind == "ok" and found is not None else ""
    return f"{icon} {name}: {label}{count}"


class SourceStatusBar(QWidget):
    diagnose_requested = Signal()

    def __init__(self, sources: dict[str, str], parent=None):
        super().__init__(parent)
        self._labels: dict[str, QLabel] = {}
        self.kinds: dict[str, str] = {}
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(6)
        lay.addWidget(QLabel("Źródła:"))
        for key, name in sources.items():
            lbl = QLabel()
            lbl.setObjectName(f"status_{key}")
            self._labels[key] = lbl
            lay.addWidget(lbl)
            self.set_status(key, name, "never")
        btn = QPushButton("🩺 Diagnostyka")
        btn.setToolTip("Sprawdza każdy portal osobno i pokazuje, na którym etapie jest problem")
        btn.clicked.connect(self.diagnose_requested.emit)
        lay.addWidget(btn)
        lay.addStretch(1)

    def set_status(self, key: str, name: str, kind: str, *, found: int | None = None, error: str | None = None,
                   when: datetime | None = None) -> None:
        lbl = self._labels.get(key)
        if lbl is None:
            return
        self.kinds[key] = kind
        _, _, bg, fg, hint = STATUS_STYLE.get(kind, STATUS_STYLE["error"])
        lbl.setText(status_text(name, kind, found))
        lbl.setStyleSheet(f"background:{bg}; color:{fg}; border-radius:4px; padding:1px 6px; font-weight:bold;")
        tip = []
        if when:
            tip.append(f"Ostatnie sprawdzenie: {when.astimezone():%d.%m %H:%M}")
        if found is not None and kind != "never":
            tip.append(f"Pobranych ofert: {found}")
        if error:
            tip.append(f"Błąd: {error}")
        if hint:
            tip.append(hint)
        lbl.setToolTip("\n".join(tip))

    def text_of(self, key: str) -> str:
        return self._labels[key].text()
