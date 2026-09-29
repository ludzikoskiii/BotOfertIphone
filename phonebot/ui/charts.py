"""Lekkie wykresy (QPainter) do zakładki „Rynek”: linia z pasmem zakresu i słupki.

Bez QtCharts/matplotlib (mniejszy PhoneBot.exe). Jeden kolor serii (niebieski z walidowanej palety, osobny
stopień dla trybu ciemnego), cienkie linie 2 px, wyciszona siatka, tekst w kolorach tekstu (nie serii).
Najechanie myszą: pionowa linia i podpowiedź z wartościami (wykres liniowy) albo podpowiedź słupka.
Za mało danych — napis zamiast mylącego wykresu.
"""
from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, timedelta

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QMouseEvent, QPainter, QPainterPath, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from ..core.market_stats import TOO_LITTLE
from .theme import current

SERIES = {"light": "#2a78d6", "dark": "#3987e5"}  # niebieski — slot 1 palety (kontrast ≥ 3:1 na obu tłach)


def series_color() -> QColor:
    return QColor(SERIES["dark" if current().dark else "light"])


def nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    """„Ładne” wartości osi (1, 2, 2,5, 5 × 10^n)."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(1, count - 1)
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.5:
        ticks.append(round(v, 6))
        v += step
    return ticks


def zl(v: float) -> str:
    return f"{v:,.0f} zł".replace(",", " ")


@dataclass
class BandPoint:
    day: date
    median: float
    low: float
    high: float
    n: int


class _Chart(QWidget):
    MARGIN = (64, 14, 16, 30)  # lewy, górny, prawy, dolny

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(190)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMouseTracking(True)
        self.empty_text = TOO_LITTLE
        self._hover: int | None = None

    def plot_rect(self) -> QRectF:
        left, top, right, bottom = self.MARGIN
        return QRectF(left, top, max(10, self.width() - left - right), max(10, self.height() - top - bottom))

    def _paint_empty(self, p: QPainter) -> None:
        pal = current()
        p.setPen(QColor(pal.muted))
        p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self.empty_text)

    def _y_axis(self, p: QPainter, lo: float, hi: float, fmt: Callable[[float], str]) -> tuple[float, float]:
        pal = current()
        ticks = nice_ticks(lo, hi)
        lo, hi = ticks[0], ticks[-1]
        r = self.plot_rect()
        grid = QColor(pal.border)
        for t in ticks:
            y = r.bottom() - (t - lo) / (hi - lo) * r.height()
            p.setPen(QPen(grid, 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor(pal.muted))
            p.drawText(QRectF(0, y - 9, r.left() - 8, 18), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                       fmt(t))
        return lo, hi

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = None
        QToolTip.hideText()
        self.update()


class LineBandChart(_Chart):
    """Mediana (linia) i typowy zakres (pasmo) w czasie."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points: list[BandPoint] = []
        self.first: date | None = None
        self.last: date | None = None

    def set_data(self, points: list[BandPoint], first: date, last: date, empty_text: str = TOO_LITTLE) -> None:
        self.points, self.first, self.last, self.empty_text = points, first, last, empty_text
        self._hover = None
        self.update()

    def _x(self, d: date) -> float:
        r = self.plot_rect()
        span = max(1, (self.last - self.first).days)
        return r.left() + (d - self.first).days / span * r.width()

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.points:
            self._paint_empty(p)
            return
        pal = current()
        lo = min(pt.low for pt in self.points)
        hi = max(pt.high for pt in self.points)
        pad = max(1.0, (hi - lo) * 0.05)
        lo, hi = self._y_axis(p, max(0.0, lo - pad), hi + pad, zl)
        r = self.plot_rect()

        def y(v: float) -> float:
            return r.bottom() - (v - lo) / (hi - lo) * r.height()

        color = series_color()
        # pasmo zakresu i linia mediany — osobno dla ciągłych odcinków (luka w danych = przerwa)
        runs: list[list[BandPoint]] = []
        for pt in self.points:
            if runs and (pt.day - runs[-1][-1].day).days <= 2:
                runs[-1].append(pt)
            else:
                runs.append([pt])
        band = QColor(color)
        band.setAlphaF(0.18)
        for run in runs:
            poly = QPolygonF([QPointF(self._x(pt.day), y(pt.high)) for pt in run]
                             + [QPointF(self._x(pt.day), y(pt.low)) for pt in reversed(run)])
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(band)
            p.drawPolygon(poly)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(color, 2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
            if len(run) == 1:
                p.setBrush(color)
                p.drawEllipse(QPointF(self._x(run[0].day), y(run[0].median)), 3, 3)
                p.setBrush(Qt.BrushStyle.NoBrush)
            else:
                path = QPainterPath(QPointF(self._x(run[0].day), y(run[0].median)))
                for pt in run[1:]:
                    path.lineTo(QPointF(self._x(pt.day), y(pt.median)))
                p.drawPath(path)
        # oś X: kilka dat
        p.setPen(QColor(pal.muted))
        span = max(1, (self.last - self.first).days)
        step = max(1, math.ceil(span / max(2, int(r.width() // 90))))
        d = self.first
        while d <= self.last:
            x = self._x(d)
            p.drawText(QRectF(x - 40, r.bottom() + 6, 80, 18), Qt.AlignmentFlag.AlignHCenter, d.strftime("%d.%m"))
            d += timedelta(days=step)
        # etykieta ostatniej mediany (bezpośredni podpis zamiast legendy)
        last = self.points[-1]
        p.setPen(QColor(pal.text))
        bold = QFont(self.font())
        bold.setBold(True)
        p.setFont(bold)
        lx = min(self._x(last.day) - 4, r.right() - 90)
        p.drawText(QRectF(lx - 60, y(last.median) - 24, 150, 18), Qt.AlignmentFlag.AlignRight, zl(last.median))
        p.setFont(self.font())
        # najechanie: linia pionowa + znacznik
        if self._hover is not None and 0 <= self._hover < len(self.points):
            pt = self.points[self._hover]
            x = self._x(pt.day)
            p.setPen(QPen(QColor(pal.muted), 1, Qt.PenStyle.DashLine))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QPen(QColor(pal.surface), 2))
            p.setBrush(color)
            p.drawEllipse(QPointF(x, y(pt.median)), 5, 5)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self.points:
            return
        x = event.position().x()
        idx = min(range(len(self.points)), key=lambda i: abs(self._x(self.points[i].day) - x))
        if idx != self._hover:
            self._hover = idx
            self.update()
        pt = self.points[idx]
        QToolTip.showText(event.globalPosition().toPoint(),
                          f"{pt.day:%d.%m.%Y}\nmediana: {zl(pt.median)}\nzakres (80% ofert): {zl(pt.low)} – "
                          f"{zl(pt.high)}\nofert: {pt.n}", self)


class BarChart(_Chart):
    """Słupki (jedna seria) z podpowiedzią przy najechaniu."""

    MARGIN = (44, 14, 12, 30)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.labels: list[str] = []
        self.values: list[float] = []
        self.tips: list[str] = []
        self.label_every = 1

    def set_data(self, labels: list[str], values: list[float], tips: list[str] | None = None, *,
                 empty_text: str = TOO_LITTLE, label_every: int = 1, show: bool = True) -> None:
        self.labels, self.values = (labels, values) if show else ([], [])
        self.tips = tips or [f"{lb}: {v:g}" for lb, v in zip(labels, values, strict=True)]
        self.empty_text, self.label_every = empty_text, max(1, label_every)
        self._hover = None
        self.update()

    def _bar_rect(self, i: int, top: float) -> QRectF:
        r = self.plot_rect()
        slot = r.width() / len(self.values)
        gap = 2 if slot > 6 else 1
        return QRectF(r.left() + i * slot + gap / 2, top, max(1.0, slot - gap), r.bottom() - top)

    def paintEvent(self, event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.values or not any(self.values):
            self._paint_empty(p)
            return
        pal = current()
        lo, hi = self._y_axis(p, 0, max(self.values), lambda v: f"{v:g}")
        r = self.plot_rect()
        color = series_color()
        for i, v in enumerate(self.values):
            top = r.bottom() - (v - lo) / (hi - lo) * r.height()
            rect = self._bar_rect(i, top)
            if v <= 0:
                continue
            c = QColor(color)
            if self._hover is not None and i != self._hover:
                c.setAlphaF(0.55)
            radius = min(4.0, rect.width() / 2, rect.height())
            path = QPainterPath()
            path.addRoundedRect(rect, radius, radius)
            square = QPainterPath()  # zaokrąglony tylko koniec z danymi — podstawa prosta, przy osi
            square.addRect(QRectF(rect.left(), rect.bottom() - radius, rect.width(), radius))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(c)
            p.drawPath(path.united(square))
        p.setPen(QColor(pal.muted))
        slot = r.width() / len(self.values)
        for i, lb in enumerate(self.labels):
            if i % self.label_every:
                continue
            x = r.left() + i * slot
            p.drawText(QRectF(x - 20, r.bottom() + 6, slot + 40, 18), Qt.AlignmentFlag.AlignHCenter, lb)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if not self.values:
            return
        r = self.plot_rect()
        i = int((event.position().x() - r.left()) / (r.width() / len(self.values)))
        if 0 <= i < len(self.values):
            if i != self._hover:
                self._hover = i
                self.update()
            QToolTip.showText(event.globalPosition().toPoint(), self.tips[i], self)
