"""Okno ustawień: wszystkie progi, koszty i opcje pobierania edytowalne bez zmian w kodzie."""
from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from functools import reduce
from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.market import manual_key
from ..core.models import RedFlag
from ..core.places import Place
from ..core.settings import MIN_PROFIT_MODE_LABELS, SalesChannel, Settings
from .location_dialog import LocationDialog
from .table_model import SOURCE_NAMES


@dataclass
class Field:
    path: str
    label: str
    kind: str = "float"  # float | int | bool | mode
    minimum: float = 0
    maximum: float = 100000
    step: float = 1
    suffix: str = ""
    decimals: int = 0
    tip: str = ""


def _get(obj: Any, path: str) -> Any:
    return reduce(getattr, path.split("."), obj)


def _set(obj: Any, path: str, value: Any) -> None:
    *parents, last = path.split(".")
    setattr(reduce(getattr, parents, obj), last, value)


def _num(value: str, default: float = 0.0) -> float:
    try:
        return float(value.replace(",", ".").replace(" ", ""))
    except ValueError:
        return default


class EditableTable(QWidget):
    """Tabela z przyciskami „Dodaj” / „Usuń” (wartości jako tekst)."""

    def __init__(self, headers: list[str], rows: list[list[Any]], parent=None):
        super().__init__(parent)
        self.table = QTableWidget(0, len(headers))
        self.table.setHorizontalHeaderLabels(headers)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        for row in rows:
            self.add_row(row)
        add, remove = QPushButton("Dodaj wiersz"), QPushButton("Usuń zaznaczony")
        add.clicked.connect(lambda: self.add_row([""] * len(headers)))
        remove.clicked.connect(lambda: self.table.removeRow(self.table.currentRow()))
        buttons = QHBoxLayout()
        buttons.addWidget(add)
        buttons.addWidget(remove)
        buttons.addStretch(1)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.table)
        lay.addLayout(buttons)

    def add_row(self, values: list[Any]) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        for c, v in enumerate(values):
            self.table.setItem(r, c, QTableWidgetItem("" if v is None else f"{v:g}" if isinstance(v, float) else str(v)))

    def values(self) -> list[list[str]]:
        out = []
        for r in range(self.table.rowCount()):
            row = [(self.table.item(r, c).text().strip() if self.table.item(r, c) else "")
                   for c in range(self.table.columnCount())]
            if any(row):
                out.append(row)
        return out


GENERAL = [
    Field("refresh_minutes", "Automatyczne odświeżanie co", "int", 0, 1440, 5, " min",
          tip="0 = tylko ręcznie (działa od etapu 5)"),
    Field("price_min", "Pobieraj oferty od ceny", "float", 0, 20000, 50, " zł"),
    Field("price_max", "Pobieraj oferty do ceny (0 = bez limitu)", "float", 0, 20000, 50, " zł"),
    Field("request_delay_s", "Odstęp między zapytaniami do portalu", "float", 1, 60, 0.5, " s", 1),
    Field("max_pages_per_query", "Stron wyników na frazę", "int", 1, 10),
    Field("source_timeout_s", "Limit czasu jednego portalu", "float", 20, 900, 10, " s"),
    Field("offer_stale_days", "Ukryj oferty niewidziane od", "int", 1, 60, 1, " dni"),
    Field("olx_category_id", "ID kategorii OLX (0 = bez filtra)", "int", 0, 999999),
]
BUYING = [
    Field("buy_shipping_cost", "Wysyłka telefonu do Ciebie", suffix=" zł"),
    Field("pickup_cost_per_km", "Dojazd po odbiór (za km, w obie strony)", "float", 0, 10, 0.1, " zł", 2),
    Field("pickup_flat_cost", "Odbiór osobisty, gdy odległość nieznana", suffix=" zł"),
    Field("own_labor_cost", "Własna robocizna za naprawę", suffix=" zł"),
    Field("parts_shipping_cost", "Wysyłka części", suffix=" zł"),
    Field("unknown_defect_risk_cost", "Ryzyko przy usterce o nieznanym koszcie", suffix=" zł"),
    Field("battery_health_threshold", "Bateria do wymiany poniżej", "int", 50, 100, 1, " %"),
]
MARKET = [
    Field("market_window_days", "Okno danych rynkowych", "int", 3, 365, 1, " dni"),
    Field("market_min_samples", "Min. liczba ofert do mediany", "int", 1, 50),
    Field("market_min_samples_fallback", "Min. liczba ofert (niska pewność)", "int", 1, 50),
    Field("asking_price_correction", "Korekta cen wywoławczych (×)", "float", 0.5, 1.2, 0.01, "", 2),
    Field("new_condition_multiplier", "Mnożnik dla nowych (×)", "float", 1, 2, 0.01, "", 2),
    Field("storage_step_pct", "Wzrost wartości na podwojenie pamięci", "float", 0, 50, 1, " %", 1),
    Field("min_valid_price", "Ignoruj ceny poniżej", suffix=" zł"),
]
VERDICT = [
    Field("negotiation_margin_pct", "Margines negocjacji ponad max cenę", "float", 0, 100, 1, " %", 1),
    Field("negotiable_bonus_pct", "Dodatkowy margines, gdy „do negocjacji”", "float", 0, 100, 1, " %", 1),
    Field("opening_ratio", "Cena otwierająca (× max cena)", "float", 0.5, 1, 0.01, "", 2),
    Field("buy_try_discount_pct", "Przy KUPUJ zaproponuj taniej o", "float", 0, 50, 1, " %", 1),
    Field("hard_flags_force_skip", "Poważne flagi wymuszają ODPUŚĆ", "bool"),
    Field("suspicious_price_ratio_working", "„Podejrzanie tanio” (sprawny) poniżej × wartości", "float", 0, 1, 0.05,
          "", 2),
    Field("suspicious_price_ratio_damaged", "„Podejrzanie tanio” (uszkodzony) poniżej × wartości", "float", 0, 1,
          0.05, "", 2),
    Field("score_green", "Zielony od oceny", "int", 0, 100),
    Field("score_yellow", "Żółty od oceny", "int", 0, 100),
]


class SettingsDialog(QDialog):
    parts_editor_requested = Signal()

    def __init__(self, settings: Settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Ustawienia")
        self.resize(760, 640)
        self.settings = copy.deepcopy(settings)
        self._readers: list[Callable[[Settings], None]] = []

        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), "Ogólne i pobieranie")
        tabs.addTab(self._profit_tab(), "Zysk i sprzedaż")
        tabs.addTab(self._buying_tab(), "Zakup i naprawa")
        tabs.addTab(self._market_tab(), "Wycena rynkowa")
        tabs.addTab(self._verdict_tab(), "Werdykt i flagi")

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Zapisz")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Anuluj")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addWidget(tabs)
        lay.addWidget(buttons)

    # ---------------------------------------------------------- pomocnicze ---

    def _form(self, fields: list[Field], form: QFormLayout | None = None) -> QFormLayout:
        form = form or QFormLayout()
        for f in fields:
            value = _get(self.settings, f.path)
            if f.kind == "bool":
                w = QCheckBox()
                w.setChecked(bool(value))
                self._readers.append(lambda s, w=w, f=f: _set(s, f.path, w.isChecked()))
            elif f.kind == "mode":
                w = QComboBox()
                for key, label in MIN_PROFIT_MODE_LABELS.items():
                    w.addItem(label, key)
                w.setCurrentIndex(max(0, w.findData(value)))
                self._readers.append(lambda s, w=w, f=f: _set(s, f.path, w.currentData()))
            elif f.kind == "int":
                w = QSpinBox(minimum=int(f.minimum), maximum=int(f.maximum), singleStep=int(f.step), suffix=f.suffix)
                w.setValue(int(value))
                self._readers.append(lambda s, w=w, f=f: _set(s, f.path, w.value()))
            else:
                w = QDoubleSpinBox(minimum=f.minimum, maximum=f.maximum, singleStep=f.step, suffix=f.suffix,
                                   decimals=f.decimals)
                w.setValue(float(value))
                self._readers.append(lambda s, w=w, f=f: _set(s, f.path, float(w.value())))
            if f.tip:
                w.setToolTip(f.tip)
            w.setObjectName(f.path)
            form.addRow(f.label + ":", w)
        return form

    @staticmethod
    def _page(*widgets: QWidget | QFormLayout) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        for w in widgets:
            if isinstance(w, QFormLayout):
                lay.addLayout(w)
            else:
                lay.addWidget(w)
        lay.addStretch(1)
        return page

    # ---------------------------------------------------------------- zakładki ---

    def _general_tab(self) -> QWidget:
        s = self.settings
        loc = QGroupBox("Twoja miejscowość")
        self.location_label = QLabel()
        change = QPushButton("Zmień…")
        change.clicked.connect(self._change_location)
        row = QHBoxLayout(loc)
        row.addWidget(self.location_label, 1)
        row.addWidget(change)
        self._update_location_label()

        sources = QGroupBox("Portale")
        sl = QVBoxLayout(sources)
        self.source_checks = {}
        for key, name in SOURCE_NAMES.items():
            cb = QCheckBox(name + (" (best effort — może być blokowany)" if key == "vinted" else ""))
            cb.setChecked(s.enabled_sources.get(key, True))
            sl.addWidget(cb)
            self.source_checks[key] = cb
        self._readers.append(lambda st: setattr(st, "enabled_sources",
                                                {k: cb.isChecked() for k, cb in self.source_checks.items()}))

        form = self._form(GENERAL)
        self.watched = QLineEdit(", ".join(s.watched_models))
        self.watched.setPlaceholderText("np. iPhone 13, iPhone 14 Pro (puste = ogólne „iphone”)")
        form.addRow("Dodatkowe frazy/modele do wyszukiwania:", self.watched)
        self._readers.append(lambda st: setattr(
            st, "watched_models", [m.strip() for m in self.watched.text().split(",") if m.strip()]))
        return self._page(loc, sources, form)

    def _update_location_label(self) -> None:
        s = self.settings
        self.location_label.setText(f"<b>{s.location_name}</b> ({s.home_lat:.4f}, {s.home_lon:.4f})")

    def _change_location(self) -> None:
        s = self.settings
        dlg = LocationDialog(s.location_name, s.home_lat, s.home_lon, self)
        if dlg.exec():
            self.apply_location(dlg.place())

    def apply_location(self, place: Place) -> None:
        self.settings.location_name, self.settings.home_lat, self.settings.home_lon = place.name, place.lat, place.lon
        self._update_location_label()

    def _profit_tab(self) -> QWidget:
        s = self.settings
        rules = []
        for attr, title in (("profit_repair", "Minimalny zysk — tryb „Naprawa → sprzedaż”"),
                            ("profit_resell", "Minimalny zysk — tryb „Szybki resell”")):
            g = QGroupBox(title)
            g.setLayout(self._form([
                Field(f"{attr}.min_amount", "Kwota", "float", 0, 10000, 10, " zł"),
                Field(f"{attr}.min_percent", "Procent od zainwestowanej kwoty", "float", 0, 500, 1, " %", 1),
                Field(f"{attr}.mode", "Jak łączyć", "mode"),
            ]))
            rules.append(g)

        channels = QGroupBox("Kanały sprzedaży (wartości orientacyjne — sprawdź cenniki)")
        self.channels = EditableTable(["Nazwa", "Prowizja %", "Opłata stała zł", "Wysyłka (Ty płacisz) zł"],
                                      [[c.name, c.commission_pct, c.fixed_fee, c.shipping_cost]
                                       for c in s.sales_channels])
        self.active_channel = QComboBox()
        self.active_channel.addItems([c.name for c in s.sales_channels])
        self.active_channel.setCurrentText(s.active_sales_channel)
        self.active_channel.setEditable(True)
        cl = QVBoxLayout(channels)
        cl.addWidget(self.channels)
        form = QFormLayout()
        form.addRow("Sprzedaję przez:", self.active_channel)
        cl.addLayout(form)
        self._readers.append(self._read_channels)
        other = self._form([Field("packaging_cost", "Pakowanie przy sprzedaży", suffix=" zł")])
        return self._page(*rules, channels, other)

    def _read_channels(self, s: Settings) -> None:
        rows = self.channels.values()
        channels = [SalesChannel(r[0] or "Kanał", _num(r[1]), _num(r[2]), _num(r[3])) for r in rows]
        s.sales_channels = channels or [SalesChannel("OLX")]
        s.active_sales_channel = self.active_channel.currentText().strip() or s.sales_channels[0].name

    def _buying_tab(self) -> QWidget:
        s = self.settings
        form = self._form(BUYING)
        fees = QGroupBox("Opłaty kupującego (ochrona kupujących)")
        self.fees = QTableWidget(len(SOURCE_NAMES), 3)
        self.fees.setHorizontalHeaderLabels(["Portal", "Procent ceny %", "Kwota stała zł"])
        self.fees.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.fees.verticalHeader().hide()
        for r, (key, name) in enumerate(SOURCE_NAMES.items()):
            pct, fixed = (s.buyer_fees.get(key) or [0.0, 0.0])[:2]
            item = QTableWidgetItem(name)
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            item.setData(Qt.ItemDataRole.UserRole, key)
            self.fees.setItem(r, 0, item)
            self.fees.setItem(r, 1, QTableWidgetItem(f"{pct:g}"))
            self.fees.setItem(r, 2, QTableWidgetItem(f"{fixed:g}"))
        self.fees.setMaximumHeight(130)
        QVBoxLayout(fees).addWidget(self.fees)
        self._readers.append(lambda st: setattr(st, "buyer_fees", {
            self.fees.item(r, 0).data(Qt.ItemDataRole.UserRole): [_num(self.fees.item(r, 1).text()),
                                                                   _num(self.fees.item(r, 2).text())]
            for r in range(self.fees.rowCount())}))
        parts = QPushButton("Edytuj tabelę cen części…")
        parts.clicked.connect(self.parts_editor_requested.emit)
        return self._page(form, fees, parts)

    def _market_tab(self) -> QWidget:
        s = self.settings
        form = self._form(MARKET)
        manual = QGroupBox("Ręczne wartości rynkowe (mają pierwszeństwo przed medianą)")
        rows = []
        for key, value in s.manual_market_values.items():
            model, _, storage = key.partition("|")
            rows.append([model, storage, value])
        self.manual = EditableTable(["Model (np. iPhone 13)", "Pamięć GB (np. 128)", "Wartość zł"], rows)
        QVBoxLayout(manual).addWidget(self.manual)
        self._readers.append(self._read_manual)
        return self._page(form, manual)

    def _read_manual(self, s: Settings) -> None:
        values = {}
        for model, storage, value in self.manual.values():
            if model and _num(value) > 0:
                gb = int(_num(storage)) if storage else None
                values[manual_key(model, gb)] = _num(value)
        s.manual_market_values = values

    def _verdict_tab(self) -> QWidget:
        s = self.settings
        form = self._form(VERDICT)
        pen = QGroupBox("Kary za czerwone flagi (punkty odejmowane od oceny)")
        pl = QFormLayout(pen)
        self.penalties: dict[str, QSpinBox] = {}
        for flag in RedFlag:
            w = QSpinBox(minimum=0, maximum=100, suffix=" pkt")
            w.setValue(s.penalty(flag))
            pl.addRow(flag.label + ":", w)
            self.penalties[flag.value] = w
        self._readers.append(lambda st: setattr(st, "flag_penalties",
                                                {k: w.value() for k, w in self.penalties.items()}))
        return self._page(form, pen)

    # ---------------------------------------------------------------- wynik ---

    def result_settings(self) -> Settings:
        result = copy.deepcopy(self.settings)
        for read in self._readers:
            read(result)
        if result.score_yellow > result.score_green:
            result.score_yellow = result.score_green
        return result
