"""Ta sama sztuka wystawiona na kilku portalach — w tabeli raz, z listą wszystkich portali.

Klucz: model, pamięć, cena zaokrąglona do 10 zł i miejscowość (``dedup_key`` z bazy). Łączone są tylko
oferty z **różnych** portali i ze znaną miejscowością (bez niej dwa telefony za tę samą cenę to zwykle
dwie różne osoby). Zostaje oferta z najlepszym werdyktem, a przy remisie najtańsza; pozostałe trafiają
do ``Offer.also_on`` (portal, adres, cena).
"""
from __future__ import annotations

from .models import Offer, Valuation


def merge_across_portals(rows: list[tuple[Offer, Valuation]]) -> list[tuple[Offer, Valuation]]:
    groups: dict[str, list[tuple[Offer, Valuation]]] = {}
    out: list[tuple[Offer, Valuation]] = []
    for row in rows:
        offer = row[0]
        offer.also_on = []
        key = offer.dedup_key
        if not key or not (offer.raw.city or "").strip():
            out.append(row)
            continue
        groups.setdefault(key, []).append(row)
    for group in groups.values():
        by_source: dict[str, tuple[Offer, Valuation]] = {}
        extra: list[tuple[Offer, Valuation]] = []
        for row in group:  # dwie oferty z tego samego portalu to osobne ogłoszenia — nie łączymy ich
            if row[0].raw.source in by_source:
                extra.append(row)
            else:
                by_source[row[0].raw.source] = row
        merged = list(by_source.values())
        if len(merged) > 1:
            merged.sort(key=lambda r: (-r[1].verdict.rank, r[0].price))
            main, rest = merged[0], merged[1:]
            main[0].also_on = [(o.raw.source, o.raw.url, o.price) for o, _ in rest]
            out.append(main)
        else:
            out += merged
        out += extra
    return out
