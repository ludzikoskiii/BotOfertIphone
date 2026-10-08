"""Profile powiadomień Telegram w programie: lista (włącz / wyłącz, statystyka 7 dni) i okno edycji z podglądem
„z ostatnich 24 godzin ten profil wysłałby X powiadomień” (liczonym w tle) i testowym powiadomieniem."""
from __future__ import annotations

import copy
import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from PySide6.QtCore import Qt, QTimer, Signal, Slot
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ..core.catalog import ALL_STORAGES, format_storage, generations
from ..core.models import Condition
from ..core.notify_profiles import COUNTRY, MODES, NOTIFY_VERDICTS, RISK_LABELS, SHIPPING, NotifyProfile, describe
from ..sources import SOURCE_NAMES
from ..storage.repositories import NotifyProfileRepository
from .workers import FuncWorker, start_in_thread


def _thread_owner():
    """Wątki podglądu i testu należą do aplikacji: zamknięcie okna w trakcie liczenia nie niszczy wątku
    (wynik do zamkniętego okna Qt po prostu nie trafi)."""
    from PySide6.QtCore import QCoreApplication

    return QCoreApplication.instance()


def _zl(v: float | None) -> str:
    return "—" if v is None else f"{v:,.0f} zł".replace(",", " ")


def _checks(options: dict[str, str], selected: list, columns: int = 3) -> tuple[QWidget, dict]:
    box, grid, boxes = QWidget(), QGridLayout(), {}
    grid.setContentsMargins(0, 0, 0, 0)
    for i, (key, label) in enumerate(options.items()):
        cb = QCheckBox(label)
        cb.setChecked(key in selected)
        grid.addWidget(cb, i // columns, i % columns)
        boxes[key] = cb
    box.setLayout(grid)
    return box, boxes


class ModelTree(QTreeWidget):
    """Modele pogrupowane w generacje — zaznaczenie generacji zaznacza wszystkie jej modele."""

    def __init__(self, selected: list[str], parent=None):
        super().__init__(parent)
        self.setHeaderHidden(True)
        self.setMinimumHeight(220)
        for gen, models in generations().items():
            label = {"X": "iPhone X / XR / XS", "SE": "iPhone SE"}.get(gen, f"iPhone {gen}")
            top = QTreeWidgetItem([f"{label} — cała generacja"])
            top.setFlags(top.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsAutoTristate)
            for m in models:
                child = QTreeWidgetItem([m])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked if m in selected else Qt.CheckState.Unchecked)
                top.addChild(child)
            self.addTopLevelItem(top)

    def generation_item(self, gen_label_start: str) -> QTreeWidgetItem | None:
        for i in range(self.topLevelItemCount()):
            item = self.topLevelItem(i)
            if item.text(0).startswith(gen_label_start):
                return item
        return None

    def selected(self) -> list[str]:
        out = []
        for i in range(self.topLevelItemCount()):
            top = self.topLevelItem(i)
            for j in range(top.childCount()):
                if top.child(j).checkState(0) == Qt.CheckState.Checked:
                    out.append(top.child(j).text(0))
        return out


class ProfileDialog(QDialog):
    """Edycja profilu powiadomień z podglądem na żywo (ostatnie 24 h) i przyciskiem testu."""

    def __init__(self, profile: NotifyProfile, *, settings, db_path=None, make_test: Callable | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Profil powiadomień — {profile.name}")
        self.resize(1080, 820)
        self.profile = copy.deepcopy(profile)
        self.settings, self.db_path, self._make_test = settings, db_path, make_test
        self._worker = None
        self._test_worker = None
        self._pending = False
        self.preview_result = None
        p = self.profile

        # --- podstawowe ---
        self.name = QLineEdit(p.name)
        self.enabled = QCheckBox("Profil włączony")
        self.enabled.setChecked(p.enabled)
        self.mode = QComboBox()
        for key, label in MODES.items():
            self.mode.addItem(label, key)
        self.mode.setCurrentIndex(max(0, self.mode.findData(p.mode)))
        verdict_box, self.verdicts = _checks({v: v for v in NOTIFY_VERDICTS}, p.verdicts)
        self.price_min = QDoubleSpinBox(maximum=50000, singleStep=50, suffix=" zł", decimals=0)
        self.price_min.setSpecialValueText("bez limitu")
        self.price_min.setValue(p.price_min)
        self.price_max = QDoubleSpinBox(maximum=50000, singleStep=50, suffix=" zł", decimals=0)
        self.price_max.setSpecialValueText("bez limitu")
        self.price_max.setValue(p.price_max)
        self.min_profit = QDoubleSpinBox(maximum=20000, singleStep=10, suffix=" zł", decimals=0)
        self.min_profit.setSpecialValueText("bez progu")
        self.min_profit.setValue(p.min_profit)
        self.min_rate = QDoubleSpinBox(maximum=2000, singleStep=5, suffix=" zł/h", decimals=0)
        self.min_rate.setSpecialValueText("bez progu")
        self.min_rate.setValue(p.min_profit_per_hour)
        self.min_score = QSpinBox(maximum=100, singleStep=5, suffix=" / 100")
        self.min_score.setSpecialValueText("bez progu")
        self.min_score.setValue(p.min_score)
        basic = QFormLayout()
        basic.addRow("Nazwa:", self.name)
        basic.addRow("", self.enabled)
        basic.addRow("Tryb wyceny:", self.mode)
        basic.addRow("Werdykty (puste = wszystkie):", verdict_box)
        price = QHBoxLayout()
        price.addWidget(self.price_min)
        price.addWidget(QLabel("–"))
        price.addWidget(self.price_max)
        basic.addRow("Cena:", price)
        basic.addRow("Min. szacowany zysk:", self.min_profit)
        basic.addRow("Min. zysk na godzinę:", self.min_rate)
        basic.addRow("Min. ocena opłacalności:", self.min_score)
        basic_box = QGroupBox("Podstawowe")
        basic_box.setLayout(basic)

        # --- telefon ---
        self.models = ModelTree(p.models)
        storages = {str(s): format_storage(s) for s in ALL_STORAGES if s >= 32}
        storage_box, self.storages = _checks(storages, [str(s) for s in p.storages], 7)
        cond_box, self.conditions = _checks({c.value: c.label for c in Condition}, p.conditions, 3)
        phone = QFormLayout()
        phone.addRow(QLabel("Modele (puste = wszystkie). Zaznacz generację, aby wybrać wszystkie jej modele:"))
        phone.addRow(self.models)
        phone.addRow("Pamięć (puste = każda):", storage_box)
        phone.addRow("Stan (puste = każdy):", cond_box)
        phone_box = QGroupBox("Telefon")
        phone_box.setLayout(phone)

        # --- skąd ---
        src_box, self.sources = _checks(dict(SOURCE_NAMES), p.sources, 3)
        self.radius = QSpinBox(maximum=1000, singleStep=10, suffix=" km")
        self.radius.setSpecialValueText("bez limitu")
        self.radius.setValue(p.radius_km)
        self.keep_shipping = QCheckBox("dalsze oferty z wysyłką też")
        self.keep_shipping.setChecked(p.radius_keeps_shipping)
        self.shipping = QComboBox()
        for key, label in SHIPPING.items():
            self.shipping.addItem(label, key)
        self.shipping.setCurrentIndex(max(0, self.shipping.findData(p.shipping)))
        self.country = QComboBox()
        for key, label in COUNTRY.items():
            self.country.addItem(label, key)
        self.country.setCurrentIndex(max(0, self.country.findData(p.country)))
        where = QFormLayout()
        where.addRow("Portale (puste = wszystkie):", src_box)
        radius = QHBoxLayout()
        radius.addWidget(self.radius)
        radius.addWidget(self.keep_shipping)
        where.addRow("Promień od Twojej miejscowości:", radius)
        where.addRow("Wysyłka:", self.shipping)
        where.addRow("Kraj:", self.country)
        where_box = QGroupBox("Skąd")
        where_box.setLayout(where)

        # --- bezpieczeństwo i inne ---
        self.max_risk = QComboBox()
        for key, label in RISK_LABELS.items():
            self.max_risk.addItem(f"ryzyko oszustwa: {label}", key)
        self.max_risk.setCurrentIndex(max(0, self.max_risk.findData(p.max_risk)))
        self.hard_flags = QCheckBox("pomijaj oferty z poważną flagą (iCloud, IMEI, podróbka)")
        self.hard_flags.setChecked(p.skip_hard_flags)
        self.parts = QCheckBox("tylko oferty, do których mam część w magazynie")
        self.parts.setChecked(p.only_with_parts)
        self.picked = QCheckBox("tylko oferty, które automatycznie trafiają do „Wybrane”")
        self.picked.setChecked(p.require_picked)
        self.words = QLineEdit(", ".join(p.exclude_words))
        self.words.setPlaceholderText("np. icloud, etui, atrapa (w tytule lub opisie)")
        other = QFormLayout()
        other.addRow("Maks. ryzyko:", self.max_risk)
        other.addRow(self.hard_flags)
        other.addRow(self.parts)
        other.addRow(self.picked)
        other.addRow("Wykluczone słowa:", self.words)
        other_box = QGroupBox("Bezpieczeństwo i inne")
        other_box.setLayout(other)

        # --- cisza i limit ---
        self.own_quiet = QCheckBox("własna cisza nocna (inaczej — globalna z ustawień)")
        self.own_quiet.setChecked(p.own_quiet)
        self.quiet_on = QCheckBox("nie wysyłaj w godzinach")
        self.quiet_on.setChecked(p.quiet_enabled)
        self.quiet_start = QSpinBox(maximum=23, suffix=":00")
        self.quiet_start.setValue(p.quiet_start)
        self.quiet_end = QSpinBox(maximum=23, suffix=":00")
        self.quiet_end.setValue(p.quiet_end)
        self.per_hour = QSpinBox(maximum=60)
        self.per_hour.setSpecialValueText("tylko globalny")
        self.per_hour.setValue(p.max_per_hour)
        timing = QFormLayout()
        timing.addRow(self.own_quiet)
        hours = QHBoxLayout()
        hours.addWidget(self.quiet_on)
        hours.addWidget(self.quiet_start)
        hours.addWidget(QLabel("–"))
        hours.addWidget(self.quiet_end)
        hours.addStretch(1)
        timing.addRow(hours)
        timing.addRow("Limit wiadomości na godzinę:", self.per_hour)
        timing_box = QGroupBox("Cisza nocna i limit (opcjonalnie dla tego profilu)")
        timing_box.setLayout(timing)

        form = QWidget()
        col = QVBoxLayout(form)
        for box in (basic_box, phone_box, where_box, other_box, timing_box):
            col.addWidget(box)
        col.addStretch(1)
        scroll = QScrollArea()  # jedna kolumna grup — przewijana w pionie, bez przewijania w poziomie
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        scroll.setWidget(form)
        scroll.setMinimumWidth(640)

        # --- podgląd ---
        self.preview_label = QLabel("Podgląd liczy się…")
        self.preview_label.setObjectName("preview_summary")
        self.preview_label.setWordWrap(True)
        self.preview_list = QListWidget()
        self.preview_list.setObjectName("preview_list")
        self.preview_list.setWordWrap(True)
        self.preview_list.setAlternatingRowColors(True)
        self.preview_list.setSpacing(2)
        self.preview_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.test_btn = QPushButton("📨 Wyślij testowe powiadomienie z tego profilu")
        self.test_btn.clicked.connect(self._test)
        self.test_status = QLabel()
        self.test_status.setWordWrap(True)
        self.test_status.setObjectName("muted")
        side = QVBoxLayout()
        title = QLabel("<b>Podgląd: ostatnie 24 godziny</b>")
        side.addWidget(title)
        side.addWidget(self.preview_label)
        side.addWidget(self.preview_list, 1)
        side.addWidget(self.test_btn)
        side.addWidget(self.test_status)
        side_w = QWidget()
        side_w.setLayout(side)
        side_w.setMinimumWidth(360)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("Zapisz")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("Anuluj")
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        body = QHBoxLayout()
        body.addWidget(scroll, 2)
        body.addWidget(side_w, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(body, 1)
        lay.addWidget(buttons)

        # podgląd po każdej zmianie (z opóźnieniem — liczony w tle)
        self._timer = QTimer(self, singleShot=True, interval=700)
        self._timer.timeout.connect(self.refresh_preview)
        for w in (self.price_min, self.price_max, self.min_profit, self.min_rate, self.min_score, self.radius,
                  self.quiet_start, self.quiet_end, self.per_hour):
            w.valueChanged.connect(lambda *_: self._timer.start())
        for w in (self.mode, self.shipping, self.country, self.max_risk):
            w.currentIndexChanged.connect(lambda *_: self._timer.start())
        for w in (self.keep_shipping, self.hard_flags, self.parts, self.picked, *self.verdicts.values(),
                  *self.storages.values(), *self.conditions.values(), *self.sources.values()):
            w.toggled.connect(lambda *_: self._timer.start())
        self.words.textChanged.connect(lambda *_: self._timer.start())
        self.models.itemChanged.connect(lambda *_: self._timer.start())
        if db_path is not None:
            self.refresh_preview()
        else:
            self.preview_label.setText("Podgląd niedostępny.")

    # --- odczyt ---

    def read(self) -> NotifyProfile:
        p = self.profile
        p.name = self.name.text().strip() or "Profil"
        p.enabled = self.enabled.isChecked()
        p.mode = self.mode.currentData()
        p.verdicts = [v for v, b in self.verdicts.items() if b.isChecked()]
        p.price_min, p.price_max = self.price_min.value(), self.price_max.value()
        p.min_profit, p.min_profit_per_hour = self.min_profit.value(), self.min_rate.value()
        p.min_score = self.min_score.value()
        p.models = self.models.selected()
        p.storages = sorted(int(k) for k, b in self.storages.items() if b.isChecked())
        p.conditions = [k for k, b in self.conditions.items() if b.isChecked()]
        p.sources = [k for k, b in self.sources.items() if b.isChecked()]
        p.radius_km, p.radius_keeps_shipping = self.radius.value(), self.keep_shipping.isChecked()
        p.shipping, p.country = self.shipping.currentData(), self.country.currentData()
        p.only_with_parts, p.max_risk = self.parts.isChecked(), self.max_risk.currentData()
        p.skip_hard_flags, p.require_picked = self.hard_flags.isChecked(), self.picked.isChecked()
        p.exclude_words = [w.strip() for w in self.words.text().split(",") if w.strip()]
        p.own_quiet, p.quiet_enabled = self.own_quiet.isChecked(), self.quiet_on.isChecked()
        p.quiet_start, p.quiet_end = self.quiet_start.value(), self.quiet_end.value()
        p.max_per_hour = self.per_hour.value()
        return p

    # --- podgląd ---

    def refresh_preview(self) -> None:
        from ..services.notify_service import preview_in_background

        if self.db_path is None:
            return
        if self._worker is not None:
            self._pending = True  # policz jeszcze raz po zakończeniu bieżącego
            return
        worker = FuncWorker(preview_in_background, self.db_path, copy.deepcopy(self.settings),
                            copy.deepcopy(self.read()))
        worker.finished.connect(self._preview_done)
        worker.failed.connect(self._preview_failed)
        self._worker = worker
        self.preview_label.setText("Podgląd liczy się…")
        start_in_thread(worker, _thread_owner())

    @Slot(object)
    def _preview_done(self, pv) -> None:
        self._worker = None
        self.preview_result = pv
        self.preview_label.setText(pv.summary())
        self.preview_list.clear()
        for offer, val in pv.matched[:30]:
            p = offer.parsed
            storage = f" {format_storage(p.storage_gb)}" if p.storage_gb else ""
            place = offer.raw.city or ""
            if offer.distance_km is not None:
                place += f" ({offer.distance_km:.0f} km)"
            self.preview_list.addItem(f"{p.model or 'iPhone'}{storage} — {_zl(offer.price)} · zysk "
                                      f"{_zl(val.expected_profit)} · {val.verdict.value}\n"
                                      f"{SOURCE_NAMES.get(offer.raw.source, offer.raw.source)} · {place}")
        if pv.count > 30:
            self.preview_list.addItem(f"…i jeszcze {pv.count - 30}")
        if self._pending:
            self._pending = False
            self.refresh_preview()

    @Slot(str)
    def _preview_failed(self, error: str) -> None:
        self._worker = None
        self.preview_label.setText(f"Podgląd: błąd — {error}")

    def wait_preview(self, timeout_s: float = 10.0) -> None:
        """Testy: czeka na policzenie podglądu."""
        import time

        from PySide6.QtWidgets import QApplication

        deadline = time.monotonic() + timeout_s
        while (self._worker is not None or self._timer.isActive()) and time.monotonic() < deadline:
            QApplication.processEvents()
            time.sleep(0.01)
        QApplication.processEvents()

    def _test(self) -> None:
        if self._make_test is None or self._test_worker is not None:
            return
        worker = FuncWorker(self._make_test(copy.deepcopy(self.read())))
        worker.finished.connect(self._test_done)
        worker.failed.connect(self._test_failed)
        self._test_worker = worker
        self.test_btn.setEnabled(False)
        self.test_status.setText("Wysyłanie…")
        start_in_thread(worker, _thread_owner())

    # wyniki z wątku roboczego — metody okna (Qt wywoła je w wątku okna; lambdy — w wątku roboczym)
    @Slot(object)
    def _test_done(self, text: str) -> None:
        self._test_worker = None
        self.test_btn.setEnabled(True)
        self.test_status.setText(f"✅ {text}")

    @Slot(str)
    def _test_failed(self, error: str) -> None:
        self._test_worker = None
        self.test_btn.setEnabled(True)
        self.test_status.setText(f"❌ {error}")

    def _accept(self) -> None:
        if not self.name.text().strip():
            QMessageBox.warning(self, "Profil", "Podaj nazwę profilu.")
            return
        self.read()
        self.accept()


class ProfilesPanel(QGroupBox):
    """Lista profili w ustawieniach: włącznik, nazwa, wysłane w 7 dni, opis filtrów; zmiany od razu w bazie."""

    changed = Signal()

    def __init__(self, conn: sqlite3.Connection, get_settings: Callable, *, db_path=None,
                 make_test: Callable | None = None, parent=None):
        super().__init__("Profile powiadomień (filtry niezależne od tabeli w programie)", parent)
        self.conn, self._get_settings, self.db_path, self._make_test = conn, get_settings, db_path, make_test
        self._test_worker = None
        self.repo = NotifyProfileRepository(conn)
        self.repo.ensure_default(get_settings())
        self.table = QTableWidget(0, 4)
        self.table.setObjectName("profiles_table")
        self.table.setHorizontalHeaderLabels(["Wł.", "Nazwa", "Wysłane (7 dni)", "Filtry"])
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(150)
        self.table.doubleClicked.connect(lambda _i: self.edit())
        self.table.itemChanged.connect(self._toggled)
        bar = QHBoxLayout()
        for text, slot in (("＋ Dodaj", self.add), ("Edytuj", self.edit), ("Duplikuj", self.duplicate),
                           ("Usuń", self.delete), ("📨 Test", self.test)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            bar.addWidget(b)
        bar.addStretch(1)
        note = QLabel("Oferta pasująca do kilku profili przychodzi raz — z nazwami profili. Profil może mieć własną "
                      "ciszę nocną i limit. W Telegramie: /profile (włączanie przyciskami), /pauza 2h, /wznow, "
                      "/status.")
        note.setWordWrap(True)
        note.setObjectName("muted")
        self.status = QLabel()
        self.status.setObjectName("profiles_status")
        self.status.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.addWidget(self.table)
        lay.addLayout(bar)
        lay.addWidget(self.status)
        lay.addWidget(note)
        self.refresh()

    def refresh(self) -> None:
        self._profiles = self.repo.all()
        week = self.repo.sent_counts(datetime.now(UTC) - timedelta(days=7))
        self.table.blockSignals(True)
        self.table.setRowCount(len(self._profiles))
        for r, p in enumerate(self._profiles):
            on = QTableWidgetItem()
            on.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            on.setCheckState(Qt.CheckState.Checked if p.enabled else Qt.CheckState.Unchecked)
            on.setData(Qt.ItemDataRole.UserRole, p.id)
            self.table.setItem(r, 0, on)
            self.table.setItem(r, 1, QTableWidgetItem(p.name))
            sent = QTableWidgetItem(str(week.get(p.id, 0)))
            sent.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            self.table.setItem(r, 2, sent)
            desc = QTableWidgetItem(describe(p))
            desc.setToolTip(describe(p))
            self.table.setItem(r, 3, desc)
        self.table.resizeColumnToContents(0)
        self.table.resizeColumnToContents(1)
        self.table.blockSignals(False)

    def _selected(self) -> NotifyProfile | None:
        row = self.table.currentRow()
        return self._profiles[row] if 0 <= row < len(self._profiles) else None

    def _toggled(self, item: QTableWidgetItem) -> None:
        if item.column() != 0:
            return
        self.repo.set_enabled(int(item.data(Qt.ItemDataRole.UserRole)), item.checkState() == Qt.CheckState.Checked)
        self.changed.emit()

    def open_editor(self, profile: NotifyProfile, *, dialog: ProfileDialog | None = None) -> bool:
        dlg = dialog or ProfileDialog(profile, settings=self._get_settings(), db_path=self.db_path,
                                      make_test=self._make_test, parent=self)
        if dialog is None and dlg.exec() != QDialog.DialogCode.Accepted:
            return False
        self.repo.save(dlg.read())
        self.refresh()
        self.changed.emit()
        return True

    def add(self) -> None:
        self.open_editor(NotifyProfile(name="Nowy profil"))

    def edit(self) -> None:
        p = self._selected()
        if p is not None:
            self.open_editor(p)

    def duplicate(self) -> None:
        p = self._selected()
        if p is not None:
            copy_ = copy.deepcopy(p)
            copy_.id, copy_.name = None, f"{p.name} (kopia)"
            self.repo.save(copy_)
            self.refresh()
            self.changed.emit()

    def delete(self) -> None:
        p = self._selected()
        if p is not None and QMessageBox.question(self, "Profile", f"Usunąć profil „{p.name}”?") == \
                QMessageBox.StandardButton.Yes:
            self.repo.delete(p.id)
            self.refresh()
            self.changed.emit()

    def test(self) -> None:
        p = self._selected()
        if p is None or self._make_test is None or self._test_worker is not None:
            return
        worker = FuncWorker(self._make_test(copy.deepcopy(p)))
        worker.finished.connect(self._test_done)
        worker.failed.connect(self._test_failed)
        self._test_worker = worker
        self.status.setText(f"Wysyłanie testu z profilu „{p.name}”…")
        start_in_thread(worker, _thread_owner())

    @Slot(object)
    def _test_done(self, text: str) -> None:
        self._test_worker = None
        self.status.setText(f"✅ {text}")

    @Slot(str)
    def _test_failed(self, error: str) -> None:
        self._test_worker = None
        self.status.setText(f"❌ {error}")
