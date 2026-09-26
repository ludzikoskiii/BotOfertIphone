"""Kursy walut (NBP, oficjalne i darmowe API) i szacunek kosztów zakupu z zagranicy (eBay).

Kursy: tabela A NBP (``api.nbp.pl``) — raz dziennie, zapamiętane; bez połączenia używany jest ostatni
zapamiętany kurs (a przed pierwszym pobraniem — przybliżony kurs awaryjny, wyraźnie oznaczony).

Zakup spoza UE: VAT importowy (23%) od ceny z wysyłką i cłem, cło (telefony komórkowe — w UE 0%, kod
HS 8517.13; do zmiany w ustawieniach) i opłata za odprawę pobierana przez przewoźnika.
"""
from __future__ import annotations

from dataclasses import dataclass

NBP_URL = "https://api.nbp.pl/api/exchangerates/tables/A/?format=json"
# awaryjne kursy (tylko gdy NBP nigdy nie był osiągalny) — wynik oznaczony jako przybliżony
FALLBACK_RATES = {"PLN": 1.0, "EUR": 4.3, "USD": 3.9, "GBP": 5.1, "CHF": 4.6, "CZK": 0.18, "SEK": 0.39}

EU_COUNTRIES = frozenset({
    "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE", "IT", "LV", "LT", "LU",
    "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"})


@dataclass
class Rates:
    rates: dict[str, float]
    date: str  # data tabeli NBP albo „awaryjne”
    fallback: bool = False

    def to_pln(self, amount: float, currency: str) -> float | None:
        rate = self.rates.get((currency or "PLN").upper())
        return None if rate is None else round(amount * rate, 2)


def parse_nbp(data) -> Rates:
    table = data[0] if isinstance(data, list) else data
    rates = {"PLN": 1.0, **{r["code"]: float(r["mid"]) for r in table["rates"]}}
    return Rates(rates, str(table.get("effectiveDate") or ""))


def fallback_rates() -> Rates:
    return Rates(dict(FALLBACK_RATES), "awaryjne (brak połączenia z NBP)", fallback=True)


@dataclass
class ImportCosts:
    vat: float = 0.0
    duty: float = 0.0
    clearance_fee: float = 0.0

    @property
    def total(self) -> float:
        return round(self.vat + self.duty + self.clearance_fee, 2)


def import_costs(price_pln: float, shipping_pln: float, country: str | None, *, vat_pct: float = 23.0,
                 duty_pct: float = 0.0, clearance_fee: float = 30.0) -> ImportCosts:
    """Szacunek dla przesyłki spoza UE; z UE (i nieznanego kraju w UE) — zero."""
    if not country or country.upper() in EU_COUNTRIES:
        return ImportCosts()
    customs_value = price_pln + shipping_pln
    duty = round(customs_value * duty_pct / 100, 2)
    vat = round((customs_value + duty) * vat_pct / 100, 2)
    return ImportCosts(vat=vat, duty=duty, clearance_fee=float(clearance_fee))
