"""Profile powiadomień Telegram: kilka zestawów filtrów, każdy z nazwą i włącznikiem.

Filtry profilu są niezależne od filtrów tabeli w programie; każdy jest opcjonalny (puste / 0 = bez ograniczeń).
Oferta pasująca do kilku profili trafia na Telegram raz — z listą profili, do których pasuje.

Kolejność sprawdzania (pierwszy niespełniony warunek to „powód odrzucenia” — widać go w podglądzie profilu):
tryb → werdykt → zysk → zysk/h → ocena → model → pamięć → cena → stan → portal → odległość i wysyłka → kraj →
magazyn → ryzyko oszustwa → poważne flagi → wykluczone słowa → „tylko z Wybranych”.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta

from .models import Condition, Mode, Offer, OfferStatus, RedFlag, Valuation, Verdict
from .text import normalize

RISK_ORDER = {"low": 0, "medium": 1, "high": 2}
RISK_LABELS = {"low": "tylko niskie", "medium": "niskie i średnie", "high": "wszystkie (także wysokie)"}
SHIPPING = {"": "obojętne", "yes": "tylko z wysyłką", "no": "tylko odbiór osobisty"}
COUNTRY = {"pl": "tylko Polska", "all": "Polska + zagranica"}
MODES = {"": "jak w programie", Mode.REPAIR.value: Mode.REPAIR.label, Mode.RESELL.value: Mode.RESELL.label}
NOTIFY_VERDICTS = (Verdict.BUY.value, Verdict.NEGOTIATE.value, Verdict.VERIFY.value)


@dataclass
class NotifyProfile:
    id: int | None = None
    name: str = "Nowy profil"
    enabled: bool = True
    # --- filtry (puste / 0 = bez ograniczeń) ---
    mode: str = ""  # "" = tryb z programu | repair | resell — wycena w tym trybie
    models: list[str] = field(default_factory=list)
    storages: list[int] = field(default_factory=list)
    price_min: float = 0.0
    price_max: float = 0.0
    min_profit: float = 0.0
    min_profit_per_hour: float = 0.0
    verdicts: list[str] = field(default_factory=lambda: [Verdict.BUY.value, Verdict.NEGOTIATE.value])
    min_score: int = 0
    conditions: list[str] = field(default_factory=list)  # wartości Condition
    sources: list[str] = field(default_factory=list)
    radius_km: int = 0  # od Twojej miejscowości (Ustawienia → lokalizacja)
    radius_keeps_shipping: bool = True  # dalsze oferty z wysyłką też
    shipping: str = ""  # "" | yes | no
    country: str = "all"  # pl | all
    only_with_parts: bool = False
    max_risk: str = "low"  # low | medium | high
    skip_hard_flags: bool = True  # iCloud, IMEI, podróbka
    exclude_words: list[str] = field(default_factory=list)
    require_picked: bool = False  # tylko oferty, które automatycznie trafiają do „Wybrane”
    # --- cisza nocna i limit (puste = globalne z Ustawień) ---
    own_quiet: bool = False
    quiet_enabled: bool = True
    quiet_start: int = 22
    quiet_end: int = 7
    max_per_hour: int = 0  # 0 = tylko limit globalny

    def filters(self) -> dict:
        data = asdict(self)
        for key in ("id", "name", "enabled"):
            data.pop(key)
        return data

    @classmethod
    def from_filters(cls, pid: int | None, name: str, enabled: bool, data: dict) -> NotifyProfile:
        known = {k: v for k, v in (data or {}).items() if k in cls.__dataclass_fields__ and k not in ("id", "name",
                                                                                                      "enabled")}
        return cls(id=pid, name=name, enabled=enabled, **known)

    def evaluation_mode(self, app_mode: Mode) -> Mode:
        return Mode(self.mode) if self.mode in (Mode.REPAIR.value, Mode.RESELL.value) else app_mode


def _risk(val: Valuation) -> str:
    return getattr(val.risk, "level", "low") or "low"


def reject_reason(offer: Offer, val: Valuation, p: NotifyProfile, *, picked: bool | None = None) -> str | None:
    """Pierwszy niespełniony filtr profilu (krótki opis) albo ``None`` = oferta pasuje.
    ``picked`` — czy oferta automatycznie trafia do „Wybrane” (potrzebne tylko przy ``require_picked``)."""
    raw, parsed = offer.raw, offer.parsed
    if p.verdicts and val.verdict.value not in p.verdicts:
        return f"werdykt {val.verdict.value}"
    if p.min_profit and (val.expected_profit is None or val.expected_profit < p.min_profit):
        return "zysk poniżej progu"
    if p.min_profit_per_hour and (val.profit_per_hour is None or val.profit_per_hour < p.min_profit_per_hour):
        return "zysk na godzinę poniżej progu"
    if p.min_score and val.score < p.min_score:
        return "ocena poniżej progu"
    if p.models and parsed.model not in p.models:
        return "model"
    if p.storages and parsed.storage_gb not in p.storages:
        return "pamięć"
    if p.price_min and offer.price < p.price_min:
        return "cena poniżej zakresu"
    if p.price_max and offer.price > p.price_max:
        return "cena powyżej zakresu"
    if p.conditions and parsed.condition.value not in p.conditions:
        return "stan"
    if p.sources and raw.source not in p.sources:
        return "portal"
    if p.shipping == "yes" and raw.shipping_available is not True:
        return "bez wysyłki"
    if p.shipping == "no" and raw.shipping_available is not False:
        return "z wysyłką"
    if p.radius_km:
        ships = p.radius_keeps_shipping and raw.shipping_available is True
        if not ships and (offer.distance_km is None or offer.distance_km > p.radius_km):
            return "za daleko"
    if p.country == "pl" and RedFlag.FOREIGN_SELLER in val.flags:
        return "z zagranicy"
    if p.only_with_parts and not val.parts_in_stock:
        return "brak części w magazynie"
    if RISK_ORDER.get(_risk(val), 0) > RISK_ORDER.get(p.max_risk, 0):
        return f"ryzyko oszustwa {getattr(val.risk, 'label', _risk(val))}"
    if p.skip_hard_flags and val.has_hard_flag:
        return "poważna flaga"
    if p.exclude_words:
        hay = normalize(f"{raw.title} {raw.description or ''}")
        for word in p.exclude_words:
            w = normalize(word)
            if w and w in hay:
                return f"słowo „{word}”"
    if p.require_picked and picked is False:
        return "poza „Wybrane”"
    return None


def matches(offer: Offer, val: Valuation, p: NotifyProfile, *, picked: bool | None = None) -> bool:
    return reject_reason(offer, val, p, picked=picked) is None


def base_ok(offer: Offer) -> bool:
    """Warunki wspólne dla wszystkich profili: aktywna, nie ukryta, nie usunięta ręcznie z „Wybrane”."""
    return offer.active and offer.status is not OfferStatus.HIDDEN and not offer.pick_excluded


def matching_profiles(offer: Offer, valuate, profiles: list[NotifyProfile], app_mode: Mode, *,
                      picked=None) -> list[tuple[NotifyProfile, Valuation]]:
    """Włączone profile, do których pasuje oferta — każda oferta jest potem wysyłana raz (deduplikacja).
    ``valuate(offer, mode)`` — wycena w trybie profilu (wyniki warto zapamiętywać: kilka profili, jeden tryb);
    ``picked(offer, val)`` — czy oferta automatycznie trafia do „Wybrane”."""
    if not base_ok(offer):
        return []
    out = []
    for p in profiles:
        if not p.enabled:
            continue
        val = valuate(offer, p.evaluation_mode(app_mode))
        is_in = picked(offer, val) if (p.require_picked and picked is not None) else None
        if matches(offer, val, p, picked=is_in):
            out.append((p, val))
    return out


# ------------------------------------------------------------ cisza i limit ---

def in_quiet(local: datetime, enabled: bool, start: int, end: int) -> bool:
    start, end = start % 24, end % 24
    if not enabled or start == end:
        return False
    h = local.hour
    return start <= h < end if start < end else (h >= start or h < end)


def quiet_end(local: datetime, end: int) -> datetime:
    t = local.replace(hour=end % 24, minute=0, second=0, microsecond=0)
    return t if t > local else t + timedelta(days=1)


def profile_quiet(p: NotifyProfile | None, settings) -> tuple[bool, int, int]:
    """(włączona, od, do) — cisza profilu albo globalna."""
    if p is not None and p.own_quiet:
        return p.quiet_enabled, p.quiet_start, p.quiet_end
    return settings.telegram_quiet_enabled, settings.telegram_quiet_start, settings.telegram_quiet_end


# ------------------------------------------------------------------- opis ---

def describe(p: NotifyProfile) -> str:
    """Krótki opis filtrów (lista profili, /profile w Telegramie)."""
    from .catalog import generations

    parts = []
    if p.mode:
        parts.append(MODES.get(p.mode, p.mode))
    if p.verdicts and set(p.verdicts) != set(NOTIFY_VERDICTS):
        parts.append("/".join(p.verdicts))
    if p.models:
        gens = [g for g, ms in generations().items() if set(ms) <= set(p.models)]
        whole = {m for g in gens for m in generations()[g]}
        rest = [m.removeprefix("iPhone ") for m in p.models if m not in whole]
        parts.append("iPhone " + ", ".join([*(f"{g} (cała gen.)" for g in gens), *rest]))
    if p.storages:
        parts.append("/".join(f"{s} GB" for s in sorted(p.storages)))
    if p.price_min or p.price_max:
        if p.price_min and p.price_max:
            parts.append(f"cena {p.price_min:.0f}–{p.price_max:.0f} zł")
        else:
            parts.append(f"cena do {p.price_max:.0f} zł" if p.price_max else f"cena od {p.price_min:.0f} zł")
    if p.min_profit:
        parts.append(f"zysk ≥ {p.min_profit:.0f} zł")
    if p.min_profit_per_hour:
        parts.append(f"≥ {p.min_profit_per_hour:.0f} zł/h")
    if p.min_score:
        parts.append(f"ocena ≥ {p.min_score}")
    if p.conditions:
        labels = {c.value: c.label for c in Condition}
        parts.append(", ".join(labels.get(c, c) for c in p.conditions))
    if p.sources:
        from ..sources import SOURCE_NAMES

        parts.append(", ".join(SOURCE_NAMES.get(s, s) for s in p.sources))
    if p.radius_km:
        parts.append(f"do {p.radius_km} km" + (" (dalej z wysyłką)" if p.radius_keeps_shipping else ""))
    if p.shipping:
        parts.append(SHIPPING[p.shipping])
    if p.country == "pl":
        parts.append("tylko Polska")
    if p.only_with_parts:
        parts.append("mam część")
    parts.append(f"ryzyko: {RISK_LABELS.get(p.max_risk, p.max_risk)}")
    if p.exclude_words:
        parts.append("bez: " + ", ".join(p.exclude_words))
    if p.require_picked:
        parts.append("tylko z „Wybrane”")
    if p.own_quiet:
        parts.append(f"cisza {p.quiet_start}–{p.quiet_end}" if p.quiet_enabled else "bez ciszy nocnej")
    if p.max_per_hour:
        parts.append(f"≤ {p.max_per_hour}/h")
    return " · ".join(parts)


def from_legacy(settings) -> NotifyProfile:
    """Dotychczasowe ustawienia powiadomień → profil „Domyślny” (działa dokładnie tak jak wcześniej):
    oferta automatycznie w „Wybrane” + kryteria Telegrama; wysokie ryzyko oszustwa pomijane (jak dotąd)."""
    c = settings.telegram_criteria
    return NotifyProfile(name="Domyślny", enabled=bool(c.enabled), verdicts=list(c.verdicts),
                         min_profit=float(c.min_profit or 0), min_score=int(c.min_score or 0),
                         skip_hard_flags=bool(c.skip_hard_flags), max_risk="medium", country="all",
                         require_picked=True)
