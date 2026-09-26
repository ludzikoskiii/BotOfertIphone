"""Model tabeli ofert (Qt model/view) z kluczami sortowania dla każdej kolumny."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from enum import IntEnum
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, QRectF, Qt
from PySide6.QtGui import QBrush, QColor, QFont, QPainter
from PySide6.QtWidgets import QStyle, QStyledItemDelegate, QStyleOptionViewItem

from ..core.catalog import format_storage
from ..core.models import Offer, OfferStatus, Severity, Valuation, Verdict
from ..sources import SOURCE_NAMES
from .images import ThumbnailCache
from .theme import FLAG_MARK, WATCHED_MARK, Palette, current

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
OFFER_ROLE = Qt.ItemDataRole.UserRole + 2
VERDICT_ROLE = Qt.ItemDataRole.UserRole + 3

_VERDICT_ORDER = {Verdict.BUY: 2, Verdict.NEGOTIATE: 1, Verdict.SKIP: 0}
_NO_VALUE = float("-inf")
_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
_CENTER = Qt.AlignmentFlag.AlignCenter
_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter


class Col(IntEnum):
    PHOTO = 0
    MODEL = 1
    STORAGE = 2
    PRICE = 3
    PROFIT = 4
    MAX_BUY = 5
    VERDICT = 6
    SOURCE = 7
    CONDITION = 8
    BATTERY = 9
    MARKET = 10
    SCORE = 11
    FLAGS = 12
    LOCATION = 13
    ADDED = 14
    LINK = 15


HEADERS = {
    Col.PHOTO: "Zdjęcie",
    Col.MODEL: "Model",
    Col.STORAGE: "Pamięć",
    Col.PRICE: "Cena",
    Col.PROFIT: "Szac. zysk",
    Col.MAX_BUY: "Max cena zakupu",
    Col.VERDICT: "Werdykt",
    Col.SOURCE: "Portal",
    Col.CONDITION: "Stan",
    Col.BATTERY: "Bateria",
    Col.MARKET: "Wartość rynkowa",
    Col.SCORE: "Ocena",
    Col.FLAGS: "Czerwone flagi",
    Col.LOCATION: "Lokalizacja",
    Col.ADDED: "Dodano",
    Col.LINK: "Link",
}
NUMERIC = {Col.PRICE, Col.MARKET, Col.PROFIT, Col.MAX_BUY, Col.BATTERY, Col.SCORE}
ALWAYS_VISIBLE = {Col.MODEL}
DEFAULT_WIDTHS = {Col.PHOTO: 84, Col.MODEL: 150, Col.STORAGE: 80, Col.PRICE: 95, Col.PROFIT: 110,
                  Col.MAX_BUY: 140, Col.VERDICT: 115, Col.SOURCE: 130, Col.CONDITION: 125, Col.BATTERY: 85,
                  Col.MARKET: 140, Col.SCORE: 75, Col.FLAGS: 220, Col.LOCATION: 170, Col.ADDED: 110, Col.LINK: 80}


def col_key(col: Col) -> str:
    """Nazwa kolumny zapisywana w ustawieniach."""
    return col.name.lower()


def col_from_key(key: str) -> Col | None:
    return Col.__members__.get(key.upper())


def money(value: float | None) -> str:
    if value is None:
        return "—"
    return f"{value:,.0f} zł".replace(",", " ")


def added_at(offer: Offer) -> datetime | None:
    return offer.raw.created_at or offer.first_seen


def format_dt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return dt.astimezone().strftime("%d.%m %H:%M")


class OffersTableModel(QAbstractTableModel):
    def __init__(self, thumbs: ThumbnailCache, parent=None):
        super().__init__(parent)
        self._rows: list[tuple[Offer, Valuation]] = []
        self._thumbs = thumbs
        self._rows_by_photo: dict[str, list[int]] = defaultdict(list)
        self._row_by_id: dict[int, int] = {}
        self._sort_cache: dict[tuple[int, int], Any] = {}
        thumbs.ready.connect(self._thumb_ready)
        self._bold = QFont()
        self._bold.setBold(True)
        self.set_palette(current())

    def set_palette(self, palette: Palette) -> None:
        """Kolory zależne od motywu (odświeża widoczne komórki)."""
        self.palette = palette
        self._positive = QBrush(QColor(palette.positive))
        self._negative = QBrush(QColor(palette.negative))
        self._muted = QBrush(QColor(palette.muted))
        self._watched_bg = QBrush(QColor(palette.watched))
        if self._rows:
            self.dataChanged.emit(self.index(0, 0), self.index(len(self._rows) - 1, len(Col) - 1))

    # --- dane ---

    def set_rows(self, rows: list[tuple[Offer, Valuation]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self._sort_cache.clear()
        self._rows_by_photo.clear()
        self._row_by_id = {offer.id: i for i, (offer, _) in enumerate(rows) if offer.id is not None}
        for i, (offer, _) in enumerate(rows):
            if offer.raw.photos:
                self._rows_by_photo[offer.raw.photos[0]].append(i)
        self.endResetModel()

    def row_at(self, row: int) -> tuple[Offer, Valuation]:
        return self._rows[row]

    def row_of(self, offer_id: int) -> int | None:
        return self._row_by_id.get(offer_id)

    def update_status(self, offer_id: int, status: OfferStatus) -> None:
        row = self.row_of(offer_id)
        if row is None:
            return
        self._rows[row][0].status = status
        self._sort_cache = {k: v for k, v in self._sort_cache.items() if k[0] != row}
        self.dataChanged.emit(self.index(row, 0), self.index(row, len(Col) - 1))

    def rows(self) -> list[tuple[Offer, Valuation]]:
        return self._rows

    def _thumb_ready(self, url: str) -> None:
        for r in self._rows_by_photo.get(url, []):
            idx = self.index(r, Col.PHOTO)
            self.dataChanged.emit(idx, idx, [Qt.ItemDataRole.DecorationRole])

    # --- Qt API ---

    def rowCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex | QPersistentModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 0 if parent.isValid() else len(Col)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if orientation != Qt.Orientation.Horizontal:
            return None
        if role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[Col(section)]
        if role == Qt.ItemDataRole.TextAlignmentRole:
            return _RIGHT if Col(section) in NUMERIC else _LEFT
        return None

    def data(self, index: QModelIndex | QPersistentModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        offer, val = self._rows[index.row()]
        col = Col(index.column())
        if role == Qt.ItemDataRole.DisplayRole:
            return self._display(col, offer, val)
        if role == SORT_ROLE:
            key = (index.row(), index.column())
            if key not in self._sort_cache:
                self._sort_cache[key] = self._sort_key(col, offer, val)
            return self._sort_cache[key]
        if role == OFFER_ROLE:
            return offer.id
        if role == VERDICT_ROLE:
            return val.verdict
        if role == Qt.ItemDataRole.BackgroundRole:
            return self._watched_bg if offer.status is OfferStatus.WATCHED else None
        if role == Qt.ItemDataRole.ForegroundRole:
            return self._foreground(col, offer, val)
        if role == Qt.ItemDataRole.FontRole:
            if col is Col.PROFIT or (col is Col.MODEL and offer.status is OfferStatus.WATCHED):
                return self._bold
            return None
        if role == Qt.ItemDataRole.DecorationRole and col is Col.PHOTO:
            return self._thumbs.get(offer.raw.photos[0] if offer.raw.photos else None)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in NUMERIC:
                return _RIGHT
            if col in (Col.VERDICT, Col.PHOTO):
                return _CENTER
            return _LEFT
        if role == Qt.ItemDataRole.ToolTipRole:
            return self._tooltip(col, offer, val)
        return None

    def _foreground(self, col: Col, offer: Offer, val: Valuation) -> QBrush | None:
        if col is Col.PROFIT:
            if val.expected_profit is None:
                return self._muted
            return self._positive if val.expected_profit > 0 else self._negative
        if col in (Col.MODEL, Col.FLAGS) and val.has_hard_flag:
            return self._negative
        if col in (Col.SOURCE, Col.ADDED, Col.LOCATION):
            return self._muted
        return None

    @staticmethod
    def _tooltip(col: Col, offer: Offer, val: Valuation) -> str | None:
        if col in (Col.MODEL, Col.FLAGS):
            flags = "".join(f"\n{FLAG_MARK} {f.label}" + (" (poważna)" if f.severity is Severity.HARD else "")
                            for f in dict.fromkeys(val.flags))
            return offer.raw.title + flags
        if col is Col.LINK:
            return offer.raw.url
        if col is Col.VERDICT:
            return f"Ocena {val.score}/100\n" + "\n".join(val.reasons)
        return "\n".join(val.reasons) if val.reasons else None

    def _display(self, col: Col, offer: Offer, val: Valuation) -> str:
        p = offer.parsed
        match col:
            case Col.PHOTO:
                return ""
            case Col.MODEL:
                prefix = f"{WATCHED_MARK} " if offer.status is OfferStatus.WATCHED else ""
                suffix = f"  {FLAG_MARK}{len(set(val.flags))}" if val.flags else ""
                return f"{prefix}{p.model or '?'}{suffix}"
            case Col.STORAGE:
                return format_storage(p.storage_gb)
            case Col.CONDITION:
                return p.condition.label
            case Col.BATTERY:
                return f"{p.battery_health} %" if p.battery_health else "—"
            case Col.PRICE:
                return money(offer.price)
            case Col.MARKET:
                return money(val.market.value)
            case Col.PROFIT:
                return money(val.expected_profit)
            case Col.MAX_BUY:
                return money(val.max_buy_price)
            case Col.VERDICT:
                return val.verdict.value
            case Col.SCORE:
                return str(val.score)
            case Col.FLAGS:
                return ", ".join(f.label for f in dict.fromkeys(val.flags)) or "—"
            case Col.SOURCE:
                return SOURCE_NAMES.get(offer.raw.source, offer.raw.source)
            case Col.LOCATION:
                city = offer.raw.city or "—"
                if offer.distance_km is not None:
                    return f"{city} ({offer.distance_km:.0f} km)"
                return city
            case Col.ADDED:
                return format_dt(added_at(offer))
            case Col.LINK:
                return "Otwórz ↗"
        return ""

    def _sort_key(self, col: Col, offer: Offer, val: Valuation) -> Any:
        p = offer.parsed
        match col:
            case Col.PHOTO:
                return len(offer.raw.photos)
            case Col.MODEL:
                return (p.model or "").lower()
            case Col.STORAGE:
                return p.storage_gb or 0
            case Col.CONDITION:
                return p.condition.label
            case Col.BATTERY:
                return p.battery_health or 0
            case Col.PRICE:
                return offer.price
            case Col.MARKET:
                return val.market.value if val.market.value is not None else _NO_VALUE
            case Col.PROFIT:
                return val.expected_profit if val.expected_profit is not None else _NO_VALUE
            case Col.MAX_BUY:
                return val.max_buy_price if val.max_buy_price is not None else _NO_VALUE
            case Col.VERDICT:
                return _VERDICT_ORDER[val.verdict] * 1000 + val.score
            case Col.SCORE:
                return val.score
            case Col.FLAGS:
                return len(set(val.flags)) + (100 if val.has_hard_flag else 0)
            case Col.SOURCE:
                return offer.raw.source
            case Col.LOCATION:
                return offer.distance_km if offer.distance_km is not None else 1e9
            case Col.ADDED:
                dt = added_at(offer)
                return dt.timestamp() if dt else 0.0
            case Col.LINK:
                return offer.raw.url
        return None


class VerdictDelegate(QStyledItemDelegate):
    """Werdykt jako kolorowa etykieta (kropka + tekst) — kolor nie jest jedynym nośnikiem informacji."""

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        value = index.data(VERDICT_ROLE)  # PySide oddaje enum jako str
        if value is None:
            super().paint(painter, option, index)
            return
        verdict = Verdict(value)
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else None
        if style is not None:
            style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, opt.widget)
        pal = current()
        font = QFont(opt.font)
        font.setBold(True)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(font)
        fm = painter.fontMetrics()
        h = min(fm.height() + 8, opt.rect.height() - 4)
        w = min(fm.horizontalAdvance(text) + 34, opt.rect.width() - 8)
        rect = QRectF(opt.rect.center().x() - w / 2, opt.rect.center().y() - h / 2 + 0.5, w, h)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(pal.verdict_bg[verdict]))
        painter.drawRoundedRect(rect, h / 2, h / 2)
        fg = QColor(pal.verdict_fg[verdict])
        painter.setBrush(fg)
        dot = 8
        painter.drawEllipse(QRectF(rect.left() + 10, rect.center().y() - dot / 2, dot, dot))
        painter.setPen(fg)
        painter.drawText(rect.adjusted(22, 0, -8, 0), Qt.AlignmentFlag.AlignCenter, text)
        painter.restore()
