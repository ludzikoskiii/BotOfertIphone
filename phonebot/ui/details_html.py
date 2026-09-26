"""Pełne wyliczenie opłacalności oferty jako HTML (dla okna szczegółów).

Moduł nie importuje Qt, dzięki czemu treść raportu jest testowana jednostkowo.
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from ..core.catalog import format_storage
from ..core.models import Offer, OfferStatus, Severity, Valuation
from ..core.settings import Settings
from ..sources import SOURCE_NAMES
from .theme import COLOR_LABEL, Palette, current


def _css(pal: Palette) -> str:
    return f"""
body {{ font-family: 'Segoe UI', sans-serif; font-size: 10pt; color: {pal.text}; }}
h2 {{ margin: 0 0 4px 0; font-size: 13pt; }}
h3 {{ margin: 16px 0 6px 0; font-size: 11pt; color: {pal.text}; border-bottom: 1px solid {pal.border}; }}
table.calc {{ border-collapse: collapse; width: 100%; }}
table.calc td {{ padding: 3px 6px; }}
td.num {{ text-align: right; white-space: nowrap; }}
tr.total td {{ border-top: 1px solid {pal.border}; font-weight: bold; }}
.muted {{ color: {pal.muted}; }}
.flag-hard {{ color: {pal.negative}; font-weight: bold; }}
.flag-soft {{ color: {pal.warning}; }}
.desc {{ white-space: pre-wrap; background: {pal.surface_alt}; padding: 8px; }}
"""


def zl(value: float | None, sign: bool = False) -> str:
    if value is None:
        return "—"
    text = f"{abs(value):,.0f} zł".replace(",", " ")
    if value < 0:
        return "−" + text
    return ("+" if sign and value > 0 else "") + text


def _row(label: str, value: str, cls: str = "") -> str:
    return f'<tr class="{cls}"><td width="68%">{escape(label)}</td><td class="num">{value}</td></tr>'


def _fmt_dt(dt: datetime | None) -> str:
    return dt.astimezone().strftime("%d.%m.%Y %H:%M") if dt else "—"


def build_details_html(
    offer: Offer,
    val: Valuation,
    settings: Settings,
    price_history: list[tuple[datetime, float]] | None = None,
    palette: Palette | None = None,
) -> str:
    p, raw = offer.parsed, offer.raw
    pal = palette or current()
    parts: list[str] = [f"<html><head><style>{_css(pal)}</style></head><body>"]

    # --- nagłówek i werdykt ---
    status = " ★ obserwowana" if offer.status is OfferStatus.WATCHED else (
        " (ukryta)" if offer.status is OfferStatus.HIDDEN else "")
    parts.append(f"<h2>{escape(raw.title)}{escape(status)}</h2>")
    location = escape(raw.city or "—")
    if offer.distance_km is not None:
        location += f" ({offer.distance_km:.0f} km od: {escape(settings.location_name)})"
    shipping = {True: "wysyłka dostępna", False: "tylko odbiór osobisty", None: "wysyłka: brak danych"}
    portal = escape(SOURCE_NAMES.get(raw.source, raw.source))
    parts.append(
        f'<p class="muted">{portal} · {location} · {shipping[raw.shipping_available]} · '
        f"dodano {_fmt_dt(raw.created_at or offer.first_seen)}</p>"
    )
    parts.append(
        f'<table width="100%" cellpadding="10" style="background:{pal.verdict_bg[val.verdict]}; '
        f'color:{pal.verdict_fg[val.verdict]};"><tr>'
        f'<td nowrap><span style="font-size:15pt; font-weight:bold;">{val.verdict.value}</span></td>'
        f'<td class="num">Cena: <b>{zl(raw.price)}</b><br>Ocena: <b>{val.score}/100</b> '
        f"({COLOR_LABEL[val.color]})</td></tr></table>"
    )
    parts.append("<p>" + "<br>".join(escape(r) for r in val.reasons if not r.startswith("⚑")) + "</p>")

    # --- negocjacje ---
    neg = val.negotiation
    parts.append("<h3>Rekomendacja negocjacji</h3>")
    parts.append(f"<p>{escape(neg.note)}</p>")
    if neg.opening_price or neg.max_price:
        parts.append('<table class="calc">')
        if neg.opening_price:
            parts.append(_row("Proponowana cena otwierająca", f"<b>{zl(neg.opening_price)}</b>"))
        if neg.max_price:
            parts.append(_row("Maksymalnie zapłać", f"<b>{zl(neg.max_price)}</b>"))
        parts.append("</table>")

    # --- wyliczenie ---
    mode = val.mode
    parts.append(f"<h3>Wyliczenie opłacalności — tryb „{escape(mode.label)}”</h3>")
    m = val.market
    source_txt = f"{escape(m.method)}, pewność: {escape(m.confidence)}"
    if m.raw_median is not None and m.value is not None and abs(m.raw_median - m.value) > 1:
        source_txt += f" · mediana {zl(m.raw_median)} × korekta"
    parts.append('<table class="calc">')
    parts.append(_row("Wartość rynkowa (sprzedaż)", f"<b>{zl(m.value)}</b>"))
    parts.append(f'<tr><td colspan="2" class="muted">&nbsp;&nbsp;{source_txt}</td></tr>')
    parts.append(_row("Cena zakupu", zl(-raw.price)))
    for item in val.repair_items:
        parts.append(_row(f"Naprawa: {item.label}", zl(-item.amount)))
    for item in val.cost_items:
        parts.append(_row(item.label, zl(-item.amount)))
    if val.expected_profit is not None:
        color = pal.positive if val.expected_profit > 0 else pal.negative
        roi = f" ({val.roi_pct:.0f}%)" if val.roi_pct is not None else ""
        parts.append(_row("Przewidywany zysk", f'<span style="color:{color}">{zl(val.expected_profit, True)}{roi}</span>',
                          "total"))
        rule = settings.profit_rule(mode)
        parts.append(_row(f"Wymagany zysk (min. {rule.min_amount:.0f} zł / {rule.min_percent:g}%)",
                          zl(val.required_profit)))
        parts.append(_row("Maksymalna cena zakupu", f"<b>{zl(val.max_buy_price)}</b>"))
    parts.append("</table>")

    # --- flagi ---
    if val.flags:
        parts.append("<h3>Czerwone flagi</h3><ul>")
        for f in dict.fromkeys(val.flags):
            cls = "flag-hard" if f.severity is Severity.HARD else "flag-soft"
            kind = "poważna" if f.severity is Severity.HARD else "ostrzeżenie"
            parts.append(f'<li class="{cls}">⚑ {escape(f.label)} <span class="muted">({kind}, '
                         f"−{settings.penalty(f)} pkt)</span></li>")
        parts.append("</ul>")

    # --- rozpoznane dane ---
    parts.append("<h3>Rozpoznane z ogłoszenia</h3><table class='calc'>")
    parts.append(_row("Model", escape(p.model or "nierozpoznany")))
    parts.append(_row("Pamięć", format_storage(p.storage_gb)))
    parts.append(_row("Stan", escape(p.condition.label)))
    defects = ", ".join(d.label + (" (AI)" if d in offer.ai_defects else "") for d in p.defects)
    parts.append(_row("Usterki", escape(defects or "brak wykrytych")))
    parts.append(_row("Kondycja baterii", f"{p.battery_health}%" if p.battery_health else "—"))
    neg_txt = {True: "tak", False: "nie (cena ostateczna)", None: "brak informacji"}[p.negotiable]
    parts.append(_row("Do negocjacji", neg_txt))
    if offer.ai_note is not None:
        ai_txt = offer.ai_note or "brak uwag"
        if offer.ai_flags:
            ai_txt += " · flagi: " + ", ".join(f.label for f in offer.ai_flags)
        parts.append(_row("Analiza AI", escape(ai_txt)))
    parts.append("</table>")

    if price_history and len(price_history) > 1:
        parts.append("<h3>Historia ceny</h3><table class='calc'>")
        for dt, price in price_history:
            parts.append(_row(_fmt_dt(dt), zl(price)))
        parts.append("</table>")

    parts.append("<h3>Opis ogłoszenia</h3>")
    parts.append(f'<div class="desc">{escape(raw.description or "(brak opisu)")}</div>')
    parts.append("</body></html>")
    return "".join(parts)
