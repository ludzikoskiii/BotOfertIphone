"""Profile powiadomień Telegram na telefonie: lista z włącznikami i pauzą, edycja wszystkich filtrów z podglądem
„z ostatnich 24 godzin ten profil wysłałby X powiadomień” i testowym powiadomieniem."""
from __future__ import annotations

from html import escape

from ..core.catalog import ALL_STORAGES, format_storage, generations
from ..core.models import Condition
from ..core.notify_profiles import COUNTRY, MODES, NOTIFY_VERDICTS, RISK_LABELS, SHIPPING, NotifyProfile, describe
from ..sources import SOURCE_NAMES
from .pages import _section_header, card, page

NOTIFY_CSS = """<style>.checks{display:flex;flex-wrap:wrap;gap:6px 14px;margin:4px 0}
.checks label,label.chk{display:flex;align-items:center;gap:6px;font-size:15px;color:var(--text)}
.checks input,label.chk input{width:auto;margin:0}.field{display:flex;flex-direction:column;gap:3px;margin:8px 0}
.field>span{font-size:13px;color:var(--muted)}.pair{display:grid;grid-template-columns:1fr 1fr;gap:8px}
.gen{border-top:1px solid var(--line);padding:6px 0}.gen>label.chk{font-weight:700}
.gen .checks{margin-left:26px}.gen .checks label{font-size:14px}h3{margin:4px 0 6px;font-size:16px}
.bar{display:flex;flex-wrap:wrap;gap:8px;margin:8px 0}.bar form{margin:0}
.save{position:sticky;bottom:0;background:var(--bg);padding:8px 0;display:flex;gap:6px;border-top:1px solid var(--line)}
.save button{flex:1;padding:10px 4px;font-size:14px;white-space:nowrap}.toggle button{white-space:nowrap}
.ok{background:var(--buy-bg);color:var(--buy);padding:8px 10px;border-radius:10px}</style>"""

GEN_JS = """<script>
document.querySelectorAll('input.gen').forEach(function(g){g.addEventListener('change',function(){
document.querySelectorAll('input[data-gen="'+g.value+'"]').forEach(function(m){m.checked=g.checked})})});
document.querySelectorAll('input[data-gen]').forEach(function(m){m.addEventListener('change',function(){
var all=document.querySelectorAll('input[data-gen="'+m.dataset.gen+'"]'),on=0;all.forEach(function(x){if(x.checked)on++});
var g=document.querySelector('input.gen[value="'+m.dataset.gen+'"]');g.checked=on===all.length;
g.indeterminate=on>0&&on<all.length})});
document.querySelectorAll('input.gen').forEach(function(g){var all=document.querySelectorAll(
'input[data-gen="'+g.value+'"]'),on=0;all.forEach(function(x){if(x.checked)on++});g.indeterminate=on>0&&on<all.length});
</script>"""

PAUSE_OPTIONS = (("1", "⏸ 1 h"), ("2", "⏸ 2 h"), ("8", "⏸ 8 h"), ("0", "⏸ do wznowienia"))


def _gen_label(gen: str) -> str:
    return {"X": "iPhone X / XR / XS", "SE": "iPhone SE"}.get(gen, f"iPhone {gen}")


def _checks(name: str, options: dict[str, str], selected) -> str:
    chosen = {str(x) for x in selected}
    return '<div class="checks">' + "".join(
        f'<label><input type="checkbox" name="{name}" value="{escape(k)}"{" checked" if k in chosen else ""}>'
        f"{escape(v)}</label>" for k, v in options.items()) + "</div>"


def _check(name: str, label: str, on: bool) -> str:
    return f'<label class="chk"><input type="checkbox" name="{name}" value="1"{" checked" if on else ""}>' \
           f"{escape(label)}</label>"


def _select(name: str, options: dict[str, str], value: str) -> str:
    return f'<select name="{name}">' + "".join(
        f'<option value="{escape(k)}"{" selected" if k == value else ""}>{escape(v)}</option>'
        for k, v in options.items()) + "</select>"


def _num(name: str, label: str, value: float, hint: str = "puste = bez limitu", step: str = "1") -> str:
    shown = "" if not value else (f"{value:.0f}" if float(value).is_integer() else str(value))
    return (f'<label class="field"><span>{escape(label)}</span><input type="number" inputmode="decimal" min="0" '
            f'step="{step}" name="{name}" value="{shown}" placeholder="{escape(hint)}"></label>')


def _hours(name: str, value: int) -> str:
    return _select(name, {str(h): f"{h}:00" for h in range(24)}, str(value))


# ----------------------------------------------------------------- lista ---

def notify_page(profiles: list[NotifyProfile], week: dict[int, int], *, csrf: str, paused: str | None,
                configured: bool, flash: str = "") -> str:
    parts = [NOTIFY_CSS]
    if flash:
        parts.append(f'<p class="ok">{escape(flash)}</p>')
    if not configured:
        parts.append('<p class="note">Telegram nie jest jeszcze połączony — token bota i chat ID wpisz w programie '
                     "na komputerze (Ustawienia → Powiadomienia).</p>")
    state = f"⏸ Powiadomienia {escape(paused)}." if paused else "🔔 Powiadomienia działają."
    bar = "".join(f'<form method="post" action="/powiadomienia"><input type="hidden" name="csrf" value="{csrf}">'
                  f'<input type="hidden" name="a" value="pause"><input type="hidden" name="h" value="{h}">'
                  f"<button>{label}</button></form>" for h, label in PAUSE_OPTIONS)
    if paused:
        bar = (f'<form method="post" action="/powiadomienia"><input type="hidden" name="csrf" value="{csrf}">'
               '<input type="hidden" name="a" value="resume"><button class="primary">▶ Wznów</button></form>') + bar
    parts.append(f'<div class="box"><b>{state}</b><div class="bar">{bar}</div></div>')
    if not profiles:
        parts.append('<p class="muted">Brak profili — dodaj pierwszy.</p>')
    for p in profiles:
        toggle = (f'<form class="toggle" method="post" action="/powiadomienia"><input type="hidden" name="csrf" value="{csrf}">'
                  f'<input type="hidden" name="a" value="toggle"><input type="hidden" name="id" value="{p.id}">'
                  f'<button class="{"primary" if p.enabled else ""}">{"✅ włączony" if p.enabled else "⛔ wyłączony"}'
                  "</button></form>")
        parts.append(f'<div class="box"><div class="row"><a class="model" href="/powiadomienia/{p.id}">'
                     f"{escape(p.name)}</a>{toggle}</div>"
                     f'<div class="muted">{escape(describe(p))}</div>'
                     f'<div class="row"><span class="muted">Wysłane w 7 dni: <b>{week.get(p.id, 0)}</b></span>'
                     f'<a href="/powiadomienia/{p.id}">Edytuj →</a></div></div>')
    parts.append('<a class="btn primary more" href="/powiadomienia/nowy">＋ Dodaj profil</a>')
    parts.append('<p class="muted">Oferta pasująca do kilku profili przychodzi raz — z nazwami profili. '
                 "W Telegramie działają też komendy /pauza 2h, /wznow, /profile i /status.</p>")
    return page("PhoneBot — powiadomienia", "".join(parts), header=_section_header("telegram", "Powiadomienia"))


# ---------------------------------------------------------------- edycja ---

def profile_page(p: NotifyProfile, *, csrf: str, preview=None, flash: str = "", error: str = "") -> str:
    target = f"/powiadomienia/{p.id}" if p.id else "/powiadomienia/nowy"
    parts = [NOTIFY_CSS]
    if error:
        parts.append(f'<p class="note">{escape(error)}</p>')
    if flash:
        parts.append(f'<p class="ok">{escape(flash)}</p>')
    if preview is not None:
        cards = "".join(card(o, v) for o, v in preview.matched[:10])
        more = f'<p class="muted">…i jeszcze {preview.count - 10}.</p>' if preview.count > 10 else ""
        parts.append(f'<div class="box"><b>Podgląd</b><p id="preview">{escape(preview.summary())}</p></div>'
                     f"{cards}{more}")
    gens = []
    for gen, models in generations().items():
        on = bool(models) and set(models) <= set(p.models)
        kids = "".join(f'<label><input type="checkbox" name="models" value="{escape(m)}" data-gen="{escape(gen)}"'
                       f'{" checked" if m in p.models else ""}>{escape(m.removeprefix("iPhone "))}</label>'
                       for m in models)
        gens.append(f'<div class="gen"><label class="chk"><input type="checkbox" class="gen" name="gen" '
                    f'value="{escape(gen)}"{" checked" if on else ""}>{escape(_gen_label(gen))} — cała generacja'
                    f'</label><div class="checks">{kids}</div></div>')
    storages = {str(s): format_storage(s) for s in ALL_STORAGES if s >= 32}
    form = f"""<form method="post" action="{target}"><input type="hidden" name="csrf" value="{csrf}">
<div class="box"><h3>Podstawowe</h3>
<label class="field"><span>Nazwa</span><input name="name" value="{escape(p.name)}" required maxlength="60"></label>
{_check("enabled", "Profil włączony", p.enabled)}
<label class="field"><span>Tryb wyceny</span>{_select("mode", MODES, p.mode)}</label>
<div class="field"><span>Werdykty (puste = wszystkie)</span>{_checks("verdicts", {v: v for v in NOTIFY_VERDICTS},
                                                                         p.verdicts)}</div>
<div class="pair">{_num("price_min", "Cena od (zł)", p.price_min)}{_num("price_max", "Cena do (zł)", p.price_max)}</div>
<div class="pair">{_num("min_profit", "Min. szacowany zysk (zł)", p.min_profit, "bez progu")}
{_num("min_profit_per_hour", "Min. zysk na godzinę (zł/h)", p.min_profit_per_hour, "bez progu")}</div>
{_num("min_score", "Min. ocena (0–100)", p.min_score, "bez progu")}
</div>
<div class="box"><h3>Telefon</h3><p class="muted">Modele (puste = wszystkie). Zaznaczenie generacji wybiera wszystkie
jej modele.</p>{"".join(gens)}
<div class="field"><span>Pamięć (puste = każda)</span>{_checks("storages", storages, p.storages)}</div>
<div class="field"><span>Stan (puste = każdy)</span>{_checks("conditions", {c.value: c.label for c in Condition},
                                                            p.conditions)}</div>
</div>
<div class="box"><h3>Skąd</h3>
<div class="field"><span>Portale (puste = wszystkie)</span>{_checks("sources", dict(SOURCE_NAMES), p.sources)}</div>
{_num("radius_km", "Promień od Twojej miejscowości (km)", p.radius_km)}
{_check("radius_keeps_shipping", "dalsze oferty z wysyłką też", p.radius_keeps_shipping)}
<label class="field"><span>Wysyłka</span>{_select("shipping", SHIPPING, p.shipping)}</label>
<label class="field"><span>Kraj</span>{_select("country", COUNTRY, p.country)}</label>
</div>
<div class="box"><h3>Bezpieczeństwo i inne</h3>
<label class="field"><span>Maks. ryzyko oszustwa</span>{_select("max_risk", RISK_LABELS, p.max_risk)}</label>
{_check("skip_hard_flags", "pomijaj poważne flagi (iCloud, IMEI, podróbka)", p.skip_hard_flags)}
{_check("only_with_parts", "tylko z częścią w magazynie", p.only_with_parts)}
{_check("require_picked", "tylko oferty, które trafiają do „Wybrane”", p.require_picked)}
<label class="field"><span>Wykluczone słowa (po przecinku, tytuł lub opis)</span>
<input name="exclude_words" value="{escape(", ".join(p.exclude_words))}" placeholder="np. icloud, atrapa"></label>
</div>
<div class="box"><h3>Cisza nocna i limit (opcjonalnie)</h3>
{_check("own_quiet", "własna cisza nocna (inaczej — globalna z ustawień)", p.own_quiet)}
{_check("quiet_enabled", "nie wysyłaj w godzinach", p.quiet_enabled)}
<div class="pair"><label class="field"><span>od</span>{_hours("quiet_start", p.quiet_start)}</label>
<label class="field"><span>do</span>{_hours("quiet_end", p.quiet_end)}</label></div>
{_num("max_per_hour", "Limit wiadomości na godzinę dla profilu", p.max_per_hour, "tylko globalny")}
</div>
<div class="save"><button name="a" value="preview">🔍 Podgląd</button>
<button class="primary" name="a" value="save">💾 Zapisz</button>
<button name="a" value="test" title="Wyślij testowe powiadomienie z tego profilu">📨 Test</button>
{'<button name="a" value="delete" formnovalidate>🗑 Usuń</button>' if p.id else ""}</div>
</form>"""
    parts.append(form)
    parts.append('<a class="more" href="/powiadomienia">← Wszystkie profile</a>')
    return page(f"PhoneBot — {p.name}", "".join(parts) + GEN_JS,
                header=_section_header("telegram", p.name if p.id else "Nowy profil"))


# ------------------------------------------------------- odczyt formularza ---

def _float(form: dict[str, list[str]], key: str, top: float) -> float:
    raw = (form.get(key) or [""])[0].replace(",", ".").replace(" ", "").strip()
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError as e:
        raise ValueError(f"niepoprawna liczba w polu „{key}”") from e
    return max(0.0, min(value, top))


def profile_from_form(form: dict[str, list[str]], base: NotifyProfile) -> NotifyProfile:
    """Formularz z telefonu → profil (pola nieznane są pomijane; brak pola wyboru = odznaczone)."""
    def one(key: str, default: str = "") -> str:
        return (form.get(key) or [default])[0].strip()

    def many(key: str, allowed) -> list[str]:
        allowed = {str(a) for a in allowed}
        out = []
        for v in form.get(key, []):
            if v in allowed and v not in out:
                out.append(v)
        return out

    gens = generations()
    known = [m for ms in gens.values() for m in ms]
    chosen = set(many("models", known))
    for g in many("gen", gens):
        chosen |= set(gens[g])
    p = NotifyProfile(id=base.id)
    p.name = one("name")[:60] or "Profil"
    p.enabled = one("enabled") == "1"
    p.mode = one("mode") if one("mode") in MODES else ""
    p.verdicts = many("verdicts", NOTIFY_VERDICTS)
    p.price_min, p.price_max = _float(form, "price_min", 100_000), _float(form, "price_max", 100_000)
    p.min_profit = _float(form, "min_profit", 50_000)
    p.min_profit_per_hour = _float(form, "min_profit_per_hour", 10_000)
    p.min_score = int(_float(form, "min_score", 100))
    p.models = [m for m in known if m in chosen]
    p.storages = sorted(int(s) for s in many("storages", ALL_STORAGES))
    p.conditions = many("conditions", [c.value for c in Condition])
    p.sources = many("sources", SOURCE_NAMES)
    p.radius_km = int(_float(form, "radius_km", 2000))
    p.radius_keeps_shipping = one("radius_keeps_shipping") == "1"
    p.shipping = one("shipping") if one("shipping") in SHIPPING else ""
    p.country = one("country") if one("country") in COUNTRY else "all"
    p.max_risk = one("max_risk") if one("max_risk") in RISK_LABELS else "low"
    p.skip_hard_flags = one("skip_hard_flags") == "1"
    p.only_with_parts = one("only_with_parts") == "1"
    p.require_picked = one("require_picked") == "1"
    p.exclude_words = [w.strip() for w in one("exclude_words").split(",") if w.strip()][:30]
    p.own_quiet = one("own_quiet") == "1"
    p.quiet_enabled = one("quiet_enabled") == "1"
    p.quiet_start = int(_float(form, "quiet_start", 23))
    p.quiet_end = int(_float(form, "quiet_end", 23))
    p.max_per_hour = int(_float(form, "max_per_hour", 60))
    return p
