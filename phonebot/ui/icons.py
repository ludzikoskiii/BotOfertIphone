"""Ikona aplikacji rysowana w kodzie (bez plików graficznych)."""
from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPen, QPixmap


def app_pixmap(size: int = 256, badge: bool = False) -> QPixmap:
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    s = size / 256
    # telefon
    p.setBrush(QColor("#212529"))
    p.setPen(Qt.PenStyle.NoPen)
    p.drawRoundedRect(QRectF(58 * s, 14 * s, 140 * s, 228 * s), 30 * s, 30 * s)
    p.setBrush(QColor("#2b8a3e"))
    p.drawRoundedRect(QRectF(70 * s, 30 * s, 116 * s, 196 * s), 18 * s, 18 * s)
    # znak „zł"
    p.setPen(QPen(QColor("white")))
    font = QFont("Arial")
    font.setBold(True)
    font.setPixelSize(int(78 * s))
    p.setFont(font)
    p.drawText(QRectF(70 * s, 30 * s, 116 * s, 196 * s), Qt.AlignmentFlag.AlignCenter, "zł")
    if badge:
        p.setBrush(QColor("#fa5252"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QRectF(170 * s, 0, 86 * s, 86 * s))
    p.end()
    return pm


def app_icon(badge: bool = False) -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(app_pixmap(size, badge))
    return icon
