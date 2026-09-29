"""Pełne wyliczenie opłacalności oferty jako HTML (dla okna szczegółów).

Moduł nie importuje Qt, dzięki czemu treść raportu jest testowana jednostkowo.
"""
from __future__ import annotations

from datetime import datetime
from html import escape

from ..core.catalog import format_storage
from ..core.fraud import SAFETY_TIPS
from ..core.models import Offer, OfferStatus, RedFlag, Severity, Valuation
from ..core.sanity import SANITY_FLAGS
from ..core.selection import pick_reason
from ..core.settings import Settings
from ..core.work_time import format_minutes
from ..core.work_time import summary as time_summary
from ..ml.combine import AGREE, CONFLICT, MISSING, UNSURE
from ..ml.combine import combine as combine_layers
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


_AI_FLAGS = {RedFlag.AI_TEXT_CONFLICT, RedFlag.AI_PHOTO_CONFLICT, RedFlag.AI_LOW_CONFIDENCE, RedFlag.AI_DESC_CONFLICT}


def _layers_html(offer: Offer, val: Valuation, settings: Settings, pal: Palette) -> str:
    combo = combine_layers(offer.layers, settings.ml)
    colors = {AGREE: pal.positive, CONFLICT: pal.negative, UNSURE: pal.warning, MISSING: pal.muted,
              "niska pewność": pal.warning, "brak danych": pal.muted}
    icons = {AGREE: "✔", CONFLICT: "✖", UNSURE: "?", MISSING: "–", "niska pewność": "?", "brak danych": "–"}

    def state(name: str, text: str) -> str:
        return (f'<span style="color:{colors.get(name, pal.text)}"><b>{icons.get(name, "")} {escape(name)}</b></span>'
                f" · {escape(text)}")

    sanity = [f.label for f in dict.fromkeys(val.flags) if f in SANITY_FLAGS and f not in _AI_FLAGS]
    rules_text = "przeszła filtr tytułu, kraju i sprzedawcy"
    if sanity:
        rules_text += "; testy sensowności: " + ", ".join(sanity)
    rows = [f'<tr><td width="34%">Reguły (etap 1)</td><td>{state(CONFLICT if sanity else AGREE, rules_text)}</td></tr>']
    for layer in combo.layers:
        rows.append(f'<tr><td width="34%">{escape(layer.name)}</td><td>{state(layer.state, layer.summary)}</td></tr>')
    rows.append(f'<tr class="total"><td>Łącznie</td><td>{state(combo.state, combo.note)}</td></tr>')
    return "<h3>Ocena warstw (reguły + lokalne AI)</h3><table class='calc'>" + "".join(rows) + "</table>"


def _time_html(val: Valuation, settings: Settings, pal: Palette) -> str:
    """Czas pracy: naprawa + obsługa, zysk na godzinę, koszt czasu wg stawki (osobno, nie w zysku)."""
    if val.work_minutes is None:
        return ""
    cfg = settings.work
    out = ["<h3>Czas pracy</h3>"]
    if val.profit_per_hour is not None and val.expected_profit is not None:
        low = cfg.min_profit_per_hour and val.profit_per_hour < cfg.min_profit_per_hour
        color = pal.negative if val.profit_per_hour <= 0 else pal.warning if low else pal.positive
        line = time_summary(val.expected_profit, val.work_minutes, val.profit_per_hour)
        out.append(f'<p><b style="color:{color}">{escape(line)}</b>')
        if cfg.min_profit_per_hour:
            out.append(f' <span class="muted">(próg: {cfg.min_profit_per_hour:.0f} zł/h)</span>')
        out.append("</p>")
    out.append('<table class="calc">')
    for item in val.time_items:
        out.append(_row(escape(item.label), format_minutes(item.minutes)))
    out.append(_row("Razem", f"<b>{format_minutes(val.work_minutes)}</b>", "total"))
    if val.time_cost is not None and cfg.hourly_rate:
        out.append(_row(f"Koszt Twojego czasu ({cfg.hourly_rate:.0f} zł/h)", zl(-val.time_cost)))
        if val.expected_profit is not None:
            rest = round(val.expected_profit - val.time_cost, 2)
            color = pal.positive if rest > 0 else pal.negative
            out.append(_row("Zysk po opłaceniu Twojego czasu",
                            f'<span style="color:{color}">{zl(rest, True)}</span>'))
    out.append("</table>")
    out.append('<p class="muted">Koszt czasu nie jest odejmowany od przewidywanego zysku — pokazuje, ile z zysku '
               "to zapłata za Twoją pracę. Czasy napraw zmienisz w „Tabeli części”, czas obsługi w "
               "Ustawieniach → Czas pracy.</p>")
    return "".join(out)


def _corrections_html(val: Valuation, settings: Settings) -> str:
    """Poprawki z Twoich transakcji (model + usterki) i podgląd wyceny z poprawkami i bez nich."""
    alt = val.alternative
    if not val.corrections and alt is None:
        return ""
    on = settings.learning.enabled
    out = ["<h3>Poprawki z Twoich transakcji</h3>"]
    if val.corrections:
        out.append("<ul>" + "".join(f"<li>{escape(n)}</li>" for n in val.corrections) + "</ul>")
    elif alt is not None:
        out.append('<p class="muted">Poprawki są wyłączone (zakładka „Transakcje”) — poniżej podgląd, jak zmieniłyby '
                   "wycenę.</p>")
    if alt is not None:
        now, other = ("z poprawkami", "bez poprawek") if on else ("bez poprawek (teraz)", "z poprawkami")

        def per_hour(v: Valuation) -> str:
            return "—" if v.profit_per_hour is None else f"{v.profit_per_hour:.0f} zł/h"

        rows = [("Werdykt", escape(val.verdict.value), escape(alt.verdict.value)),
                ("Przewidywany zysk", zl(val.expected_profit), zl(alt.expected_profit)),
                ("Maksymalna cena zakupu", zl(val.max_buy_price), zl(alt.max_buy_price)),
                ("Koszt naprawy", zl(val.repair_cost), zl(alt.repair_cost)),
                ("Czas pracy", format_minutes(val.work_minutes), format_minutes(alt.work_minutes)),
                ("Zysk na godzinę", per_hour(val), per_hour(alt))]
        out.append(f'<table class="calc"><tr><td></td><td><b>{now}</b></td><td><b>{other}</b></td></tr>')
        for label, a, b in rows:
            out.append(f"<tr><td>{label}</td><td>{a}</td><td>{b}</td></tr>")
        out.append("</table>")
    return "".join(out)


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
    if not offer.active:
        parts.append(f'<p class="flag-hard">⌛ Nieaktualna — oferta zniknęła z portalu (ostatnio widziana '
                     f"{_fmt_dt(offer.last_seen)}). Zostaje w „Wybrane”, dopóki jej nie usuniesz.</p>")
    reason = pick_reason(offer, val, settings.selection)
    if reason:
        parts.append(f'<p class="muted">✓ {escape(reason)}</p>')
    if offer.transaction_id is not None:
        parts.append('<p class="muted">🛒 Kupiona — transakcja w zakładce „Transakcje” (przycisk „🛒 Transakcja”).</p>')
    location = escape(raw.city or "—")
    if offer.distance_km is not None:
        location += f" ({offer.distance_km:.0f} km od: {escape(settings.location_name)})"
    shipping = {True: "wysyłka dostępna", False: "tylko odbiór osobisty", None: "wysyłka: brak danych"}
    portal = escape(SOURCE_NAMES.get(raw.source, raw.source))
    parts.append(
        f'<p class="muted">{portal} · {location} · {shipping[raw.shipping_available]} · '
        f"dodano {_fmt_dt(raw.created_at or offer.first_seen)}</p>"
    )
    if offer.also_on:
        links = ", ".join(f'<a href="{escape(url, quote=True)}">{escape(SOURCE_NAMES.get(src, src))}</a> ({zl(price)})'
                          for src, url, price in offer.also_on)
        parts.append(f'<p class="muted">Ta sama oferta także na: {links}</p>')
    # werdykt w osobnej linii — „DO WERYFIKACJI” nie łamie się w wąskim panelu
    verdict = "MOŻLIWE OSZUSTWO" if getattr(val.risk, "level", "") == "high" else val.verdict.value
    parts.append(
        f'<table width="100%" cellpadding="10" style="background:{pal.verdict_bg[val.verdict]}; '
        f'color:{pal.verdict_fg[val.verdict]};"><tr><td>'
        f'<span style="font-size:15pt; font-weight:bold; white-space:nowrap;">{verdict}</span><br>'
        f'Cena: <b>{zl(raw.price)}</b> · Ocena: <b>{val.score}/100</b> ({COLOR_LABEL[val.color]})'
        f"</td></tr></table>"
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
    if m.reference_note:
        parts.append(f'<tr><td colspan="2" class="muted">&nbsp;&nbsp;cena referencyjna: {escape(m.reference_note)}</td></tr>')
    if raw.params.get("original_currency") and raw.params.get("original_currency") != "PLN":
        parts.append(f'<tr><td colspan="2" class="muted">&nbsp;&nbsp;cena w ogłoszeniu: '
                     f'{escape(raw.params.get("original_price", ""))} {escape(raw.params["original_currency"])} '
                     f'(kurs NBP {escape(raw.params.get("fx_date", ""))})</td></tr>')
    if val.trend_text:
        parts.append(f'<tr><td colspan="2" class="muted">&nbsp;&nbsp;trend ceny ({settings.market_stats.trend_days} '
                     f"dni): {escape(val.trend_text)}</td></tr>")
    if val.active_days is not None:
        parts.append(f'<tr><td colspan="2" class="muted">&nbsp;&nbsp;ogłoszenia tego modelu są aktywne średnio '
                     f"~{val.active_days:.0f} dni (zakładka „Rynek”)</td></tr>")
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
        if val.time_limited and val.work_minutes:
            label = (f"Wymagany zysk (min. {settings.work.min_profit_per_hour:.0f} zł/h × "
                     f"{format_minutes(val.work_minutes)})")
        else:
            label = f"Wymagany zysk (min. {rule.min_amount:.0f} zł / {rule.min_percent:g}%)"
        parts.append(_row(label, zl(val.required_profit)))
        parts.append(_row("Maksymalna cena zakupu", f"<b>{zl(val.max_buy_price)}</b>"))
    parts.append("</table>")
    parts.append(_time_html(val, settings, pal))
    parts.append(_corrections_html(val, settings))

    # --- warstwy oceny: reguły + lokalne AI ---
    parts.append(_layers_html(offer, val, settings, pal))

    # --- ryzyko oszustwa ---
    risk = val.risk
    if risk is not None and risk.signals:
        cls = "flag-hard" if risk.level == "high" else "flag-soft" if risk.level == "medium" else "muted"
        title = "MOŻLIWE OSZUSTWO" if risk.level == "high" else f"Ryzyko oszustwa: {risk.label}"
        parts.append(f'<h3>Ryzyko oszustwa</h3><p class="{cls}"><b>{escape(title)}</b> ({risk.score} pkt)</p><ul>')
        for sgn in risk.signals:
            detail = f" — {escape(sgn.detail)}" if sgn.detail else ""
            parts.append(f"<li>{escape(sgn.label)}{detail} <span class=\"muted\">(+{sgn.points} pkt)</span></li>")
        parts.append("</ul>")
        if risk.level != "low":
            parts.append("<p><b>Jak kupić bezpiecznie:</b></p><ul>"
                         + "".join(f"<li>{escape(t)}</li>" for t in SAFETY_TIPS) + "</ul>")

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
    ai_mark = " (AI z opisu)"
    parts.append(_row("Model", escape(p.model or "nierozpoznany")))
    parts.append(_row("Pamięć", format_storage(p.storage_gb) + (ai_mark if "storage" in offer.ai_filled else "")))
    parts.append(_row("Stan", escape(p.condition.label) + (ai_mark if "for_parts" in offer.ai_filled else "")))
    defects = ", ".join(d.label + (" (AI)" if d in offer.ai_defects else "") for d in p.defects)
    parts.append(_row("Usterki", escape(defects or "brak wykrytych")))
    battery = f"{p.battery_health}%" if p.battery_health else "—"
    parts.append(_row("Kondycja baterii", battery + (ai_mark if "battery" in offer.ai_filled else "")))
    neg_txt = {True: "tak", False: "nie (cena ostateczna)", None: "brak informacji"}[p.negotiable]
    parts.append(_row("Do negocjacji", neg_txt))
    parts.append("</table>")
    if offer.ai_note is not None:  # długi tekst — pod tabelą (komórki z liczbami się nie zawijają)
        ai_txt = offer.ai_note or "brak uwag"
        if offer.ai_flags:
            ai_txt += " · flagi: " + ", ".join(f.label for f in offer.ai_flags)
        parts.append(f"<p><b>Opis wg AI (Ollama):</b> {escape(ai_txt)}</p>")

    if price_history and len(price_history) > 1:
        parts.append("<h3>Historia ceny</h3><table class='calc'>")
        for dt, price in price_history:
            parts.append(_row(_fmt_dt(dt), zl(price)))
        parts.append("</table>")

    parts.append("<h3>Opis ogłoszenia" + (' <span class="muted">(pobrany ze strony oferty)</span>'
                                           if offer.desc_from_page else "") + "</h3>")
    parts.append(f'<div class="desc">{escape(raw.description or "(brak opisu)")}</div>')
    parts.append("</body></html>")
    return "".join(parts)
