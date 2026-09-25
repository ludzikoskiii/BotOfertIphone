"""Wybór Twojej miejscowości: wyszukiwarka OSM, lista miejscowości lub współrzędne."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)

from ..core.places import PLACES, Place
from ..net.geocode import GeocodeError, geocode
from .workers import start_in_thread


class GeocodeWorker(QObject):
    finished = Signal(object)  # list[Place]
    failed = Signal(str)

    def __init__(self, query: str):
        super().__init__()
        self.query = query

    @Slot()
    def run(self) -> None:
        try:
            self.finished.emit(geocode(self.query))
        except GeocodeError as e:
            self.failed.emit(str(e))


class LocationDialog(QDialog):
    def __init__(self, name: str, lat: float, lon: float, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Twoja lokalizacja")
        self.resize(560, 520)
        self._thread: QThread | None = None
        self._worker: GeocodeWorker | None = None

        info = QLabel("Od tej miejscowości liczona jest odległość do ofert, koszt dojazdu "
                      "przy odbiorze osobistym i filtr promienia.")
        info.setWordWrap(True)

        # --- wyszukiwarka ---
        search_box = QGroupBox("Wyszukaj miejscowość (OpenStreetMap)")
        self.query = QLineEdit()
        self.query.setPlaceholderText("np. Kacwin, Jurgów, Nowy Targ…")
        self.query.returnPressed.connect(self._search)
        self.search_btn = QPushButton("Szukaj")
        self.search_btn.clicked.connect(self._search)
        self.results = QListWidget()
        self.results.itemClicked.connect(self._pick_item)
        self.results.itemDoubleClicked.connect(lambda it: (self._pick_item(it), self.accept()))
        self.search_status = QLabel()
        self.search_status.setStyleSheet("color: #868e96;")
        row = QHBoxLayout()
        row.addWidget(self.query, 1)
        row.addWidget(self.search_btn)
        sl = QVBoxLayout(search_box)
        sl.addLayout(row)
        sl.addWidget(self.results)
        sl.addWidget(self.search_status)

        # --- lista wbudowana ---
        self.quick = QComboBox()
        self.quick.addItem("— wybierz z listy —", None)
        for p in PLACES:
            self.quick.addItem(f"{p.name} ({p.description})", p)
        self.quick.currentIndexChanged.connect(self._pick_quick)

        # --- pola wynikowe ---
        self.name_edit = QLineEdit(name)
        self.lat = QDoubleSpinBox(decimals=4, minimum=49.0, maximum=55.0, singleStep=0.01)
        self.lon = QDoubleSpinBox(decimals=4, minimum=14.0, maximum=24.2, singleStep=0.01)
        self.lat.setValue(lat)
        self.lon.setValue(lon)
        form = QFormLayout()
        form.addRow("Szybki wybór:", self.quick)
        form.addRow("Nazwa miejscowości:", self.name_edit)
        form.addRow("Szerokość geogr.:", self.lat)
        form.addRow("Długość geogr.:", self.lon)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Anuluj")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(search_box, 1)
        layout.addLayout(form)
        layout.addWidget(buttons)

    # --- wynik ---

    def place(self) -> Place:
        return Place(self.name_edit.text().strip() or "Moja lokalizacja", self.lat.value(), self.lon.value())

    def set_place(self, p: Place) -> None:
        self.name_edit.setText(p.name)
        self.lat.setValue(p.lat)
        self.lon.setValue(p.lon)

    def _pick_quick(self) -> None:
        p = self.quick.currentData()
        if isinstance(p, Place):
            self.set_place(p)

    def _pick_item(self, item: QListWidgetItem) -> None:
        self.set_place(item.data(Qt.ItemDataRole.UserRole))

    # --- wyszukiwanie w tle ---

    def _search(self) -> None:
        q = self.query.text().strip()
        if not q or self._thread is not None:
            return
        self.search_btn.setEnabled(False)
        self.search_status.setText("Szukam…")
        worker = GeocodeWorker(q)
        worker.finished.connect(self.show_results)
        worker.failed.connect(self._failed)
        self._worker = worker
        self._thread = start_in_thread(worker, self)
        self._thread.finished.connect(self._done)

    def show_results(self, places: list[Place]) -> None:
        self.results.clear()
        for p in places:
            item = QListWidgetItem(f"{p.name} — {p.description}")
            item.setData(Qt.ItemDataRole.UserRole, p)
            self.results.addItem(item)
        self.search_status.setText(f"Znaleziono: {len(places)}. Kliknij wynik, aby go wybrać."
                                   if places else "Nic nie znaleziono — spróbuj innej nazwy.")

    def _failed(self, message: str) -> None:
        self.search_status.setText(message + " Wybierz z listy albo wpisz współrzędne ręcznie.")

    def _done(self) -> None:
        self._thread = None
        self._worker = None
        self.search_btn.setEnabled(True)
