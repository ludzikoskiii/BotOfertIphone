"""Model tabeli ofert (Qt model/view) z kluczami sortowania dla każdej kolumny."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from enum import IntEnum
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QPersistentModelIndex, Qt
from PySide6.QtGui import QBrush, QColor, QFont

from ..core.catalog import format_storage
from ..core.models import Offer, OfferStatus, Severity, Valuation, Verdict
from .images import ThumbnailCache
from .theme import FLAG_MARK, ROW_BACKGROUND, VERDICT_COLOR, WATCHED_MARK

SORT_ROLE = Qt.ItemDataRole.UserRole + 1
OFFER_ROLE = Qt.ItemDataRole.UserRole + 2

SOURCE_NAMES = {"olx": "OLX", "allegro_lokalnie": "Allegro Lokalnie", "vinted": "Vinted"}
_VERDICT_ORDER = {Verdict.BUY: 2, Verdict.NEGOTIATE: 1, Verdict.SKIP: 0}
_NO_VALUE = float("-inf")


class Col(IntEnum):
    PHOTO = 0
    MODEL = 1
    STORAGE = 2
    CONDITION = 3
    PRICE = 4
    MARKET = 5
    PROFIT = 6
    MAX_BUY = 7
    VERDICT = 8
    SOURCE = 9
    LOCATION = 10
    ADDED = 11
    LINK = 12


HEADERS = {
    Col.PHOTO: "Zdjęcie",
    Col.MODEL: "Model",
    Col.STORAGE: "Pamięć",
    Col.CONDITION: "Stan",
    Col.PRICE: "Cena",
    Col.MARKET: "Wartość rynkowa",
    Col.PROFIT: "Szac. zysk",
    Col.MAX_BUY: "Max cena zakupu",
    Col.VERDICT: "Werdykt",
    Col.SOURCE: "Portal",
    Col.LOCATION: "Lokalizacja",
    Col.ADDED: "Dodano",
    Col.LINK: "Link",
}
NUMERIC = {Col.PRICE, Col.MARKET, Col.PROFIT, Col.MAX_BUY}


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
        self._sort_cache: dict[tuple[int, int], Any] = {}
        thumbs.ready.connect(self._thumb_ready)
        self._bg = {c: QBrush(QColor(v)) for c, v in ROW_BACKGROUND.items()}
        self._verdict_fg = {v: QBrush(QColor(c)) for v, c in VERDICT_COLOR.items()}
        self._flag_fg = QBrush(QColor("#c92a2a"))
        self._bold = QFont()
        self._bold.setBold(True)

    # --- dane ---

    def set_rows(self, rows: list[tuple[Offer, Valuation]]) -> None:
        self.beginResetModel()
        self._rows = rows
        self._sort_cache.clear()
        self._rows_by_photo.clear()
        for i, (offer, _) in enumerate(rows):
            if offer.raw.photos:
                self._rows_by_photo[offer.raw.photos[0]].append(i)
        self.endResetModel()

    def row_at(self, row: int) -> tuple[Offer, Valuation]:
        return self._rows[row]

    def row_of(self, offer_id: int) -> int | None:
        for i, (offer, _) in enumerate(self._rows):
            if offer.id == offer_id:
                return i
        return None

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
        if orientation == Qt.Orientation.Horizontal and role == Qt.ItemDataRole.DisplayRole:
            return HEADERS[Col(section)]
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
        if role == Qt.ItemDataRole.BackgroundRole:
            return self._bg[val.color]
        if role == Qt.ItemDataRole.ForegroundRole:
            if col is Col.VERDICT:
                return self._verdict_fg[val.verdict]
            if col is Col.MODEL and val.has_hard_flag:
                return self._flag_fg
            return None
        if role == Qt.ItemDataRole.FontRole:
            if col in (Col.VERDICT, Col.PROFIT) or (col is Col.MODEL and offer.status is OfferStatus.WATCHED):
                return self._bold
            return None
        if role == Qt.ItemDataRole.DecorationRole and col is Col.PHOTO:
            return self._thumbs.get(offer.raw.photos[0] if offer.raw.photos else None)
        if role == Qt.ItemDataRole.TextAlignmentRole:
            if col in NUMERIC:
                return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            if col in (Col.VERDICT, Col.STORAGE, Col.PHOTO):
                return int(Qt.AlignmentFlag.AlignCenter)
        if role == Qt.ItemDataRole.ToolTipRole:
            if col is Col.MODEL:
                flags = "".join(f"\n{FLAG_MARK} {f.label}" + (" (poważna)" if f.severity is Severity.HARD else "")
                                for f in dict.fromkeys(val.flags))
                return offer.raw.title + flags
            if col is Col.LINK:
                return offer.raw.url
            return "\n".join(val.reasons) if val.reasons else None
        return None

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
            case Col.PRICE:
                return money(offer.price)
            case Col.MARKET:
                return money(val.market.value)
            case Col.PROFIT:
                return money(val.expected_profit)
            case Col.MAX_BUY:
                return money(val.max_buy_price)
            case Col.VERDICT:
                return f"{val.verdict.value} · {val.score}"
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
