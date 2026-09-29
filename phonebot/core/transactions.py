"""Transakcje (kupione telefony), realny zysk i samodoskonalenie wyceny.

* Transakcja: dane telefonu, zakup, części z magazynu (po ich cenie zakupu), inne koszty, faktyczny czas
  naprawy, sprzedaż; status kupiony → w naprawie → wystawiony → sprzedany. „Kupiłem” zapisuje też wycenę
  programu z chwili zakupu (``Snapshot``) — do porównania z faktycznym wynikiem.
* Poprawki: osobno dla kombinacji model + usterki (np. „iPhone 12, zbity ekran”) porównujemy wycenę z chwili
  zakupu z wynikiem: koszt naprawy, czas naprawy, cena odsprzedaży, czas do sprzedaży. Poprawka działa dopiero
  od ``min_transactions`` transakcji danego typu i stopniowo: waga = n / (n + ``prior``). Liczymy medianę
  proporcji (jedna nietypowa transakcja jej nie przestawi) i przycinamy ją do ±``max_change_pct``.
"""
from __future__ import annotations

import statistics
from dataclasses import asdict, dataclass, field
from datetime import datetime

from .models import Condition, Defect

# etykiety kosztów sprzedaży z ``valuation.selling_costs`` (przy „Kupiłem” przenosimy tylko koszty zakupu)
SELL_COST_PREFIXES = ("Prowizja", "Opłata stała", "Wysyłka do kupującego", "Pakowanie")
STATUSES = {"bought": "kupiony", "repair": "w naprawie", "listed": "wystawiony", "sold": "sprzedany"}
STATUS_ORDER = list(STATUSES)
COST_KINDS = {"repair": "naprawa", "buy": "zakup (wysyłka, dojazd)", "sell": "sprzedaż (prowizja, wysyłka)",
              "other": "inne"}

# „iPhone 12 ze zbitym ekranem” — usterki w narzędniku, do wniosków pisanych zwykłym językiem
_DEFECT_PHRASE = {
    Defect.SCREEN: "ze zbitym ekranem",
    Defect.BACK_GLASS: "z pękniętą tylną szybą",
    Defect.BATTERY: "ze słabą baterią",
    Defect.CHARGING_PORT: "z uszkodzonym portem ładowania",
    Defect.CAMERA: "z uszkodzonym aparatem",
    Defect.CAMERA_LENS: "z pękniętym szkiełkiem aparatu",
    Defect.FACE_ID: "bez działającego Face ID",
    Defect.SPEAKER: "z uszkodzonym głośnikiem",
    Defect.MICROPHONE: "z uszkodzonym mikrofonem",
    Defect.BUTTONS: "z uszkodzonymi przyciskami",
    Defect.HOUSING: "z wgniecioną obudową",
    Defect.NO_POWER: ", który się nie włącza",
    Defect.WATER_DAMAGE: "po zalaniu",
}


@dataclass
class LearningConfig:
    """Ustawienia → Transakcje: poprawki wyceny z Twoich transakcji."""

    enabled: bool = True  # uwzględniaj poprawki w wycenie (wyłączone = tylko podgląd)
    min_transactions: int = 3  # poprawka od tylu transakcji danego typu
    prior: int = 3  # waga poprawki = n / (n + prior): 3 transakcje → 50%, 6 → 67%, 12 → 80%
    max_change_pct: float = 50.0  # największa poprawka (±%) — chroni przed błędnie wpisanymi danymi
    tolerance_pct: float = 3.0  # mniejsze różnice = „zgadza się z wyceną”
    default_sell_days: float = 14.0  # zakładany czas od wystawienia do sprzedaży (do porównania)


@dataclass
class Snapshot:
    """Wycena programu w chwili zakupu („Kupiłem”).

    ``resale_value``, ``repair_cost`` i ``repair_minutes`` to wycena BAZOWA (bez poprawek z transakcji) — poprawki
    uczą się względem niej, więc nie nakładają się same na siebie. ``*_shown`` — to, co pokazał program."""

    verdict: str = ""
    resale_value: float | None = None  # wartość rynkowa V (cena odsprzedaży)
    repair_cost: float | None = None
    resale_shown: float | None = None
    repair_cost_shown: float | None = None
    profit: float | None = None
    max_buy: float | None = None
    repair_minutes: int | None = None
    handling_minutes: int | None = None
    sell_days: float | None = None
    profit_per_hour: float | None = None
    corrected: bool = False  # wycena już zawierała poprawki z transakcji

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> Snapshot | None:
        if not data:
            return None
        known = {k: v for k, v in data.items() if k in cls.__dataclass_fields__}
        return cls(**known)


@dataclass
class CostEntry:
    kind: str  # repair | buy | sell | other
    label: str
    amount: float


@dataclass
class UsedPart:
    """Część zdjęta z magazynu dla tej transakcji (po cenie zakupu z partii)."""

    part: Defect
    qty: int
    unit_price: float

    @property
    def total(self) -> float:
        return round(self.qty * self.unit_price, 2)


@dataclass
class Transaction:
    id: int | None = None
    offer_id: int | None = None
    model: str | None = None
    storage_gb: int | None = None
    condition: Condition = Condition.DAMAGED
    defects: list[Defect] = field(default_factory=list)
    source: str = ""
    url: str = ""
    title: str = ""
    status: str = "bought"
    bought_at: datetime | None = None
    buy_price: float = 0.0
    costs: list[CostEntry] = field(default_factory=list)
    parts: list[UsedPart] = field(default_factory=list)  # z magazynu (wczytywane z zużycia magazynu)
    repair_minutes: int | None = None  # faktyczny czas naprawy
    handling_minutes: int | None = None  # odbiór, sprawdzenie, wystawienie, sprzedaż
    listed_at: datetime | None = None
    sold_at: datetime | None = None
    sell_price: float | None = None
    sold_where: str = ""
    note: str = ""
    snapshot: Snapshot | None = None

    # ------------------------------------------------------------ wyliczenia ---

    @property
    def status_label(self) -> str:
        return STATUSES.get(self.status, self.status)

    @property
    def sold(self) -> bool:
        return self.status == "sold" and self.sell_price is not None

    @property
    def parts_cost(self) -> float:
        return round(sum(p.total for p in self.parts), 2)

    def costs_of(self, kind: str) -> float:
        return round(sum(c.amount for c in self.costs if c.kind == kind), 2)

    @property
    def other_costs(self) -> float:
        return round(sum(c.amount for c in self.costs), 2)

    @property
    def repair_cost(self) -> float:
        """Faktyczny koszt naprawy: części z magazynu + koszty oznaczone jako „naprawa”."""
        return round(self.parts_cost + self.costs_of("repair"), 2)

    @property
    def total_cost(self) -> float:
        return round(self.buy_price + self.parts_cost + self.other_costs, 2)

    @property
    def profit(self) -> float | None:
        """Realny zysk — dopiero po sprzedaży."""
        return round(self.sell_price - self.total_cost, 2) if self.sold else None

    @property
    def minutes(self) -> int | None:
        if self.repair_minutes is None and self.handling_minutes is None:
            return None
        return (self.repair_minutes or 0) + (self.handling_minutes or 0)

    @property
    def profit_per_hour(self) -> float | None:
        profit, minutes = self.profit, self.minutes
        if profit is None or not minutes:
            return None
        return round(profit / (minutes / 60), 2)

    @property
    def sell_days(self) -> float | None:
        """Od wystawienia (albo zakupu, gdy brak daty wystawienia) do sprzedaży."""
        if not self.sold or self.sold_at is None:
            return None
        start = self.listed_at or self.bought_at
        if start is None:
            return None
        return max(0.0, round((self.sold_at - start).total_seconds() / 86400, 1))

    @property
    def key(self) -> tuple[str, tuple[str, ...]]:
        """Typ transakcji do poprawek: model + usterki."""
        return key_for(self.model, self.defects)


def snapshot_from(val, cfg: LearningConfig) -> Snapshot:
    """Wycena z chwili zakupu (``Valuation``) do zapisania w transakcji."""
    base = val.baseline or {}
    handling = sum(i.minutes for i in val.time_items if not i.repair) if val.time_items else None
    return Snapshot(
        verdict=val.verdict.value, resale_value=base.get("resale", val.market.value),
        repair_cost=base.get("repair_cost", val.repair_cost), resale_shown=val.resale_value,
        repair_cost_shown=val.repair_cost, profit=val.expected_profit, max_buy=val.max_buy_price,
        repair_minutes=base.get("repair_minutes"), handling_minutes=handling,
        # czas sprzedaży bazowy: czas aktywności ogłoszeń modelu (zakładka „Rynek”), a bez danych — z ustawień
        sell_days=val.active_days or cfg.default_sell_days, profit_per_hour=val.profit_per_hour,
        corrected=bool(val.corrections),
    )


def from_offer(offer, val, cfg: LearningConfig, now: datetime) -> Transaction:
    """„Kupiłem”: transakcja z danymi oferty, kosztami zakupu z wyceny i wyceną z chwili zakupu."""
    # koszty zakupu z wyceny (wysyłka / dojazd, opłata kupującego, cło); koszty sprzedaży dopiszesz po sprzedaży
    buy_costs = [CostEntry("buy", item.label, item.amount) for item in val.cost_items
                 if not item.label.startswith(SELL_COST_PREFIXES)]
    snap = snapshot_from(val, cfg)
    return Transaction(
        offer_id=offer.id, model=offer.parsed.model, storage_gb=offer.parsed.storage_gb,
        condition=offer.parsed.condition, defects=list(dict.fromkeys(offer.parsed.defects)), source=offer.raw.source,
        url=offer.raw.url, title=offer.raw.title, status="bought", bought_at=now, buy_price=offer.price,
        costs=buy_costs, handling_minutes=snap.handling_minutes, snapshot=snap,
    )


def key_for(model: str | None, defects) -> tuple[str, tuple[str, ...]]:
    return (model or "?", tuple(sorted({Defect(d).value for d in defects})))


def describe_key(key: tuple[str, tuple[str, ...]]) -> str:
    """„iPhone 12 ze zbitym ekranem”, „iPhone 13 bez usterek”."""
    model, defects = key
    if not defects:
        return f"{model} bez usterek"
    phrases = [_DEFECT_PHRASE.get(Defect(d), Defect(d).label.lower()) for d in defects]
    text = model
    for i, phrase in enumerate(phrases):
        joiner = "" if phrase.startswith(",") else " "
        text += (" i" if i else "") + joiner + phrase
    return text


def plural(n: int, one: str, few: str, many: str) -> str:
    if n == 1:
        return one
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return few
    return many


def tx_count(n: int) -> str:
    return f"{n} {plural(n, 'transakcja', 'transakcje', 'transakcji')}"


# ------------------------------------------------------------------ poprawki ---

METRICS = ("repair_cost", "repair_time", "resale", "sell_days")


@dataclass
class Factor:
    metric: str
    ratio: float  # mediana (faktycznie / zakładane), przycięta
    n: int
    weight: float  # 0 = za mało danych
    expected: float  # mediana wartości zakładanej (do opisu)
    actual: float  # mediana wartości faktycznej
    tolerance_pct: float = 0.0  # mniejsza różnica = zgodne z wyceną, bez poprawki

    @property
    def applied(self) -> float:
        """Mnożnik stosowany w wycenie: 1 + waga × (proporcja − 1)."""
        return round(1 + self.weight * (self.ratio - 1), 4)

    @property
    def active(self) -> bool:
        """Dość transakcji, żeby liczyć poprawkę."""
        return self.weight > 0

    @property
    def applies(self) -> bool:
        """Poprawka zmienia wycenę: dość transakcji i różnica większa niż tolerancja."""
        return self.active and abs(self.ratio - 1) * 100 >= self.tolerance_pct


@dataclass
class Correction:
    key: tuple[str, tuple[str, ...]]
    count: int  # transakcje tego typu (wszystkie)
    factors: dict[str, Factor] = field(default_factory=dict)

    def factor(self, metric: str) -> Factor | None:
        f = self.factors.get(metric)
        return f if f is not None and f.applies else None

    @property
    def active(self) -> bool:
        return any(f.applies for f in self.factors.values())


def _pairs(tx: Transaction, metric: str, cfg: LearningConfig) -> tuple[float, float] | None:
    """(zakładane, faktyczne) dla jednej transakcji albo None, gdy nie ma czego porównać."""
    snap = tx.snapshot
    if snap is None:
        return None
    done_repair = tx.status in ("listed", "sold")  # naprawa zakończona
    # 0 zł / 0 min traktujemy jak „nie wpisano” (nie da się odróżnić darmowej naprawy od braku danych)
    if metric == "repair_cost" and done_repair and snap.repair_cost and tx.repair_cost > 0:
        return snap.repair_cost, tx.repair_cost
    if metric == "repair_time" and done_repair and snap.repair_minutes and tx.repair_minutes:
        return float(snap.repair_minutes), float(tx.repair_minutes)
    if metric == "resale" and tx.sold and snap.resale_value:
        return snap.resale_value, float(tx.sell_price)
    if metric == "sell_days" and tx.sell_days is not None:
        return float(snap.sell_days or cfg.default_sell_days), tx.sell_days
    return None


def corrections(transactions: list[Transaction], cfg: LearningConfig) -> dict[tuple, Correction]:
    """Poprawki dla każdego typu transakcji (model + usterki)."""
    groups: dict[tuple, list[Transaction]] = {}
    for tx in transactions:
        if tx.model:
            groups.setdefault(tx.key, []).append(tx)
    limit = max(0.0, cfg.max_change_pct) / 100
    out: dict[tuple, Correction] = {}
    for key, txs in groups.items():
        corr = Correction(key, len(txs))
        for metric in METRICS:
            pairs = [p for p in (_pairs(tx, metric, cfg) for tx in txs) if p is not None and p[0] > 0]
            if not pairs:
                continue
            ratio = statistics.median(actual / expected for expected, actual in pairs)
            ratio = min(1 + limit, max(1 - limit, ratio))
            n = len(pairs)
            weight = n / (n + max(0, cfg.prior)) if n >= max(1, cfg.min_transactions) else 0.0
            corr.factors[metric] = Factor(metric, round(ratio, 4), n, round(weight, 3),
                                          statistics.median(e for e, _ in pairs), statistics.median(a for _, a in pairs),
                                          cfg.tolerance_pct)
        out[key] = corr
    return out


class Corrections:
    """Poprawki do wyceny (``PartsCatalog.corrections``) — wyszukiwanie po modelu i usterkach oferty."""

    def __init__(self, by_key: dict[tuple, Correction], cfg: LearningConfig):
        self.by_key = by_key
        self.cfg = cfg

    def for_offer(self, model: str | None, defects) -> Correction | None:
        corr = self.by_key.get(key_for(model, defects))
        return corr if corr is not None and corr.active else None


# ------------------------------------------------------------------- wnioski ---

def _pct(ratio: float) -> float:
    return round((ratio - 1) * 100)


def _sentence(f: Factor, cfg: LearningConfig) -> str | None:
    pct = _pct(f.ratio)
    same = abs(f.ratio - 1) * 100 < cfg.tolerance_pct  # to samo kryterium co ``Factor.applies``
    if f.metric == "repair_cost":
        if same:
            return "koszt naprawy zgadza się z wyceną"
        return (f"naprawa kosztuje Cię średnio o {abs(pct):.0f}% {'więcej' if pct > 0 else 'mniej'}, niż zakładam "
                f"(ok. {f.actual:.0f} zł zamiast {f.expected:.0f} zł)")
    if f.metric == "repair_time":
        if same:
            return "czas naprawy zgadza się z wyceną"
        return (f"naprawa zajmuje Ci średnio o {abs(pct):.0f}% {'dłużej' if pct > 0 else 'krócej'}, niż zakładam "
                f"(ok. {f.actual:.0f} min zamiast {f.expected:.0f} min)")
    if f.metric == "resale":
        if same:
            return "cena sprzedaży zgadza się z wyceną"
        return (f"sprzedajesz średnio o {abs(pct):.0f}% {'drożej' if pct > 0 else 'taniej'}, niż zakładam "
                f"(ok. {f.actual:.0f} zł zamiast {f.expected:.0f} zł)")
    if f.metric == "sell_days":
        days = f"{f.actual:.0f} {plural(round(f.actual), 'dzień', 'dni', 'dni')}"
        if same:
            return f"sprzedaż trwa średnio {days}, tak jak zakładam"
        return f"sprzedaż trwa średnio {days} (zakładam {f.expected:.0f})"
    return None


@dataclass
class Insight:
    key: tuple[str, tuple[str, ...]]
    text: str
    active: bool  # poprawka działa (albo zadziała po włączeniu)
    count: int


def insights(by_key: dict[tuple, Correction], cfg: LearningConfig) -> list[Insight]:
    """Wnioski zwykłym językiem, np. „iPhone 12 ze zbitym ekranem: naprawa kosztuje Cię średnio o 18% więcej,
    niż zakładam (5 transakcji). Uwzględniam to w wycenie.”"""
    out: list[Insight] = []
    for key, corr in sorted(by_key.items(), key=lambda kv: (-kv[1].count, kv[0])):
        name = describe_key(key)
        if not corr.factors:
            out.append(Insight(key, f"{name}: {tx_count(corr.count)} — jeszcze bez wyniku do porównania "
                                    "(poprawki liczę po naprawie i sprzedaży).", False, corr.count))
            continue
        for metric in METRICS:
            f = corr.factors.get(metric)
            if f is None:
                continue
            what = _sentence(f, cfg)
            text = f"{name}: {what} ({tx_count(f.n)})."
            if not f.active:
                text += f" Za mało danych — poprawka od {cfg.min_transactions} transakcji."
            elif not f.applies:
                text += " Bez poprawki."
            elif metric == "sell_days":
                text += " Pokazuję to w szczegółach oferty."
            elif cfg.enabled:
                text += f" Uwzględniam to w wycenie (waga {f.weight * 100:.0f}%)."
            else:
                text += " Poprawki są wyłączone — nie uwzględniam."
            out.append(Insight(key, text, f.active, f.n))
    return out


@dataclass
class Summary:
    count: int = 0
    sold: int = 0
    profit: float = 0.0
    minutes: int = 0
    per_hour: float | None = None
    frozen: float = 0.0  # pieniądze w telefonach jeszcze nie sprzedanych (zakup + części + koszty)
    by_status: dict[str, int] = field(default_factory=dict)


def summarize(transactions: list[Transaction]) -> Summary:
    s = Summary(count=len(transactions))
    for tx in transactions:
        s.by_status[tx.status] = s.by_status.get(tx.status, 0) + 1
        if tx.sold:
            s.sold += 1
            s.profit += tx.profit or 0.0
            s.minutes += tx.minutes or 0
        else:
            s.frozen += tx.total_cost
    s.profit, s.frozen = round(s.profit, 2), round(s.frozen, 2)
    if s.minutes:
        s.per_hour = round(s.profit / (s.minutes / 60), 2)
    return s
