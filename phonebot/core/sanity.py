"""Zabezpieczenia werdyktu: testy sensowności i limity werdyktu przy flagach.

Reguły działają po wyliczeniu opłacalności. Nie zmieniają wyliczeń, tylko ograniczają
najlepszy możliwy werdykt, gdy dane wyglądają podejrzanie:

* cena dużo niższa od wartości rynkowej → „Cena nierealnie niska” → najwyżej DO WERYFIKACJI,
* nierealnie wysoki zysk (%) → najwyżej DO WERYFIKACJI,
* nieznana pamięć (wycena przybliżona) → najwyżej DO WERYFIKACJI,
* każda inna flaga ogranicza werdykt: miękka (ostrzeżenie) i poważna — progi w ustawieniach.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import RedFlag, Severity, Verdict

# flagi testów sensowności — zawsze najwyżej DO WERYFIKACJI (tego wymaga ich sens)
SANITY_FLAGS = frozenset({RedFlag.PRICE_UNREALISTIC, RedFlag.PROFIT_UNREALISTIC, RedFlag.STORAGE_UNKNOWN,
                          RedFlag.SERIAL_SELLER})

VERDICT_CHOICES = {
    Verdict.BUY.value: "bez limitu (może być KUPUJ)",
    Verdict.NEGOTIATE.value: "najwyżej NEGOCJUJ",
    Verdict.VERIFY.value: "najwyżej DO WERYFIKACJI",
    Verdict.SKIP.value: "zawsze ODPUŚĆ",
}


@dataclass
class SanityConfig:
    # cena poniżej tego ułamka wartości rynkowej = podejrzana (nigdy KUPUJ)
    price_min_ratio_working: float = 0.30
    # dla uszkodzonych / „na części” niższy próg — tanie uszkodzone telefony to normalna okazja do naprawy
    price_min_ratio_damaged: float = 0.15
    # zysk powyżej tylu procent zainwestowanej kwoty = do weryfikacji
    profit_max_pct: float = 150.0
    # oferta bez rozpoznanej pamięci może dostać najwyżej DO WERYFIKACJI
    unknown_storage_verify: bool = True
    # najwyższy werdykt przy fladze ostrzegawczej (miękkiej) i poważnej
    soft_flag_cap: str = Verdict.NEGOTIATE.value
    hard_flag_cap: str = Verdict.VERIFY.value
    # sprzedawcy seryjni: tyle tanich ofert od jednego sprzedawcy…
    serial_enabled: bool = True
    serial_min_offers: int = 3
    # …w cenie poniżej tego ułamka wartości rynkowej modelu
    serial_price_ratio: float = 0.40


def flag_cap(flag: RedFlag, cfg: SanityConfig, *, hard_force_skip: bool = False) -> Verdict:
    """Najlepszy werdykt, na jaki pozwala dana flaga."""
    if flag in SANITY_FLAGS:
        return Verdict.VERIFY
    if flag.severity is Severity.HARD:
        return Verdict.SKIP if hard_force_skip else _verdict(cfg.hard_flag_cap, Verdict.VERIFY)
    return _verdict(cfg.soft_flag_cap, Verdict.NEGOTIATE)


def verdict_cap(flags: list[RedFlag], cfg: SanityConfig, *, hard_force_skip: bool = False
                ) -> tuple[Verdict, list[RedFlag]]:
    """Najlepszy dopuszczalny werdykt przy tych flagach i flagi, które go ograniczają."""
    cap = Verdict.BUY
    limiting: list[RedFlag] = []
    for f in dict.fromkeys(flags):
        c = flag_cap(f, cfg, hard_force_skip=hard_force_skip)
        if c.rank < cap.rank:
            cap, limiting = c, [f]
        elif c.rank == cap.rank and c is not Verdict.BUY:
            limiting.append(f)
    return cap, limiting


def _verdict(value: str, default: Verdict) -> Verdict:
    try:
        return Verdict(value)
    except ValueError:
        return default
