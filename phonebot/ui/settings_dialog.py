"""Okno ustawień: wszystkie progi, koszty i opcje pobierania edytowalne bez zmian w kodzie."""
from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass
from functools import reduce
from typing import Any

from PySide6.QtCore import Qt, Signal, Slot
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
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from ..core.market import manual_key
from ..core.models import RedFlag, Verdict
from ..core.places import Place
from ..core.sanity import VERDICT_CHOICES
from ..core.settings import MIN_PROFIT_MODE_LABELS, VINTED_COUNTRY_MODES, SalesChannel, Settings
from .location_dialog import LocationDialog
from .table_model import SOURCE_NAMES
from .theme import THEME_LABELS


@dataclass
class Field:
    path: str
    label: str
    kind: str = "float"  # float | int | bool | mode | pct (ułamek pokazywany w %) | choice
    minimum: float = 0
    maximum: float = 100000
    step: float = 1
    suffix: str = ""
    decimals: int = 0
    tip: str = ""
    choices: dict[str, str] | None = None  # dla kind="choice": wartość → etykieta


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
          tip="0 = tylko ręcznie"),
    Field("price_min", "Pobieraj oferty od ceny", "float", 0, 20000, 50, " zł"),
    Field("price_max", "Pobieraj oferty do ceny (0 = bez limitu)", "float", 0, 20000, 50, " zł"),
    Field("request_delay_s", "Odstęp między zapytaniami do portalu", "float", 1, 60, 0.5, " s", 1),
    Field("max_pages_per_query", "Stron wyników na frazę", "int", 1, 10),
    Field("source_timeout_s", "Limit czasu jednego portalu", "float", 20, 900, 10, " s"),
    Field("offer_stale_days", "Ukryj oferty niewidziane od", "int", 1, 60, 1, " dni"),
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
    Field("market_floor_ratio", "Pomijaj w wycenie ceny poniżej", "pct", 0, 90, 5, " % mediany",
          tip="Chroni wycenę przed akcesoriami i oszustwami, które przeszły filtry"),
]
SANITY = [
    Field("sanity.price_min_ratio_working", "Cena podejrzana (telefon sprawny) poniżej", "pct", 0, 100, 5,
          " % wartości rynkowej", tip="Taka oferta dostaje najwyżej DO WERYFIKACJI — nigdy KUPUJ"),
    Field("sanity.price_min_ratio_damaged", "Cena podejrzana (uszkodzony / na części) poniżej", "pct", 0, 100, 5,
          " % wartości rynkowej", tip="Niższy próg, bo tanie uszkodzone telefony to normalna okazja do naprawy"),
    Field("sanity.profit_max_pct", "Zysk do weryfikacji powyżej", "float", 10, 10000, 10, " % inwestycji"),
    Field("sanity.unknown_storage_verify", "Nieznana pamięć → najwyżej DO WERYFIKACJI", "bool"),
    Field("sanity.soft_flag_cap", "Oferta z flagą ostrzegawczą (np. brak zdjęć)", "choice", choices=VERDICT_CHOICES),
    Field("sanity.hard_flag_cap", "Oferta z poważną flagą (iCloud, IMEI, podróbka)", "choice",
          choices=VERDICT_CHOICES),
]
SERIAL = [
    Field("sanity.serial_enabled", "Wykrywaj sprzedawców seryjnych i ukrywaj ich oferty", "bool"),
    Field("sanity.serial_min_offers", "Liczba tanich ofert jednego sprzedawcy", "int", 2, 50),
    Field("sanity.serial_price_ratio", "„Tania” oferta — poniżej", "pct", 5, 100, 5, " % wartości rynkowej"),
]
COUNTRY = [
    Field("vinted_country_mode", "Oferty z Vinted", "choice", choices=VINTED_COUNTRY_MODES),
    Field("seller_lookups_per_scan", "Sprawdzaj kraj — sprzedawców na odświeżenie", "int", 0, 200,
          tip="Każdy sprzedawca to jedno zapytanie do Vinted (wynik jest zapamiętywany na 30 dni). "
              "0 = tylko język tytułu."),
]
VERDICT = [
    Field("negotiation_margin_pct", "Margines negocjacji ponad max cenę", "float", 0, 100, 1, " %", 1),
    Field("negotiable_bonus_pct", "Dodatkowy margines, gdy „do negocjacji”", "float", 0, 100, 1, " %", 1),
    Field("opening_ratio", "Cena otwierająca (× max cena)", "float", 0.5, 1, 0.01, "", 2),
    Field("buy_try_discount_pct", "Przy KUPUJ zaproponuj taniej o", "float", 0, 50, 1, " %", 1),
    Field("suspicious_price_ratio_working", "„Podejrzanie tanio” (sprawny) poniżej × wartości", "float", 0, 1, 0.05,
          "", 2),
    Field("suspicious_price_ratio_damaged", "„Podejrzanie tanio” (uszkodzony) poniżej × wartości", "float", 0, 1,
          0.05, "", 2),
    Field("score_green", "Zielony od oceny", "int", 0, 100),
    Field("score_yellow", "Żółty od oceny", "int", 0, 100),
]


AI_TEXT = [
    Field("ml.text_enabled", "Klasyfikator tytułów (telefon / akcesorium / część / kupię)", "bool"),
    Field("ml.text_phone_conf", "Tytuł potwierdza telefon od pewności", "pct", 30, 99, 5),
    Field("ml.text_conflict_conf", "Tytuł przeczy regułom od pewności", "pct", 30, 99, 5,
          tip="Gdy klasyfikator jest tak pewny, że to akcesorium/część/kupię — werdykt najwyżej DO WERYFIKACJI"),
    Field("ml.retrain_after_labels", "Douczaj automatycznie po nowych oznaczeniach", "int", 5, 1000, 5),
    Field("ml.learn_from_hidden", "Ucz się też z ofert ukrytych ręcznie (jako „nie telefon”)", "bool",
          tip="Słaba wskazówka. Ukryte oferty, które model uważa za telefony (ukryte np. przez cenę), są "
              "pomijane. Pewną informację daje przycisk „To nie jest telefon”."),
]
AI_LLM = [
    Field("ml.llm_enabled", "Czytaj opisy ofert „DO WERYFIKACJI” lokalnym modelem (Ollama)", "bool",
          tip="Wyciąga z opisu pamięć, kondycję baterii, usterki, blokady i „na części”. Każdy opis raz."),
    Field("ml.llm_think", "Tryb „myślenia” modelu (dokładniej, kilka razy wolniej)", "bool",
          tip="Qwen3 najpierw analizuje ogłoszenie, potem odpowiada. W teście na 16 opisach nie poprawił "
              "wyników (93% wobec 95%), a był ok. 5 razy wolniejszy — dlatego domyślnie wyłączony."),
    Field("ml.llm_fetch_pages", "Pobieraj opis ze strony oferty, gdy wyniki wyszukiwania go nie mają", "bool",
          tip="Vinted i Sprzedajemy.pl nie podają opisu w wynikach. Jedno zapytanie na ofertę DO WERYFIKACJI, "
              "w tym samym limicie zapytań co wyszukiwanie; blokada portalu wstrzymuje pobieranie."),
]
AI_PHOTO = [
    Field("ml.photo_enabled", "Analiza głównego zdjęcia (CLIP, na procesorze)", "bool"),
    Field("ml.photo_phone_conf", "Zdjęcie potwierdza telefon od pewności", "pct", 30, 99, 5),
    Field("ml.photo_conflict_conf", "Zdjęcie przeczy regułom (etui, szkło, pudełko) od pewności", "pct", 50, 99, 5,
          tip="Test na 80 prawdziwych zdjęciach: przy 80% żadne zdjęcie telefonu nie zostało uznane za akcesorium"),
]


class SettingsDialog(QDialog):
    parts_editor_requested = Signal()
    retrain_requested = Signal()

    def __init__(self, settings: Settings, parent=None, false_positives: list[tuple[str, int]] | None = None, *,
                 model_info=None, photo_model_ready: bool = False, labels: int = 0):
        super().__init__(parent)
        self.setWindowTitle("Ustawienia")
        self.resize(900, 700)
        self.settings = copy.deepcopy(settings)
        self.false_positives = false_positives or []
        self.model_info = model_info
        self.photo_model_ready = photo_model_ready
        self.labels = labels
        self._readers: list[Callable[[Settings], None]] = []

        tabs = QTabWidget()
        tabs.addTab(self._general_tab(), "Ogólne i pobieranie")
        tabs.addTab(self._profit_tab(), "Zysk i sprzedaż")
        tabs.addTab(self._buying_tab(), "Zakup i naprawa")
        tabs.addTab(self._market_tab(), "Wycena rynkowa")
        tabs.addTab(self._verdict_tab(), "Werdykt i flagi")
        tabs.addTab(self._selection_tab(), "Wybrane")
        tabs.addTab(self._filter_tab(), "Filtr ogłoszeń")
        tabs.addTab(self._safety_tab(), "Zabezpieczenia")
        tabs.addTab(self._ai_tab(), "AI lokalne")
        tabs.addTab(self._messages_tab(), "Wiadomości")
        tabs.addTab(self._notify_tab(), "Powiadomienia")

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
            elif f.kind == "pct":
                w = QDoubleSpinBox(minimum=f.minimum, maximum=f.maximum, singleStep=f.step, suffix=f.suffix or " %",
                                   decimals=f.decimals)
                w.setValue(float(value) * 100)
                self._readers.append(lambda s, w=w, f=f: _set(s, f.path, round(w.value() / 100, 4)))
            elif f.kind == "choice":
                w = QComboBox()
                for key, label in (f.choices or {}).items():
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
        scroll = QScrollArea()  # długie zakładki przewijają się zamiast ściskać pola
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setWidget(page)
        return scroll

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
        look = QGroupBox("Wygląd")
        ll = QFormLayout(look)
        self.theme_combo = QComboBox()
        for key, label in THEME_LABELS.items():
            self.theme_combo.addItem(label, key)
        self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(s.ui_theme)))
        self.theme_combo.setToolTip("Systemowy = taki jak w ustawieniach Windows")
        ll.addRow("Motyw:", self.theme_combo)
        self.font_spin = QSpinBox(minimum=8, maximum=16, suffix=" pt")
        self.font_spin.setValue(s.ui_font_pt)
        ll.addRow("Rozmiar czcionki:", self.font_spin)
        self._readers.append(lambda st: (setattr(st, "ui_theme", self.theme_combo.currentData()),
                                         setattr(st, "ui_font_pt", self.font_spin.value())))
        return self._page(loc, look, sources, form)

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

    def _selection_tab(self) -> QWidget:
        intro = QLabel(
            "Lista <b>„Wybrane”</b> (zakładka nad tabelą) zbiera oferty warte uwagi. Trafiają tam automatycznie "
            "oferty spełniające poniższe kryteria, a także oferty dodane ręcznie (★ Obserwuj / „Dodaj do "
            "Wybranych”). Ręczna decyzja ma pierwszeństwo: „Usuń z Wybranych” wyklucza ofertę na stałe, "
            "nawet jeśli spełnia kryteria. Oferta, która zniknie z portalu, zostaje na liście z oznaczeniem "
            "⌛ nieaktualna.")
        intro.setWordWrap(True)
        auto = QGroupBox("Kryteria automatyczne (wszystkie muszą być spełnione)")
        form = self._form([Field("selection.enabled", "Dodawaj oferty automatycznie", "bool")])
        self.selection_verdicts = self._criteria_form("selection", form)
        auto.setLayout(form)
        note = QLabel("Filtry i sortowanie są wspólne dla obu list; każda lista pamięta własne sortowanie.")
        note.setObjectName("muted")
        note.setWordWrap(True)
        return self._page(intro, auto, note)

    def _criteria_form(self, attr: str, form: QFormLayout) -> dict[str, QCheckBox]:
        """Werdykty + progi (``SelectionCriteria``) — wspólne dla „Wybrane” i Telegrama."""
        crit = getattr(self.settings, attr)
        verdicts = QHBoxLayout()
        boxes: dict[str, QCheckBox] = {}
        for v in Verdict:
            box = QCheckBox(v.value)
            box.setObjectName(f"{attr}.verdict.{v.name.lower()}")
            box.setChecked(v.value in crit.verdicts)
            verdicts.addWidget(box)
            boxes[v.value] = box
        verdicts.addStretch(1)
        form.addRow("Werdykt:", verdicts)
        self._readers.append(lambda s: setattr(getattr(s, attr), "verdicts", [
            v for v, box in boxes.items() if box.isChecked()]))
        self._form([
            Field(f"{attr}.min_profit", "Minimalny szacowany zysk", "float", 0, 20000, 10, " zł", tip="0 = bez progu"),
            Field(f"{attr}.min_score", "Minimalna ocena", "int", 0, 100, 5, " / 100", tip="0 = bez progu"),
            Field(f"{attr}.skip_hard_flags", "Pomijaj oferty z poważną flagą (iCloud, IMEI, podróbka)", "bool"),
        ], form)
        return boxes

    def _safety_tab(self) -> QWidget:
        s = self.settings
        intro = QLabel("Reguły, które chronią przed fałszywymi okazjami. Oferta, która nie przejdzie testu, "
                       "dostaje najwyżej werdykt <b>DO WERYFIKACJI</b> (szary) albo trafia do „Odrzucone”.")
        intro.setWordWrap(True)
        verdict = QGroupBox("Testy sensowności i limity werdyktu")
        verdict.setLayout(self._form(SANITY))
        filt = QGroupBox("Filtr tytułu")
        filt.setLayout(self._form([Field("listing_filter.multi_model_reject",
                                         "Odrzucaj tytuły z kilkoma generacjami („13 14 15”, „12/13/14”)", "bool")]))
        serial = QGroupBox("Sprzedawcy seryjni (wiele tanich „iPhone'ów” od jednej osoby)")
        serial.setLayout(self._form(SERIAL))
        country = QGroupBox("Kraj ofert (Vinted pokazuje też ogłoszenia z zagranicy)")
        country.setLayout(self._form(COUNTRY))

        portals = QGroupBox("Kategorie i minimalna cena pobierania na portalach")
        grid = QFormLayout(portals)
        self.category_edits: dict[str, tuple[QLineEdit, QLineEdit]] = {}
        self.min_price_edits: dict[str, QDoubleSpinBox] = {}
        for key, name in SOURCE_NAMES.items():
            cat = s.source_categories.get(key, {})
            cid, path = QLineEdit(str(cat.get("id", ""))), QLineEdit(str(cat.get("path", "")))
            cid.setMaximumWidth(80)
            cid.setPlaceholderText("ID")
            path.setPlaceholderText("adres kategorii (puste = wszystkie kategorie)")
            price = QDoubleSpinBox(minimum=0, maximum=5000, singleStep=10, suffix=" zł", decimals=0)
            price.setValue(float(s.source_min_price.get(key, 0) or 0))
            row = QHBoxLayout()
            row.addWidget(QLabel("ID:"))
            row.addWidget(cid)
            row.addWidget(path, 1)
            row.addWidget(QLabel("od ceny:"))
            row.addWidget(price)
            grid.addRow(name + ":", row)
            self.category_edits[key] = (cid, path)
            self.min_price_edits[key] = price
        note = QLabel("Vinted: API nie pozwala filtrować po kategorii (sprawdzone), dlatego tanie akcesoria "
                      "odcina minimalna cena pobierania. Allegro Lokalnie ma tylko wspólną kategorię "
                      "„Telefony i akcesoria”.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        grid.addRow(note)
        self._readers.append(self._read_portals)
        return self._page(intro, verdict, filt, serial, country, portals)

    def _ai_tab(self) -> QWidget:
        intro = QLabel("Darmowe AI działające na Twoim komputerze — bez płatnych usług i bez wysyłania danych. "
                       "Reguły decydują, co trafia do tabeli; AI może to potwierdzić albo podważyć. Gdy tytuł, "
                       "zdjęcie lub opis przeczy regułom albo pewność jest niska, werdykt to <b>DO WERYFIKACJI</b>.")
        intro.setWordWrap(True)
        text = QGroupBox("Klasyfikator tytułów (scikit-learn)")
        tl = QVBoxLayout(text)
        self.model_label = QLabel()
        self.model_label.setWordWrap(True)
        self.model_label.setTextFormat(Qt.TextFormat.RichText)
        tl.addWidget(self.model_label)
        row = QHBoxLayout()
        self.retrain_btn = QPushButton("🎓 Douczyć model")
        self.retrain_btn.setToolTip("Uczy klasyfikator od nowa na zbiorze startowym, odrzuconych ofertach "
                                    "i Twoich oznaczeniach (kilka sekund, w tle)")
        self.retrain_btn.clicked.connect(self.retrain_requested.emit)
        self.training_label = QLabel()
        self.training_label.setObjectName("muted")
        row.addWidget(self.retrain_btn)
        row.addWidget(self.training_label, 1)
        tl.addLayout(row)
        tl.addLayout(self._form(AI_TEXT))
        photo = QGroupBox("Analiza zdjęć (CLIP)")
        pl = QVBoxLayout(photo)
        from ..ml.photo_model import MODEL_SIZE_MB

        status = ("✔ model pobrany — działa bez internetu" if self.photo_model_ready else
                  f"model zostanie pobrany raz przy starcie programu ({MODEL_SIZE_MB} MB, z Hugging Face)")
        pinfo = QLabel(f"Stan: {status}.<br>Analizowane są tylko oferty z werdyktem KUPUJ, NEGOCJUJ lub "
                       "DO WERYFIKACJI; każda raz (wynik zapisany w bazie). Dobrze rozpoznaje etui i szkła, "
                       "słabo puste pudełka (na pudełku jest zdjęcie telefonu).")
        pinfo.setWordWrap(True)
        pinfo.setObjectName("muted")
        pl.addWidget(pinfo)
        pl.addLayout(self._form(AI_PHOTO))
        self.set_model_info(self.model_info)
        return self._page(intro, text, photo, self._llm_group())

    def _llm_group(self) -> QGroupBox:
        from ..ml.ollama import DOWNLOAD_URL, SUGGESTED_MODELS

        s = self.settings.ml
        box = QGroupBox("Analiza opisów — lokalny model językowy (Ollama, opcjonalnie)")
        lay = QVBoxLayout(box)
        info = QLabel(
            f"Darmowy program <a href='{DOWNLOAD_URL}'>Ollama</a> uruchamia model językowy na Twojej karcie "
            "graficznej — nic nie wychodzi poza komputer. Polecany model: <b>qwen3:8b</b> (ok. 5,2 GB, ok. 6 GB "
            "pamięci karty; na RTX 3060 Ti ok. 2–6 s na opis). Czytane są tylko oferty DO WERYFIKACJI, każda raz. "
            "Bez Ollamy program działa normalnie.")
        info.setWordWrap(True)
        info.setOpenExternalLinks(True)
        info.setTextFormat(Qt.TextFormat.RichText)
        lay.addWidget(info)
        lay.addLayout(self._form(AI_LLM))
        form = QFormLayout()
        self.llm_url = QLineEdit(s.llm_url)
        self.llm_url.setPlaceholderText("http://127.0.0.1:11434")
        self.llm_model = QComboBox()
        self.llm_model.setEditable(True)
        self.llm_model.addItems(list(SUGGESTED_MODELS))
        self.llm_model.setCurrentText(s.llm_model)
        self.llm_model.setToolTip("qwen3:8b — polecany; qwen2.5:7b — lżejszy. Możesz wpisać dowolny model z Ollamy.")
        form.addRow("Adres Ollamy:", self.llm_url)
        form.addRow("Model:", self.llm_model)
        lay.addLayout(form)
        row = QHBoxLayout()
        self.llm_check_btn = QPushButton("Sprawdź połączenie")
        self.llm_check_btn.clicked.connect(self._llm_check)
        self.llm_pull_btn = QPushButton("⬇ Pobierz model")
        self.llm_pull_btn.setToolTip("Pobiera wybrany model do Ollamy (raz; kilka GB). Można też w terminalu: "
                                     "ollama pull qwen3:8b")
        self.llm_pull_btn.clicked.connect(self._llm_pull)
        row.addWidget(self.llm_check_btn)
        row.addWidget(self.llm_pull_btn)
        row.addStretch(1)
        lay.addLayout(row)
        self.llm_status = QLabel("")
        self.llm_status.setWordWrap(True)
        self.llm_status.setObjectName("muted")
        lay.addWidget(self.llm_status)
        self._readers.append(lambda st: (
            setattr(st.ml, "llm_url", self.llm_url.text().strip() or "http://127.0.0.1:11434"),
            setattr(st.ml, "llm_model", self.llm_model.currentText().strip() or "qwen3:8b")))
        return box

    def _llm_check(self) -> None:
        from ..ml.ollama import OllamaClient

        url, model = self.llm_url.text().strip(), self.llm_model.currentText().strip()
        self.llm_status.setText("Sprawdzam Ollamę…")

        def check():
            with OllamaClient(url or "http://127.0.0.1:11434") as client:
                return client.status()

        def show(status) -> None:
            if not status.running:
                self.llm_status.setText(f"❌ {status.error}. Pobierz program: ollama.com/download")
            elif status.has_model(model):
                self.llm_status.setText(f"✅ Ollama {status.version} działa, model {model} jest zainstalowany.")
            else:
                have = ", ".join(status.models) or "brak"
                self.llm_status.setText(f"⚠ Ollama {status.version} działa, ale nie ma modelu {model} "
                                        f"(zainstalowane: {have}) — kliknij „Pobierz model”.")

        self._run_bg(check, show, status=self.llm_status)

    def _llm_pull(self) -> None:
        from .workers import OllamaPullWorker, start_in_thread

        if getattr(self, "_pull_worker", None) is not None:
            return
        url = self.llm_url.text().strip() or "http://127.0.0.1:11434"
        model = self.llm_model.currentText().strip() or "qwen3:8b"
        worker = OllamaPullWorker(url, model)
        worker.progress.connect(self.llm_status.setText)
        worker.finished.connect(self._llm_pulled)
        worker.failed.connect(self._llm_pull_failed)
        self._pull_worker = worker
        self.llm_pull_btn.setEnabled(False)
        self.llm_status.setText(f"⏳ Pobieranie {model}…")
        self._pull_thread = start_in_thread(worker, self.parentWidget() or self)
        self.finished.connect(self._stop_pull)

    @Slot(object)
    def _llm_pulled(self, model) -> None:
        self._pull_worker = None
        self.llm_pull_btn.setEnabled(True)
        self.llm_status.setText(f"✅ Model {model} pobrany — analiza opisów może działać.")

    @Slot(str)
    def _llm_pull_failed(self, message: str) -> None:
        self._pull_worker = None
        self.llm_pull_btn.setEnabled(True)
        self.llm_status.setText(f"❌ {message}")

    def _stop_pull(self, *_args) -> None:
        """Zamknięcie okna przerywa pobieranie (Ollama wznowi je przy kolejnym „Pobierz model”)."""
        worker, thread = getattr(self, "_pull_worker", None), getattr(self, "_pull_thread", None)
        if worker is not None:
            worker.stop()
        if thread is not None:
            thread.quit()
            thread.wait(5000)

    def set_model_info(self, info) -> None:
        """Skuteczność modelu na danych testowych (też po douczeniu w tle, gdy okno jest otwarte)."""
        self.model_info = info
        if info is None:
            self.model_label.setText("Model nie jest jeszcze wytrenowany — powstanie automatycznie przy starcie "
                                     "programu (kilka sekund).")
            return
        names = {"phone": "telefon", "accessory": "akcesorium", "part": "część", "wanted": "kupię"}
        per = " · ".join(f"{names.get(c, c)} {v['recall']:.0%}" for c, v in info.per_class.items())
        src = {"seed": "startowe", "rejected": "odrzucone przez reguły", "user": "Twoje oznaczenia",
               "hidden": "ukryte"}
        sources = ", ".join(f"{src.get(k, k)}: {v}" for k, v in info.sources.items())
        if info.hidden_skipped:
            sources += (f"; pominięte ukryte: {info.hidden_skipped} — model uznał je za telefony, więc pewnie "
                        "zostały ukryte z innego powodu")
        new = max(0, self.labels - info.labels_seen)
        self.model_label.setText(
            f"<b>Skuteczność na danych testowych: {info.accuracy:.0%}</b> ({info.n_test} tytułów odłożonych przed "
            f"treningiem) · <b>na prawdziwych tytułach z portali: {info.benchmark_accuracy:.0%}</b> "
            f"({info.benchmark_n}, model ich nie widział)<br>Trafność wg klasy: {per}<br>"
            f"Dane: {info.n_train} przykładów ({sources}) · wytrenowano {info.trained_at[:16].replace('T', ' ')} "
            f"UTC · nowych oznaczeń od treningu: {new}")
        self.training_label.setText("")

    def set_training_state(self, text: str) -> None:
        self.training_label.setText(text)

    def _read_portals(self, s: Settings) -> None:
        s.source_categories = {k: {"id": cid.text().strip(), "path": path.text().strip().strip("/")}
                               for k, (cid, path) in self.category_edits.items()}
        s.source_min_price = {k: float(w.value()) for k, w in self.min_price_edits.items() if w.value() > 0}

    _FILTER_LISTS = (
        ("accessory_words", "Akcesoria (odrzucane, gdy są przedmiotem sprzedaży)"),
        ("part_words", "Części zamienne (odrzucane, gdy sprzedawana jest sama część)"),
        ("wanted_words", "Ogłoszenia kupna / zamiany"),
        ("addon_markers", "Słowa oznaczające dodatek do telefonu (+ etui gratis, z pudełkiem)"),
        ("single_part_markers", "Słowa oznaczające samą część („sam wyświetlacz”)"),
        ("accessory_category_words", "Kategorie portalu z akcesoriami/częściami"),
    )

    def _filter_tab(self) -> QWidget:
        cfg = self.settings.listing_filter
        info = QLabel("Każda pozycja w osobnej linii (wielkość liter i polskie znaki nie mają znaczenia). "
                      "Akcesorium w tytule razem z telefonem („iPhone 13 + etui gratis”) nie powoduje odrzucenia — "
                      "filtr ocenia kontekst. Odrzucone ogłoszenia z powodem: przycisk „Odrzucone” w oknie głównym.")
        info.setWordWrap(True)
        grid = QWidget()
        glay = QHBoxLayout(grid)
        glay.setContentsMargins(0, 0, 0, 0)
        self.filter_edits: dict[str, QPlainTextEdit] = {}
        columns = [QVBoxLayout(), QVBoxLayout()]
        for i, (attr, label) in enumerate(self._FILTER_LISTS):
            box = QGroupBox(label)
            edit = QPlainTextEdit("\n".join(getattr(cfg, attr)))
            edit.setMinimumHeight(110)
            QVBoxLayout(box).addWidget(edit)
            columns[i % 2].addWidget(box)
            self.filter_edits[attr] = edit
        for col in columns:
            glay.addLayout(col)
        form = self._form([Field("listing_filter.suspicious_price_ratio",
                                 "Sprawdź dokładniej, gdy cena poniżej × mediany rynkowej", "float", 0.01, 0.9,
                                 0.01, "", 2)])
        self._readers.append(self._read_filter_lists)
        widgets: list = [info, grid, form]
        if self.false_positives:
            fp = ", ".join(f"„{k}” ×{n}" for k, n in self.false_positives[:10])
            hint = QLabel(f"Niesłuszne odrzucenia (przywrócone „To jest telefon”) według słowa: {fp}. "
                          "Jeśli któreś słowo często się myli, rozważ usunięcie go z listy.")
            hint.setWordWrap(True)
            hint.setStyleSheet("color: #e67700;")
            widgets.insert(1, hint)
        return self._page(*widgets)

    def _read_filter_lists(self, s: Settings) -> None:
        for attr, edit in self.filter_edits.items():
            words = [w.strip() for w in edit.toPlainText().splitlines() if w.strip()]
            setattr(s.listing_filter, attr, list(dict.fromkeys(words)))

    def _messages_tab(self) -> QWidget:
        from ..core.messages import DEFAULT_TEMPLATES, PLACEHOLDERS, TEMPLATE_KEYS, TEMPLATE_NAMES

        legend = "<br>".join(f"<b>{{{k}}}</b> — {v}" for k, v in PLACEHOLDERS.items())
        info = QLabel("Szablony wiadomości do sprzedającego (bez AI, za darmo). W panelu szczegółów widać gotowy "
                      "tekst z danymi oferty — możesz go poprawić, a <b>„📋 Skopiuj wiadomość”</b> kopiuje go do "
                      "schowka; wklejasz go w portalu. Domyślnie wybierany jest szablon pasujący do werdyktu "
                      "(NEGOCJUJ — Twój styl negocjacji); na liście nad tekstem możesz wybrać inny. Argumenty "
                      "są tylko prawdziwe — z ogłoszenia i z wyceny.<br><br>"
                      f"Pola do wstawienia:<br>{legend}")
        info.setWordWrap(True)
        info.setTextFormat(Qt.TextFormat.RichText)
        self.template_edits: dict[str, QPlainTextEdit] = {}
        boxes = []
        for key in TEMPLATE_KEYS:
            box = QGroupBox(TEMPLATE_NAMES[key])
            lay = QVBoxLayout(box)
            edit = QPlainTextEdit(self.settings.message_templates.get(key) or DEFAULT_TEMPLATES[key])
            edit.setObjectName(f"template_{key}")
            edit.setMinimumHeight(110)
            lay.addWidget(edit)
            self.template_edits[key] = edit
            boxes.append(box)
        from ..core.messages import STYLE_NAMES

        style = QGroupBox("Negocjacja (NEGOCJUJ)")
        style.setLayout(self._form([
            Field("negotiation_style", "Domyślny styl wiadomości", "choice", choices=STYLE_NAMES,
                  tip="Uprzejmy — grzecznie i z argumentami; konkretny — krótko, z propozycją ceny; "
                      "szybki odbiór — najpierw szybki odbiór i gotówka"),
            Field("pickup_radius_km", "Proponuj odbiór osobisty i gotówkę do", "int", 0, 500, 5, " km",
                  tip="Dalej (albo gdy odległość nieznana) wiadomość proponuje szybką płatność i wysyłkę. "
                      "0 = nigdy nie proponuj odbioru."),
        ]))
        reset = QPushButton("Przywróć domyślne szablony")
        reset.clicked.connect(lambda: [e.setPlainText(DEFAULT_TEMPLATES[k]) for k, e in self.template_edits.items()])
        self._readers.append(lambda st: setattr(st, "message_templates", {
            k: (e.toPlainText().strip() or DEFAULT_TEMPLATES[k]) for k, e in self.template_edits.items()}))
        return self._page(info, style, *boxes, reset)

    def _notify_tab(self) -> QWidget:
        s = self.settings
        general = QGroupBox("Działanie w tle i powiadomienia")
        general.setLayout(self._form([
            Field("minimize_to_tray", "Zamknięcie okna chowa do zasobnika", "bool"),
            Field("notify_desktop", "Powiadomienia Windows o nowych zielonych ofertach", "bool"),
            Field("notify_price_drops", "Powiadamiaj też o obniżce ceny do zielonej", "bool"),
            Field("notify_max_per_scan", "Maks. powiadomień Windows na odświeżenie", "int", 1, 50),
        ]))

        from ..core.secret_store import is_encrypted
        from ..services.telegram_queue import QUIET_MODES

        howto = QLabel(
            "<b>Jak połączyć Telegram (raz, ok. 2 minut):</b><ol>"
            "<li>W Telegramie wyszukaj <b>@BotFather</b> (niebieski znaczek weryfikacji) i napisz <code>/newbot</code>.</li>"
            "<li>Podaj nazwę bota (np. <i>Mój PhoneBot</i>) i jego login kończący się na <i>bot</i> "
            "(np. <i>kacwin_phone_bot</i>).</li>"
            "<li>BotFather odpisze <b>tokenem</b> w rodzaju <code>123456789:AAH…</code> — skopiuj go do pola "
            "„Token bota” poniżej. Nikomu go nie pokazuj.</li>"
            "<li>Otwórz rozmowę ze swoim nowym botem (link w wiadomości od BotFather) i wyślij mu <code>/start</code>.</li>"
            "<li>Kliknij <b>„Pobierz chat ID”</b> — program odczyta numer Twojej rozmowy z botem.</li>"
            "<li>Kliknij <b>„Wyślij test”</b>, zaznacz „Wysyłaj powiadomienia na Telegram” i zapisz.</li></ol>")
        howto.setWordWrap(True)
        howto.setTextFormat(Qt.TextFormat.RichText)

        tg = QGroupBox("Telegram — połączenie")
        tg_form = self._form([Field("telegram_enabled", "Wysyłaj powiadomienia na Telegram", "bool")])
        self.tg_token = QLineEdit(s.telegram_bot_token)
        self.tg_token.setObjectName("telegram_token")
        self.tg_token.setEchoMode(QLineEdit.EchoMode.Password)
        self.tg_token.setPlaceholderText("token od @BotFather, np. 123456:ABC…")
        self.tg_chat = QLineEdit(s.telegram_chat_id)
        self.tg_chat.setObjectName("telegram_chat")
        self.tg_chat.setPlaceholderText("np. 123456789")
        find_btn, test_btn = QPushButton("Pobierz chat ID"), QPushButton("Wyślij test")
        find_btn.clicked.connect(self._telegram_find_chat)
        test_btn.clicked.connect(self._telegram_test)
        self.tg_status = QLabel("")
        self.tg_status.setWordWrap(True)
        self.tg_status.setObjectName("muted")
        row = QHBoxLayout()
        row.addWidget(self.tg_chat, 1)
        row.addWidget(find_btn)
        row.addWidget(test_btn)
        tg_form.addRow("Token bota:", self.tg_token)
        tg_form.addRow("Chat ID:", row)
        tg_form.addRow(self.tg_status)
        tg.setLayout(tg_form)
        self._readers.append(lambda st: (setattr(st, "telegram_bot_token", self.tg_token.text().strip()),
                                         setattr(st, "telegram_chat_id", self.tg_chat.text().strip())))

        what = QGroupBox("Kiedy wysyłać (oferta musi być w „Wybrane” i spełnić te kryteria)")
        what_form = QFormLayout()
        self.telegram_verdicts = self._criteria_form("telegram_criteria", what_form)
        self._form([
            Field("telegram_price_drops", "Obniżka ceny oferty z „Wybrane”", "bool"),
            Field("telegram_photos", "Miniatura zdjęcia w wiadomości", "bool"),
            Field("telegram_max_per_hour", "Najwyżej wiadomości na godzinę", "int", 1, 60,
                  tip="Nadmiar trafia do jednej wiadomości z podsumowaniem"),
        ], what_form)
        what.setLayout(what_form)
        quiet = QGroupBox("Cisza nocna")
        quiet.setLayout(self._form([
            Field("telegram_quiet_enabled", "Nie wysyłaj w nocy", "bool"),
            Field("telegram_quiet_start", "Od godziny", "int", 0, 23, 1, ":00"),
            Field("telegram_quiet_end", "Do godziny", "int", 0, 23, 1, ":00"),
            Field("telegram_quiet_mode", "Oferty z nocy", "choice", choices=QUIET_MODES),
        ]))
        info = ("Każda oferta jest zgłaszana tylko raz (zapisane w bazie), nigdy oferty sprzed pierwszego włączenia "
                "powiadomień. Wiadomości, których nie udało się wysłać (brak internetu), są ponawiane w tle.")
        secure = ("Token i chat ID są zapisywane osobno, zaszyfrowane kontem Windows (DPAPI) — nie w kodzie ani "
                  "w zwykłych ustawieniach." if is_encrypted() else
                  "Token i chat ID są zapisywane osobno, poza zwykłymi ustawieniami (szyfrowanie DPAPI działa "
                  "w wersji dla Windows).")
        note = QLabel(f"{info}<br>{secure}")
        note.setWordWrap(True)
        note.setObjectName("muted")
        return self._page(general, howto, tg, what, quiet, note)

    def _run_bg(self, func, on_ok, status: QLabel | None = None) -> None:
        """Zadanie w tle; wynik trafia do ``on_ok`` w wątku okna (metody okna, nie lambdy — patrz niżej)."""
        from .workers import FuncWorker, start_in_thread

        worker = FuncWorker(func)
        self._bg_ok, self._bg_status = on_ok, status or self.tg_status
        # sygnały z wątku roboczego do metod tego okna: Qt wywoła je w wątku okna (lambda — w wątku roboczym)
        worker.finished.connect(self._bg_finished)
        worker.failed.connect(self._bg_failed)
        self._bg_worker = worker  # referencja chroni przed GC
        # wątek należy do okna głównego: zamknięcie ustawień w trakcie zadania nie niszczy działającego wątku
        start_in_thread(worker, self.parentWidget() or self)

    @Slot(object)
    def _bg_finished(self, result) -> None:
        self._bg_ok(result)

    @Slot(str)
    def _bg_failed(self, message: str) -> None:
        self._bg_status.setText(f"❌ {message}")

    def _telegram_find_chat(self) -> None:
        from ..services.notifications import TelegramClient

        token = self.tg_token.text()
        self.tg_status.setText("Sprawdzam wiadomości bota…")

        def found(chat_id: str) -> None:
            self.tg_chat.setText(chat_id)
            self.tg_status.setText(f"✅ Znaleziono chat ID: {chat_id}")

        self._run_bg(lambda: TelegramClient(token).find_chat_id(), found)

    def _telegram_test(self) -> None:
        from ..services.notifications import TelegramClient

        token, chat = self.tg_token.text(), self.tg_chat.text()
        self.tg_status.setText("Wysyłam wiadomość testową…")
        self._run_bg(lambda: TelegramClient(token, chat).send("✅ PhoneBot: powiadomienia działają."),
                     lambda _r: self.tg_status.setText("✅ Wysłano — sprawdź Telegram."))

    # ---------------------------------------------------------------- wynik ---

    def result_settings(self) -> Settings:
        result = copy.deepcopy(self.settings)
        for read in self._readers:
            read(result)
        if result.score_yellow > result.score_green:
            result.score_yellow = result.score_green
        return result
