"""Odświeżanie przyrostowe: harmonogram per portal (bez okna — sama logika, łatwa do testowania).

* **Nowe oferty** — każdy portal osobno, co ``interval_s`` (domyślnie 2 min) z losową zmiennością ±``jitter_s``,
  żeby zapytania nie szły w równym rytmie. Wolny albo niedziałający portal nie opóźnia pozostałych.
* **Ochrona przed blokadą** — po odpowiedzi 403/429/captcha odstęp dla tego portalu rośnie: 2 → 5 → 15 → 60 min;
  gdy portal znowu działa, wraca do 2 min. Minimalny odstęp każdego portalu ustawiasz w Ustawieniach.
* **Stare oferty** — „Wybrane” i obserwowane co ``watch_check_minutes`` (czy istnieją, czy zmieniła się cena),
  pozostałe raz na dobę w nocy (``nightly_hour``) razem z pełnym pobraniem kontrolnym.
* **Archiwum** — oferty starsze niż ``archive_days`` znikają z tabeli (zostają w bazie do statystyk).
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta


def _default_min_intervals() -> dict[str, int]:
    # OLX z maili: powiadomienia przychodzą co kilka–kilkanaście minut — częściej nie ma sensu
    return {"olx": 300}


@dataclass
class RefreshConfig:
    """Ustawienia odświeżania (Ustawienia → Ogólne i pobieranie)."""

    enabled: bool = True  # szybkie odświeżanie nowych ofert (wyłączone = stary tryb: pełne co ``refresh_minutes``)
    interval_s: int = 120
    jitter_s: int = 20
    min_interval_s: dict[str, int] = field(default_factory=_default_min_intervals)  # portal → minimalny odstęp
    backoff_minutes: list[int] = field(default_factory=lambda: [5, 15, 60])  # kolejne odstępy po blokadach
    known_stop: int = 3  # tyle znanych ogłoszeń na stronie = koniec nowych
    watch_check_minutes: int = 60  # „Wybrane” i obserwowane
    nightly_hour: int = 3  # pełne pobranie kontrolne + sprawdzenie pozostałych ofert (godzina)
    nightly_page_checks: int = 300  # najwyżej tyle stron ofert sprawdzanych w nocy (łagodnie dla portali)
    archive_days: int = 3
    new_badge_minutes: int = 30


BLOCKED = "blocked"
OK_KINDS = ("ok", "empty")


@dataclass
class SourceState:
    next_due: datetime
    level: int = 0  # 0 = normalny odstęp, 1.. = po kolejnych blokadach
    last_kind: str = ""
    last_run: datetime | None = None
    running: bool = False


class RefreshScheduler:
    """Kiedy odpytać który portal (szybkie odświeżanie) i kiedy zadania okresowe."""

    def __init__(self, cfg: RefreshConfig, sources: list[str], now: datetime, *, rng: random.Random | None = None,
                 first_delay_s: int = 10):
        self.cfg = cfg
        self.rng = rng or random.Random()
        # start rozłożony: portale nie ruszają w tej samej sekundzie
        self.states = {s: SourceState(now + timedelta(seconds=first_delay_s + i * 7)) for i, s in enumerate(sources)}

    def set_sources(self, sources: list[str], now: datetime) -> None:
        for s in sources:
            self.states.setdefault(s, SourceState(now + timedelta(seconds=10)))
        for s in list(self.states):
            if s not in sources:
                del self.states[s]

    # --------------------------------------------------------------- odstępy ---

    def base_interval(self, source: str) -> int:
        return max(self.cfg.interval_s, int(self.cfg.min_interval_s.get(source, 0) or 0), 30)

    def interval(self, source: str) -> int:
        """Odstęp w sekundach (bez losowej zmienności) — dłuższy po blokadach."""
        st = self.states[source]
        base = self.base_interval(source)
        if st.level <= 0 or not self.cfg.backoff_minutes:
            return base
        step = self.cfg.backoff_minutes[min(st.level, len(self.cfg.backoff_minutes)) - 1]
        return max(base, int(step) * 60)

    def due(self, now: datetime) -> list[str]:
        return [s for s, st in self.states.items() if not st.running and st.next_due <= now]

    def started(self, source: str) -> None:
        self.states[source].running = True

    def record(self, source: str, kind: str, now: datetime) -> None:
        """Wynik przebiegu: blokada wydłuża odstęp, działanie wraca do normalnego."""
        st = self.states.get(source)
        if st is None:
            return
        st.running = False
        st.last_kind, st.last_run = kind, now
        if kind == BLOCKED:
            st.level = min(st.level + 1, max(1, len(self.cfg.backoff_minutes)))
        elif kind in OK_KINDS:
            st.level = 0
        jitter = self.rng.uniform(-self.cfg.jitter_s, self.cfg.jitter_s) if self.cfg.jitter_s else 0
        st.next_due = now + timedelta(seconds=max(30, self.interval(source) + jitter))

    def badge(self, source: str) -> str:
        """Krótko na znaczniku portalu: wydłużony odstęp po blokadzie („co 15 min”)."""
        st = self.states.get(source)
        if st is None or st.level <= 0:
            return ""
        return f"co {self.interval(source) // 60} min"

    def describe(self, source: str, now: datetime) -> str:
        st = self.states.get(source)
        if st is None:
            return ""
        minutes = self.interval(source) / 60
        every = f"{minutes:.0f} min" if minutes >= 1 else f"{self.interval(source)} s"
        if st.running:
            return f"sprawdzam teraz (nowe oferty co ~{every})"
        left = max(0, int((st.next_due - now).total_seconds()))
        when = f"{left // 60} min" if left >= 60 else f"{left} s"
        if st.level > 0:
            steps = " → ".join(str(m) for m in [self.base_interval(source) // 60, *self.cfg.backoff_minutes])
            return (f"portal blokuje — odstęp wydłużony do {every} ({steps} min), kolejna próba za {when}; "
                    f"po udanej próbie wraca do {self.base_interval(source) // 60} min")
        return f"nowe oferty co ~{every}, następne sprawdzenie za {when}"

    # ------------------------------------------------------- zadania okresowe ---

    def watch_due(self, now: datetime, last: datetime | None) -> bool:
        return last is None or now - last >= timedelta(minutes=self.cfg.watch_check_minutes)

    def nightly_due(self, now: datetime, last: datetime | None) -> bool:
        """Raz na dobę, od ``nightly_hour`` (albo przy pierwszym uruchomieniu po tej godzinie)."""
        if now.hour < self.cfg.nightly_hour:
            return False
        return last is None or last.date() < now.date()


def is_new(first_seen: datetime | None, now: datetime, minutes: int) -> bool:
    """Znacznik „NOWE” w tabeli: oferta pojawiła się w ciągu ostatnich ``minutes`` minut."""
    return first_seen is not None and minutes > 0 and now - first_seen <= timedelta(minutes=minutes)
