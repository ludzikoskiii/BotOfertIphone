"""Lista „Wybrane”: oferty spełniające Twoje kryteria automatyczne + dodane ręcznie.

Zasady (ręczne decyzje mają pierwszeństwo przed automatem):

1. „★ Obserwuj” = ręczne dodanie do Wybranych — oferta jest tam zawsze, nawet gdy nie spełnia kryteriów.
2. „Usuń z Wybranych” = ręczne wykluczenie — automat już jej nie doda (aż do ponownego „Obserwuj”).
3. Pozostałe oferty trafiają do Wybranych automatycznie, jeśli spełniają kryteria z Ustawień
   (werdykty, minimalny zysk, minimalna ocena).
4. Oferta, która zniknęła z portalu (sprzedana, usunięta albo niewidziana od ``offer_stale_days``), zostaje
   w Wybranych z oznaczeniem ⌛ „nieaktualna” jeszcze przez ``keep_inactive_days`` dni (domyślnie 1), na końcu
   listy — potem znika sama, żeby stare oferty nie zasłaniały nowych. Obserwowane (★) zostają do ręcznego
   usunięcia; przycisk „Usuń nieaktualne” czyści wszystkie od razu.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .models import Offer, OfferStatus, Valuation, Verdict


@dataclass
class SelectionCriteria:
    """Kryteria automatycznego dodawania do „Wybrane” (Ustawienia → Wybrane)."""

    enabled: bool = True
    verdicts: list[str] = field(default_factory=lambda: [Verdict.BUY.value, Verdict.NEGOTIATE.value])
    min_profit: float = 150.0  # 0 = bez progu
    min_score: int = 0  # 0 = bez progu
    skip_hard_flags: bool = True  # oferta z poważną flagą (iCloud, IMEI, podróbka) nie trafia automatycznie
    keep_inactive_days: int = 1  # nieaktualna (⌛) zostaje w „Wybrane” tyle dni; 0 = do ręcznego usunięcia


def high_risk(val: Valuation) -> bool:
    """Wysokie ryzyko oszustwa (``core.fraud``) — nigdy automatycznie do „Wybrane” ani na Telegram."""
    return getattr(val.risk, "level", "low") == "high"


def auto_match(offer: Offer, val: Valuation, c: SelectionCriteria) -> bool:
    if not c.enabled or offer.status is OfferStatus.HIDDEN or high_risk(val):
        return False
    if c.verdicts and val.verdict.value not in c.verdicts:
        return False
    if c.min_profit and (val.expected_profit is None or val.expected_profit < c.min_profit):
        return False
    if c.min_score and val.score < c.min_score:
        return False
    return not (c.skip_hard_flags and val.has_hard_flag)


def is_picked(offer: Offer, val: Valuation, c: SelectionCriteria) -> bool:
    """Czy oferta należy do „Wybrane” (ręczne decyzje mają pierwszeństwo)."""
    if offer.pick_excluded:
        return False
    if offer.status is OfferStatus.WATCHED:
        return True
    return auto_match(offer, val, c)


def pick_reason(offer: Offer, val: Valuation, c: SelectionCriteria) -> str:
    """Krótkie wyjaśnienie do panelu szczegółów."""
    if offer.pick_excluded:
        return "usunięta ręcznie z „Wybrane” (automat jej nie doda)"
    if offer.status is OfferStatus.WATCHED:
        return "w „Wybrane” — dodana ręcznie (obserwowana)"
    if auto_match(offer, val, c):
        return "w „Wybrane” — spełnia kryteria automatyczne"
    return ""


def describe(c: SelectionCriteria) -> str:
    if not c.enabled:
        return "automat wyłączony — tylko oferty dodane ręcznie"
    parts = []
    if c.verdicts:
        parts.append("werdykt " + " / ".join(c.verdicts))
    if c.min_profit:
        parts.append(f"zysk ≥ {c.min_profit:.0f} zł")
    if c.min_score:
        parts.append(f"ocena ≥ {c.min_score}")
    if c.skip_hard_flags:
        parts.append("bez poważnych flag")
    return ", ".join(parts) or "wszystkie oferty"
