"""Łączenie warstw oceny: reguły (etap 1) + klasyfikator tytułu + analiza zdjęcia + opis (Ollama, opcjonalnie).

Oferta w tabeli przeszła już reguły (to one uznały ją za telefon). Warstwy AI mogą to potwierdzić
albo podważyć — nigdy same nie odrzucają oferty, tylko ograniczają werdykt:

* warstwa **sprzeczna** (tytuł wygląda na akcesorium/część/kupię, zdjęcie pokazuje etui/szkło/pudełko
  z wysoką pewnością albo opis mówi, że to nie telefon) → flaga → najwyżej DO WERYFIKACJI,
* **niska pewność** (żadna warstwa nie potwierdza, że to telefon) → flaga → najwyżej DO WERYFIKACJI,
* co najmniej jedna potwierdza, a żadna nie przeczy → bez zmian.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.catalog import format_storage
from ..core.models import AiLayers, Defect, RedFlag

TEXT_NAMES = {"phone": "telefon", "accessory": "akcesorium", "part": "część", "wanted": "kupię / zamienię"}
PHOTO_NAMES = {"smartphone": "smartfon", "case": "etui", "screen_protector": "szkło ochronne", "box": "pudełko"}

AGREE, CONFLICT, UNSURE, MISSING = "zgodne", "sprzeczne", "niepewne", "brak"


@dataclass
class Layer:
    name: str
    state: str  # zgodne | sprzeczne | niepewne | brak
    summary: str  # np. „telefon 94% (akcesorium 4%, część 1%)”


@dataclass
class Combined:
    state: str  # zgodne | sprzeczne | niska pewność | brak danych
    flags: list[RedFlag] = field(default_factory=list)
    layers: list[Layer] = field(default_factory=list)
    note: str = ""


def _fmt(probs: dict[str, float], names: dict[str, str]) -> str:
    ordered = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
    if not ordered:
        return "—"
    (first, p), rest = ordered[0], ordered[1:]
    tail = ", ".join(f"{names.get(k, k)} {v:.0%}" for k, v in rest if v >= 0.01)
    return f"{names.get(first, first)} {p:.0%}" + (f" ({tail})" if tail else "")


def text_layer(ai: AiLayers | None, phone_conf: float, conflict_conf: float) -> Layer:
    if ai is None or not ai.text_probs:
        return Layer("Tytuł (klasyfikator)", MISSING, "brak wyniku")
    p_phone = ai.text_probs.get("phone", 0.0)
    other, p_other = max(((k, v) for k, v in ai.text_probs.items() if k != "phone"), key=lambda kv: kv[1],
                         default=("", 0.0))
    summary = _fmt(ai.text_probs, TEXT_NAMES)
    if p_other >= conflict_conf and p_other > p_phone:
        return Layer("Tytuł (klasyfikator)", CONFLICT, summary)
    if p_phone >= phone_conf:
        return Layer("Tytuł (klasyfikator)", AGREE, summary)
    return Layer("Tytuł (klasyfikator)", UNSURE, summary)


def photo_layer(ai: AiLayers | None, phone_conf: float, conflict_conf: float, *, enabled: bool = True) -> Layer:
    if not enabled:
        return Layer("Zdjęcie (CLIP)", MISSING, "analiza zdjęć wyłączona")
    if ai is None or not ai.photo_probs:
        reason = f"nie udało się: {ai.photo_error}" if ai is not None and ai.photo_error else "nie analizowano"
        return Layer("Zdjęcie (CLIP)", MISSING, reason)
    p_phone = ai.photo_probs.get("smartphone", 0.0)
    p_other = max((v for k, v in ai.photo_probs.items() if k != "smartphone"), default=0.0)
    summary = _fmt(ai.photo_probs, PHOTO_NAMES)
    if p_other >= conflict_conf:
        return Layer("Zdjęcie (CLIP)", CONFLICT, summary)
    if p_phone >= phone_conf:
        return Layer("Zdjęcie (CLIP)", AGREE, summary)
    return Layer("Zdjęcie (CLIP)", UNSURE, summary)


DESC_NAME = "Opis (Ollama)"


def desc_layer(ai: AiLayers | None, *, enabled: bool) -> Layer:
    """Opis przeczytany lokalnym modelem językowym — tylko oferty „DO WERYFIKACJI”."""
    if not enabled:
        return Layer(DESC_NAME, MISSING, "analiza opisów wyłączona")
    if ai is None or (not ai.desc and not ai.desc_error):
        return Layer(DESC_NAME, MISSING, "nie analizowano (tylko oferty DO WERYFIKACJI)")
    if not ai.desc:
        return Layer(DESC_NAME, MISSING, f"nie udało się: {ai.desc_error}")
    d = ai.desc
    note = str(d.get("note") or "")
    if not d.get("is_phone", True):
        return Layer(DESC_NAME, CONFLICT, "to nie telefon" + (f" — {note}" if note else ""))
    parts = []
    if d.get("storage_gb"):
        parts.append(f"pamięć {format_storage(d['storage_gb'])}")
    if d.get("battery_health"):
        parts.append(f"bateria {d['battery_health']}%")
    if d.get("for_parts"):
        parts.append("na części")
    defects = [Defect(x).label.lower() for x in d.get("defects", []) if x in Defect._value2member_map_]
    if defects:
        parts.append("usterki: " + ", ".join(defects))
    flags = [RedFlag(x).label for x in d.get("flags", []) if x in RedFlag._value2member_map_]
    if flags:
        parts.append("ryzyko: " + ", ".join(flags))
    summary = "telefon" + (f"; {', '.join(parts)}" if parts else "") + (f" — {note}" if note else "")
    return Layer(DESC_NAME, AGREE, summary)


def combine(ai: AiLayers | None, cfg) -> Combined:
    """``cfg`` = ``MlConfig`` z ustawień. Wynik: stan łączny, flagi do werdyktu i opis każdej warstwy."""
    text = text_layer(ai, cfg.text_phone_conf, cfg.text_conflict_conf) if cfg.text_enabled else \
        Layer("Tytuł (klasyfikator)", MISSING, "klasyfikator wyłączony")
    photo = photo_layer(ai, cfg.photo_phone_conf, cfg.photo_conflict_conf, enabled=cfg.photo_enabled)
    desc = desc_layer(ai, enabled=cfg.llm_enabled)
    layers = [text, photo, desc]
    flags: list[RedFlag] = []
    for layer, flag in ((text, RedFlag.AI_TEXT_CONFLICT), (photo, RedFlag.AI_PHOTO_CONFLICT),
                        (desc, RedFlag.AI_DESC_CONFLICT)):
        if layer.state == CONFLICT:
            flags.append(flag)
    if flags:
        return Combined(CONFLICT, flags, layers, "reguły uznały ofertę za telefon, a AI się z tym nie zgadza")
    states = [layer.state for layer in layers]
    if all(s == MISSING for s in states):
        return Combined("brak danych", [], layers, "brak wyników AI — decydują same reguły")
    if AGREE not in states:
        return Combined("niska pewność", [RedFlag.AI_LOW_CONFIDENCE], layers,
                        "żadna warstwa AI nie potwierdza wyraźnie, że to telefon")
    return Combined(AGREE, [], layers, "reguły i AI zgodnie: telefon")
