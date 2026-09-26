"""Strony wersji na telefon (HTML renderowany w Pythonie, bez zewnętrznych bibliotek i CDN-ów)."""
from __future__ import annotations

from html import escape
from urllib.parse import urlencode

from ..core.catalog import format_storage
from ..core.messages import NEGOTIATION_STYLES, STYLE_NAMES, TEMPLATE_KEYS, TEMPLATE_NAMES, compose, opening_price, zl
from ..core.models import Offer, OfferStatus, Severity, Valuation, Verdict
from ..core.selection import pick_reason
from ..core.settings import Settings
from ..core.sorting import FIELDS, describe
from ..ml.seed_data import LABEL_NAMES
from ..sources import SOURCE_NAMES

VERDICT_CLASS = {Verdict.BUY: "buy", Verdict.NEGOTIATE: "neg", Verdict.VERIFY: "ver", Verdict.SKIP: "skip"}

CSS = """
:root{--bg:#f4f5f7;--card:#fff;--text:#1d1f23;--muted:#6b7280;--line:#e3e5e8;--accent:#1c7ed6;
--buy:#2b8a3e;--buy-bg:#d3f9d8;--neg:#a15c00;--neg-bg:#fff3bf;--ver:#495057;--ver-bg:#e9ecef;
--skip:#c92a2a;--skip-bg:#ffe3e3;--pos:#2b8a3e;--negv:#c92a2a}
@media (prefers-color-scheme:dark){:root{--bg:#141517;--card:#1f2124;--text:#e9ecef;--muted:#9aa0a6;
--line:#2e3136;--accent:#4dabf7;--buy:#8ce99a;--buy-bg:#1f3a26;--neg:#ffd43b;--neg-bg:#3d3312;--ver:#ced4da;
--ver-bg:#2e3136;--skip:#ff8787;--skip-bg:#3d1f1f;--pos:#8ce99a;--negv:#ff8787}}
*{box-sizing:border-box}body{margin:0;font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
background:var(--bg);color:var(--text);-webkit-text-size-adjust:100%}
a{color:var(--accent)}header{position:sticky;top:0;z-index:5;background:var(--card);
border-bottom:1px solid var(--line);padding:8px 12px 6px}
h1{font-size:18px;margin:0 0 6px}.tabs{display:flex;gap:6px}.tabs a{flex:1;text-align:center;padding:8px;
border-radius:10px;text-decoration:none;color:var(--text);background:var(--bg);font-weight:600}
.tabs a.on{background:var(--accent);color:#fff}main{padding:10px 10px 40px;max-width:720px;margin:auto}
details.panel{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:8px 10px;margin-bottom:10px}
details.panel summary{font-weight:600;cursor:pointer}
form.grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:8px}
form.grid label{font-size:13px;color:var(--muted);display:flex;flex-direction:column;gap:3px}
input,select,textarea,button{font:inherit;color:inherit}
input,select,textarea{width:100%;padding:8px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
button,.btn{display:inline-block;padding:10px 14px;border-radius:10px;border:1px solid var(--line);
background:var(--card);text-decoration:none;color:var(--text);font-weight:600;text-align:center}
button.primary,.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.card{display:block;background:var(--card);border:1px solid var(--line);border-radius:12px;padding:10px 12px;
margin-bottom:8px;text-decoration:none;color:var(--text)}.card.out{opacity:.55}
.row{display:flex;justify-content:space-between;align-items:center;gap:8px}
.model{font-weight:700;font-size:17px}.muted{color:var(--muted);font-size:13px}
.price{font-weight:700;white-space:nowrap}.pos{color:var(--pos);font-weight:700}.negv{color:var(--negv);font-weight:700}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-weight:700;font-size:13px;white-space:nowrap}
.badge.buy{background:var(--buy-bg);color:var(--buy)}.badge.neg{background:var(--neg-bg);color:var(--neg)}
.badge.ver{background:var(--ver-bg);color:var(--ver)}.badge.skip{background:var(--skip-bg);color:var(--skip)}
.photo{width:100%;max-height:340px;object-fit:contain;border-radius:12px;background:var(--card)}
table.kv{width:100%;border-collapse:collapse}table.kv td{padding:5px 0;border-bottom:1px solid var(--line)}
table.kv td:last-child{text-align:right;font-weight:600}.actions{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.actions form{margin:0}.actions button{width:100%}.box{background:var(--card);border:1px solid var(--line);
border-radius:12px;padding:10px 12px;margin:10px 0}.flag{color:var(--negv)}.soft{color:var(--neg)}
textarea{min-height:210px}.note{background:var(--neg-bg);color:var(--neg);padding:8px 10px;border-radius:10px}
.more{display:block;text-align:center;margin:12px 0}
"""

COPY_JS = """
function copyMsg(){var t=document.getElementById('msg');t.focus();t.select();t.setSelectionRange(0,99999);
var ok=false;try{ok=document.execCommand('copy')}catch(e){}
if(navigator.clipboard&&window.isSecureContext){navigator.clipboard.writeText(t.value).then(function(){done()},
function(){if(ok)done()})}else if(ok){done()}else{document.getElementById('copied').textContent=
'Zaznaczono tekst — skopiuj go ręcznie.'}}
function done(){document.getElementById('copied').textContent='✓ Skopiowano — wklej w portalu.'}
if('serviceWorker' in navigator&&window.isSecureContext){navigator.serviceWorker.register('/sw.js')}
"""


def page(title: str, body: str, *, header: str = "") -> str:
    return (f'<!doctype html><html lang="pl"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
            f'<meta name="theme-color" content="#2b8a3e"><link rel="manifest" href="/manifest.webmanifest">'
            f'<link rel="apple-touch-icon" href="/icon-192.png"><meta name="apple-mobile-web-app-capable" content="yes">'
            f"<title>{escape(title)}</title><style>{CSS}</style></head><body>{header}<main>{body}</main>"
            f"<script>{COPY_JS}</script></body></html>")


def login_page(error: str = "", locked_for: int = 0, next_url: str = "/") -> str:
    msg = ""
    if locked_for:
        msg = f'<p class="note">Za dużo błędnych prób. Spróbuj za {locked_for} s.</p>'
    elif error:
        msg = f'<p class="note">{escape(error)}</p>'
    body = (f'<div class="box"><h1>PhoneBot</h1><p class="muted">Podaj PIN ustawiony w programie na komputerze '
            f'(Ustawienia → Telefon).</p>{msg}<form method="post" action="/login">'
            f'<input type="hidden" name="next" value="{escape(next_url, quote=True)}">'
            f'<input name="pin" type="password" inputmode="numeric" autocomplete="current-password" autofocus '
            f'placeholder="PIN" required><p><button class="primary" style="width:100%">Zaloguj</button></p>'
            f"</form></div>")
    return page("PhoneBot — logowanie", body)


def _money(v: float | None) -> str:
    return "—" if v is None else zl(v)


def _profit(v: float | None) -> str:
    if v is None:
        return '<span class="muted">zysk —</span>'
    return f'<span class="{"pos" if v > 0 else "negv"}">{"+" if v > 0 else ""}{zl(v)}</span>'


def _name(offer: Offer) -> str:
    p = offer.parsed
    return (p.model or "iPhone") + (f" {format_storage(p.storage_gb)}" if p.storage_gb else "")


def _place(offer: Offer) -> str:
    city = offer.raw.city or "—"
    return f"{city} ({offer.distance_km:.0f} km)" if offer.distance_km is not None else city


def card(offer: Offer, val: Valuation) -> str:
    marks = ("⌛ " if not offer.active else "") + ("★ " if offer.status is OfferStatus.WATCHED else "")
    flags = f' · <span class="flag">⚑{len(set(val.flags))}</span>' if val.flags else ""
    portal = SOURCE_NAMES.get(offer.raw.source, offer.raw.source)
    return (f'<a class="card{" out" if not offer.active else ""}" href="/oferta/{offer.id}">'
            f'<div class="row"><span class="model">{marks}{escape(_name(offer))}</span>'
            f'<span class="badge {VERDICT_CLASS[val.verdict]}">{escape(val.verdict.value)}</span></div>'
            f'<div class="row"><span class="price">{zl(offer.price)}</span>{_profit(val.expected_profit)}</div>'
            f'<div class="muted">{escape(portal)} · {escape(_place(offer))} · ocena {val.score}{flags}</div></a>')


def list_page(rows: list[tuple[Offer, Valuation]], *, list_key: str, counts: dict[str, int], params: dict,
              sort_spec, models: list[str], shown: int, total: int, csrf: str) -> str:
    def link(**change) -> str:
        q = {**params, **change}
        return "/?" + urlencode({k: v for k, v in q.items() if v not in ("", None)})

    all_url, picked_url = escape(link(lista="all", limit="")), escape(link(lista="picked", limit=""))
    all_on, picked_on = ("on", "") if list_key == "all" else ("", "on")
    tabs = (f'<nav class="tabs"><a class="{all_on}" href="{all_url}">Wszystkie ({counts.get("all", 0)})</a>'
            f'<a class="{picked_on}" href="{picked_url}">Wybrane ({counts.get("picked", 0)})</a></nav>')
    header = (f'<header><div class="row"><h1>PhoneBot</h1><form method="post" action="/logout">'
              f'<input type="hidden" name="csrf" value="{csrf}"><button>Wyloguj</button></form></div>{tabs}</header>')
    sort_opts = "".join(f'<option value="{k}"{" selected" if sort_spec and sort_spec[0].field == k else ""}>'
                        f"{escape(f.label)}</option>" for k, f in FIELDS.items())
    first = sort_spec[0] if sort_spec else None
    order = first.order if first else "desc"
    model_opts = '<option value="">wszystkie</option>' + "".join(
        f'<option{" selected" if params.get("model") == m else ""}>{escape(m)}</option>' for m in models)
    portal_opts = '<option value="">wszystkie</option>' + "".join(
        f'<option value="{k}"{" selected" if params.get("portal") == k else ""}>{escape(n)}</option>'
        for k, n in SOURCE_NAMES.items())
    verdict_opts = '<option value="">wszystkie</option>' + "".join(
        f'<option{" selected" if params.get("werdykt") == v.value else ""}>{escape(v.value)}</option>' for v in Verdict)
    hidden = f'<input type="hidden" name="lista" value="{escape(list_key)}">'
    filters = (
        f'<details class="panel"{" open" if params.get("open") else ""}><summary>Sortowanie i filtry · '
        f'<span class="muted">{escape(describe(sort_spec))}</span></summary><form class="grid" method="get" action="/">'
        f"{hidden}<label>Sortuj<select name=\"sort\">{sort_opts}</select></label>"
        f'<label>Kierunek<select name="kier"><option value="desc"{" selected" if order == "desc" else ""}>malejąco ↓'
        f'</option><option value="asc"{" selected" if order == "asc" else ""}>rosnąco ↑</option></select></label>'
        f'<label>Model<select name="model">{model_opts}</select></label>'
        f'<label>Portal<select name="portal">{portal_opts}</select></label>'
        f'<label>Werdykt<select name="werdykt">{verdict_opts}</select></label>'
        f'<label>Cena do (zł)<input name="cena_max" inputmode="numeric" value="{escape(params.get("cena_max", ""))}">'
        f'</label><label>Min. zysk (zł)<input name="zysk_min" inputmode="numeric" '
        f'value="{escape(params.get("zysk_min", ""))}"></label>'
        f'<label>Szukaj<input name="q" value="{escape(params.get("q", ""))}"></label>'
        f'<button class="primary">Pokaż</button><a class="btn" href="/?lista={escape(list_key)}">Wyczyść</a>'
        f"</form></details>")
    cards = "".join(card(o, v) for o, v in rows) or '<p class="muted">Brak ofert dla tych filtrów.</p>'
    more = ""
    if shown < total:
        more = f'<a class="more btn" href="{escape(link(limit=str(shown + 100)))}">Pokaż więcej ({total - shown})</a>'
    return page("PhoneBot", filters + cards + more, header=header)


def details_page(offer: Offer, val: Valuation, settings: Settings, *, csrf: str, style: str | None = None,
                 key: str | None = None, back: str = "/") -> str:
    style = style if style in NEGOTIATION_STYLES else settings.negotiation_style
    if key not in TEMPLATE_KEYS:
        key = None
    key, text = compose(offer, val, settings.message_templates, key=key, style=style,
                        pickup_km=settings.pickup_radius_km)
    header = (f'<header><div class="row"><a class="btn" href="{escape(back)}">← Lista</a>'
              f'<span class="badge {VERDICT_CLASS[val.verdict]}">{escape(val.verdict.value)}</span></div></header>')
    parts = []
    if offer.raw.photos:
        parts.append(f'<img class="photo" src="{escape(offer.raw.photos[0], quote=True)}" alt="" loading="lazy" '
                     f'referrerpolicy="no-referrer">')
    parts.append(f"<h1>{escape(offer.raw.title)}</h1>")
    if not offer.active:
        parts.append('<p class="note">⌛ Nieaktualna — oferta zniknęła z portalu.</p>')
    reason = pick_reason(offer, val, settings.selection)
    if reason:
        parts.append(f'<p class="muted">✓ {escape(reason)}</p>')
    neg = val.negotiation
    rows = [("Cena", zl(offer.price)), ("Szacowany zysk", _profit(val.expected_profit)),
            ("Max cena zakupu", _money(val.max_buy_price)), ("Wartość rynkowa", _money(val.market.value)),
            ("Ocena", f"{val.score}/100"), ("Portal", escape(SOURCE_NAMES.get(offer.raw.source, offer.raw.source))),
            ("Miejsce", escape(_place(offer)))]
    if val.verdict is Verdict.NEGOTIATE:
        rows.insert(1, ("Proponowana cena", f"<b>{zl(opening_price(offer, val))}</b>"
                        + (f' <span class="muted">(maks. {zl(neg.max_price)})</span>' if neg.max_price else "")))
    parts.append('<table class="kv">' + "".join(f"<tr><td>{k}</td><td>{v}</td></tr>" for k, v in rows) + "</table>")
    if val.flags:
        items = "".join(f'<li class="{"flag" if f.severity is Severity.HARD else "soft"}">⚑ {escape(f.label)}</li>'
                        for f in dict.fromkeys(val.flags))
        parts.append(f'<div class="box"><b>Czerwone flagi</b><ul>{items}</ul></div>')
    if val.reasons:
        parts.append('<div class="box">' + "<br>".join(escape(r) for r in val.reasons[:4]) + "</div>")
    # wiadomość do sprzedającego
    tmpl_opts = "".join(f'<option value="{k}"{" selected" if k == key else ""}>{escape(TEMPLATE_NAMES[k])}</option>'
                        for k in TEMPLATE_KEYS)
    parts.append(
        f'<div class="box"><b>Wiadomość do sprzedającego</b>'
        f'<form method="get" action="/oferta/{offer.id}" class="grid"><label>Szablon<select name="szablon" '
        f'onchange="this.form.submit()">{tmpl_opts}</select></label><label>Styl negocjacji<select name="styl" '
        f'onchange="this.form.submit()">'
        + "".join(f'<option value="{k}"{" selected" if k == style else ""}>{escape(n)}</option>'
                  for k, n in STYLE_NAMES.items())
        + f'</select></label></form><textarea id="msg">{escape(text)}</textarea>'
        f'<p><button class="primary" type="button" onclick="copyMsg()" style="width:100%">📋 Kopiuj</button></p>'
        f'<p id="copied" class="muted"></p></div>')
    # akcje
    watched = offer.status is OfferStatus.WATCHED

    def action(name: str, label: str, extra: str = "") -> str:
        return (f'<form method="post" action="/oferta/{offer.id}/akcja"><input type="hidden" name="csrf" '
                f'value="{csrf}"><input type="hidden" name="a" value="{name}">{extra}<button>{label}</button></form>')

    not_phone = "".join(f'<option value="{k}">{escape(v)}</option>' for k, v in LABEL_NAMES.items() if k != "phone")
    parts.append(
        f'<p><a class="btn primary" style="width:100%" href="{escape(offer.raw.url, quote=True)}" target="_blank" '
        f'rel="noopener noreferrer">Otwórz ogłoszenie ↗</a></p><div class="actions">'
        + action("unwatch" if watched else "watch", "☆ Przestań obserwować" if watched else "★ Obserwuj")
        + action("unhide" if offer.status is OfferStatus.HIDDEN else "hide",
                 "Odkryj" if offer.status is OfferStatus.HIDDEN else "Ukryj")
        + action("phone", "✓ To jest telefon")
        + f'<form method="post" action="/oferta/{offer.id}/akcja"><input type="hidden" name="csrf" value="{csrf}">'
          f'<input type="hidden" name="a" value="not_phone"><select name="label">{not_phone}</select>'
          f"<button style=\"width:100%;margin-top:4px\">✖ To nie jest telefon</button></form></div>")
    if offer.raw.description:
        desc = offer.raw.description[:1200] + ("…" if len(offer.raw.description) > 1200 else "")
        parts.append(f'<details class="panel"><summary>Opis z ogłoszenia</summary><p>{escape(desc)}</p></details>')
    return page(_name(offer), "".join(parts), header=header)


def message_page(text: str, back: str = "/") -> str:
    return page("PhoneBot", f'<div class="box"><p>{escape(text)}</p><a class="btn primary" href="{escape(back)}">'
                            f"← Wróć</a></div>")
