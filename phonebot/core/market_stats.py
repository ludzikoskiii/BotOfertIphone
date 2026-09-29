"""Statystyki rynku: ceny w czasie, podaż, czas aktywności ogłoszeń, najlepsze pory na zakupy, trend.

Liczone w tle z ofert w bazie (także archiwalnych) i zapisywane (``services.market_stats``), żeby nie
spowalniać aplikacji. Każda statystyka ma próg danych — poniżej niego pokazujemy „za mało danych”
zamiast mylących wykresów.

* Ceny dzienne: dla każdego dnia ogłoszenia aktywne tego dnia (od pierwszego do ostatniego zobaczenia),
  z ceną obowiązującą tego dnia (historia cen). Mediana i typowy zakres (10.–90. percentyl — 80% ofert),
  osobno dla modelu × pamięci (albo wszystkich pamięci) × klasy stanu. Ta sama sztuka z kilku portali — raz.
* Podaż: nowe ogłoszenia danego dnia.
* Czas aktywności: od wystawienia (data z portalu, a gdy jej brak — pierwsze zobaczenie) do zniknięcia z portalu
  — przybliżona szybkość sprzedaży (zniknięte ogłoszenie mogło też zostać po prostu usunięte).
* Najlepsze pory: kiedy pojawia się najwięcej ofert z werdyktem KUPUJ / NEGOCJUJ (werdykt z chwili pojawienia się
  oferty) i kiedy ceny są najniższe względem mediany rynku z tego dnia.
* Trend: prosta dopasowana do cen nowych ogłoszeń z ostatnich ``trend_days`` dni (niezależne próbki — dzienne
  mediany aktywnych ogłoszeń są ze sobą skorelowane), jako % zmiany w tym okresie; zmiana poniżej
  ``trend_stable_pct``% albo nieodróżnialna od szumu — „stabilny”.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

CLASSES = {"used": "używane sprawne", "damaged": "uszkodzone / na części", "new": "nowe"}
WEEKDAYS = ["poniedziałek", "wtorek", "środa", "czwartek", "piątek", "sobota", "niedziela"]
WEEKDAYS_SHORT = ["pon", "wt", "śr", "czw", "pt", "sob", "nd"]
WEEKDAYS_IN = ["w poniedziałek", "we wtorek", "w środę", "w czwartek", "w piątek", "w sobotę", "w niedzielę"]
ALL_STORAGE = 0  # wiersze „wszystkie pamięci”
GOOD_VERDICTS = ("KUPUJ", "NEGOCJUJ")
TOO_LITTLE = "za mało danych"


@dataclass
class MarketStatsConfig:
    """Ustawienia → Rynek."""

    enabled: bool = True
    recompute_hours: int = 6  # przeliczanie w tle co tyle godzin
    window_days: int = 90  # najdłuższy zakres wykresów
    refresh_days: int = 21  # ostatnie dni liczone od nowa (starsze są zapisane na stałe)
    min_offers_point: int = 3  # punkt wykresu cen: co najmniej tyle ofert danego dnia
    min_points: int = 5  # wykres: co najmniej tyle punktów w zakresie
    trend_days: int = 30
    trend_stable_pct: float = 3.0
    trend_min_offers: int = 15  # trend: co najmniej tyle nowych ogłoszeń w oknie
    min_ended: int = 5  # czas aktywności: co najmniej tyle zakończonych ogłoszeń
    min_good_offers: int = 10  # najlepsze pory: co najmniej tyle okazji (KUPUJ / NEGOCJUJ)
    backfill_limit: int = 2000  # werdykty „z chwili pojawienia się” uzupełniane na jedno przeliczenie


@dataclass
class OfferPoint:
    """Oferta do statystyk (jedna na sztukę — bez duplikatów z innych portali)."""

    id: int
    model: str
    storage_gb: int | None
    cls: str  # used | damaged | new
    first_seen: datetime
    last_seen: datetime
    price: float
    prices: list[tuple[datetime, float]] = field(default_factory=list)  # historia cen (rosnąco)
    created_at: datetime | None = None
    active: bool = True
    first_verdict: str | None = None

    def price_on(self, day: date) -> float:
        """Cena obowiązująca danego dnia (ostatnia zmiana do końca dnia)."""
        price = self.prices[0][1] if self.prices else self.price
        for when, p in self.prices:
            if when.date() <= day:
                price = p
            else:
                break
        return price

    @property
    def listed_at(self) -> datetime:
        """Wystawienie: data z portalu, gdy jest wcześniejsza niż pierwsze zobaczenie, inaczej pierwsze zobaczenie."""
        if self.created_at is not None and self.created_at <= self.first_seen:
            return self.created_at
        return self.first_seen


@dataclass
class DayStat:
    model: str
    storage_gb: int  # 0 = wszystkie
    cls: str
    day: date
    n: int
    p10: float | None
    p25: float | None
    median: float | None
    p75: float | None
    p90: float | None
    new_count: int


def quantile(values: list[float], q: float) -> float | None:
    """Percentyl z interpolacją liniową (``values`` posortowane)."""
    if not values:
        return None
    if len(values) == 1:
        return values[0]
    pos = (len(values) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(values) - 1)
    return round(values[lo] + (values[hi] - values[lo]) * (pos - lo), 2)


def daily_stats(offers: list[OfferPoint], days: list[date]) -> list[DayStat]:
    """Statystyki dzienne dla podanych dni (model × pamięć/wszystkie × klasa stanu)."""
    wanted = set(days)
    prices: dict[tuple, list[float]] = {}
    new: dict[tuple, int] = {}
    for o in offers:
        keys = [(o.model, o.storage_gb or ALL_STORAGE, o.cls)]
        if o.storage_gb:
            keys.append((o.model, ALL_STORAGE, o.cls))
        start, end = o.first_seen.date(), o.last_seen.date()
        if start in wanted:
            for k in keys:
                new[(*k, start)] = new.get((*k, start), 0) + 1
        d = max(start, min(days))
        stop = min(end, max(days))
        while d <= stop:
            if d in wanted:
                p = o.price_on(d)
                for k in keys:
                    prices.setdefault((*k, d), []).append(p)
            d += timedelta(days=1)
    out = []
    for key in sorted(set(prices) | set(new), key=lambda k: (k[0], k[1], k[2], k[3])):
        vals = sorted(prices.get(key, []))
        model, storage, cls, day = key
        out.append(DayStat(model, storage, cls, day, len(vals), quantile(vals, 0.10), quantile(vals, 0.25),
                           quantile(vals, 0.5), quantile(vals, 0.75), quantile(vals, 0.90), new.get(key, 0)))
    return out


# ---------------------------------------------------------------------- trend ---

@dataclass
class Trend:
    direction: str  # up | flat | down | unknown
    pct: float | None  # zmiana w oknie trendu (%)
    days: int
    offers: int  # suma ofert w punktach
    median: float | None  # ostatnia mediana

    @property
    def label(self) -> str:
        return {"up": "rośnie", "flat": "stabilny", "down": "spada"}.get(self.direction, TOO_LITTLE)

    @property
    def arrow(self) -> str:
        return {"up": "↗", "flat": "→", "down": "↘"}.get(self.direction, "")

    def describe(self) -> str:
        if self.direction == "unknown" or self.pct is None:
            return TOO_LITTLE
        if self.direction == "flat":  # zmiana mała albo w granicach szumu — bez liczby, żeby nie mylić
            return f"{self.arrow} {self.label} (bez wyraźnej zmiany w {self.days} dni, {self.offers} nowych ofert)"
        pct = f"{self.pct:+.0f}%".replace("-", "−")
        return f"{self.arrow} {self.label} ({pct} w {self.days} dni, {self.offers} nowych ofert)"

    def to_dict(self) -> dict:
        return {"direction": self.direction, "pct": self.pct, "days": self.days, "offers": self.offers,
                "median": self.median}

    @classmethod
    def from_dict(cls, d: dict) -> Trend:
        return cls(d.get("direction", "unknown"), d.get("pct"), int(d.get("days", 0)), int(d.get("offers", 0)),
                   d.get("median"))


def usable_points(rows: list[DayStat], cfg: MarketStatsConfig) -> list[DayStat]:
    return [r for r in rows if r.median is not None and r.n >= cfg.min_offers_point]


def trend(samples: list[tuple[date, float]], cfg: MarketStatsConfig, today: date) -> Trend:
    """Trend z cen NOWYCH ogłoszeń (niezależne próbki) z ostatnich ``trend_days`` dni: prosta dopasowana do
    (dzień, cena). „Rośnie” / „spada” tylko przy zmianie ≥ ``trend_stable_pct``% i wyraźnie większej od szumu
    (|nachylenie| ≥ 2 błędy standardowe) — inaczej „stabilny”. Ceny odstające (poza 0,5–2 × mediana) pomijane."""
    since = today - timedelta(days=cfg.trend_days)
    pts = [(d, p) for d, p in samples if d > since]
    if pts:
        med = quantile(sorted(p for _, p in pts), 0.5)
        pts = [(d, p) for d, p in pts if 0.5 * med <= p <= 2 * med]
    n = len(pts)
    days_with_data = len({d for d, _ in pts})
    last_median = quantile(sorted(p for _, p in pts), 0.5) if pts else None
    if n < cfg.trend_min_offers or days_with_data < cfg.min_points:
        return Trend("unknown", None, cfg.trend_days, n, last_median)
    xs = [(d - since).days for d, _ in pts]
    ys = [p for _, p in pts]
    mx, my = sum(xs) / n, sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0 or my <= 0:
        return Trend("unknown", None, cfg.trend_days, n, last_median)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx
    resid = sum((y - (my + slope * (x - mx))) ** 2 for x, y in zip(xs, ys, strict=True))
    se = (resid / max(1, n - 2) / sxx) ** 0.5
    pct = round(slope * cfg.trend_days / my * 100, 1)
    significant = se == 0 or abs(slope) >= 2 * se
    direction = "flat" if abs(pct) < cfg.trend_stable_pct or not significant else ("up" if pct > 0 else "down")
    return Trend(direction, pct, cfg.trend_days, n, last_median)


# ---------------------------------------------------------- czas aktywności ---

@dataclass
class ActiveTime:
    median_days: float | None
    count: int

    def describe(self) -> str:
        if self.median_days is None:
            return TOO_LITTLE
        return f"ok. {self.median_days:.0f} dni (mediana z {self.count} zakończonych ogłoszeń)"


def active_time(offers: list[OfferPoint], cfg: MarketStatsConfig) -> ActiveTime:
    """Jak długo ogłoszenie jest aktywne: od wystawienia do zniknięcia z portalu (tylko zakończone)."""
    days = sorted(max(0.0, (o.last_seen - o.listed_at).total_seconds() / 86400) for o in offers if not o.active)
    if len(days) < cfg.min_ended:
        return ActiveTime(None, len(days))
    return ActiveTime(round(quantile(days, 0.5), 1), len(days))


# ------------------------------------------------------------ najlepsze pory ---

@dataclass
class Slot:
    good: int = 0  # okazje (KUPUJ / NEGOCJUJ)
    total: int = 0  # wszystkie nowe oferty
    ratios: list[float] = field(default_factory=list)  # cena / mediana rynku z tego dnia

    @property
    def price_index(self) -> float | None:
        """Mediana (cena / mediana rynku) − 1, w %: −4 = oferty średnio 4% tańsze."""
        if len(self.ratios) < 5:
            return None
        return round((quantile(sorted(self.ratios), 0.5) - 1) * 100, 1)


@dataclass
class BestTimes:
    weekdays: list[Slot]
    hours: list[Slot]
    good_total: int
    enough: bool

    def best_weekday(self) -> int | None:
        return max(range(7), key=lambda i: self.weekdays[i].good) if self.enough else None

    def best_hours(self, width: int = 3) -> tuple[int, int] | None:
        """Najlepsze okno godzin (np. 18–21) wg liczby okazji."""
        if not self.enough:
            return None
        best = max(range(24), key=lambda h: sum(self.hours[(h + i) % 24].good for i in range(width)))
        return best, (best + width) % 24

    def cheapest_weekday(self) -> int | None:
        idx = [(s.price_index, i) for i, s in enumerate(self.weekdays) if s.price_index is not None]
        return min(idx)[1] if len(idx) >= 3 else None

    def describe(self) -> list[str]:
        if not self.enough:
            return [f"{TOO_LITTLE} (okazji KUPUJ / NEGOCJUJ: {self.good_total})"]
        lines = []
        day, hours = self.best_weekday(), self.best_hours()
        lines.append(f"Najwięcej okazji (KUPUJ / NEGOCJUJ) pojawia się {WEEKDAYS_IN[day]} "
                     f"({self.weekdays[day].good} z {self.good_total}) i między {hours[0]}:00 a {hours[1]}:00.")
        cheap = self.cheapest_weekday()
        if cheap is not None:
            idx = self.weekdays[cheap].price_index
            if idx <= -1:
                lines.append(f"Najniższe ceny: oferty wystawiane {WEEKDAYS_IN[cheap]} są średnio o {abs(idx):.0f}% "
                             "tańsze od mediany rynku.")
            else:
                lines.append("Ceny nowych ofert nie zależą wyraźnie od dnia tygodnia.")
        return lines


def best_times(offers: list[OfferPoint], medians: dict[tuple, float], cfg: MarketStatsConfig) -> BestTimes:
    """``medians``: (model, pamięć, klasa, dzień) → mediana rynku (do porównania cen)."""
    weekdays, hours = [Slot() for _ in range(7)], [Slot() for _ in range(24)]
    good_total = 0
    for o in offers:
        when = o.listed_at if o.created_at is not None and o.first_seen - o.listed_at < timedelta(days=1) \
            else o.first_seen  # data z portalu tylko, gdy to świeże ogłoszenie (nie „podbite” stare)
        local = when.astimezone()
        slots = (weekdays[local.weekday()], hours[local.hour])
        good = o.first_verdict in GOOD_VERDICTS
        good_total += good
        med = medians.get((o.model, o.storage_gb or ALL_STORAGE, o.cls, o.first_seen.date()))
        for s in slots:
            s.total += 1
            s.good += good
            if med:
                s.ratios.append(o.prices[0][1] / med if o.prices else o.price / med)
    return BestTimes(weekdays, hours, good_total, good_total >= cfg.min_good_offers)
