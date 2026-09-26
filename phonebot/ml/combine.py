"""Łączenie warstw oceny: reguły (etap 1) + klasyfikator tytułu + analiza zdjęcia.

Oferta w tabeli przeszła już reguły (to one uznały ją za telefon). Warstwy AI mogą to potwierdzić
albo podważyć — nigdy same nie odrzucają oferty, tylko ograniczają werdykt:

* warstwa **sprzeczna** (tytuł wygląda na akcesorium/część/kupię albo zdjęcie pokazuje etui/szkło/pudełko
  z wysoką pewnością) → flaga → najwyżej DO WERYFIKACJI,
* **niska pewność** (ani tytuł, ani zdjęcie nie potwierdza, że to telefon) → flaga → najwyżej DO WERYFIKACJI,
* obie potwierdzają albo jedna potwierdza, a druga nie ma zdania → bez zmian.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from ..core.models import AiLayers, RedFlag

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


def combine(ai: AiLayers | None, cfg) -> Combined:
    """``cfg`` = ``MlConfig`` z ustawień. Wynik: stan łączny, flagi do werdyktu i opis każdej warstwy."""
    text = text_layer(ai, cfg.text_phone_conf, cfg.text_conflict_conf) if cfg.text_enabled else \
        Layer("Tytuł (klasyfikator)", MISSING, "klasyfikator wyłączony")
    photo = photo_layer(ai, cfg.photo_phone_conf, cfg.photo_conflict_conf, enabled=cfg.photo_enabled)
    layers = [text, photo]
    flags: list[RedFlag] = []
    if text.state == CONFLICT:
        flags.append(RedFlag.AI_TEXT_CONFLICT)
    if photo.state == CONFLICT:
        flags.append(RedFlag.AI_PHOTO_CONFLICT)
    if flags:
        return Combined(CONFLICT, flags, layers, "reguły uznały ofertę za telefon, a AI się z tym nie zgadza")
    if text.state == MISSING and photo.state == MISSING:
        return Combined("brak danych", [], layers, "brak wyników AI — decydują same reguły")
    if AGREE not in (text.state, photo.state):
        return Combined("niska pewność", [RedFlag.AI_LOW_CONFIDENCE], layers,
                        "ani tytuł, ani zdjęcie nie potwierdza wyraźnie, że to telefon")
    return Combined(AGREE, [], layers, "reguły i AI zgodnie: telefon")
