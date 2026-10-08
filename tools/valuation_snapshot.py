"""Pełny zapis wyceny wszystkich ofert z bazy — do sprawdzenia, że zmiana kodu (np. optymalizacja) nie zmienia
wyników. Porównaj zapisy dwóch wersji programu na tej samej (skopiowanej) bazie:

    python tools/valuation_snapshot.py KOPIA.sqlite3 przed.json      # stara wersja programu
    python tools/valuation_snapshot.py KOPIA.sqlite3 po.json         # nowa wersja
    python tools/valuation_snapshot.py --compare przed.json po.json

Zapisywane jest wszystko, co zwraca wycena (werdykt, zysk, ocena, kolor, ryzyko z sygnałami, rynek, powody,
negocjacja…), dla każdej oferty w bazie (także ukrytych, nieaktualnych i z archiwum) w obu trybach.
"""
from __future__ import annotations

import dataclasses
import enum
import json
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _plain(value):
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_plain(v) for v in value]
        return sorted(items, key=repr) if isinstance(value, (set, frozenset)) else items
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, float):
        return repr(value)
    return value if isinstance(value, (str, int, bool, type(None))) else repr(value)


def snapshot(db: Path) -> dict:
    from phonebot.core.models import Mode
    from phonebot.services.evaluator import Evaluator
    from phonebot.storage.db import open_database
    from phonebot.storage.repositories import OfferRepository, SettingsRepository

    tmp = Path(tempfile.mkdtemp()) / "snap.sqlite3"
    shutil.copy(db, tmp)
    conn = open_database(tmp)
    settings = SettingsRepository(conn).load()
    offers = OfferRepository(conn).list(include_hidden=True, active_only=False, include_archived=True)
    out: dict = {}
    for mode in Mode:
        evaluator = Evaluator(conn, settings)
        for offer in offers:
            val = evaluator.evaluate(offer, mode)
            out[f"{mode.value}:{offer.id}"] = {"val": _plain(val), "distance_km": _plain(offer.distance_km),
                                               "transaction_id": offer.transaction_id}
    conn.close()
    return out


def compare(a: dict, b: dict) -> int:
    keys = sorted(set(a) | set(b))
    diffs = [k for k in keys if a.get(k) != b.get(k)]
    print(f"ofert × tryby: {len(keys)}, różnic: {len(diffs)}")
    for k in diffs[:10]:
        va, vb = a.get(k, {}), b.get(k, {})
        fields = sorted({f for f in (va.get("val") or {}) | (vb.get("val") or {})
                         if (va.get("val") or {}).get(f) != (vb.get("val") or {}).get(f)})
        print(f"  {k}: pola {fields[:8]}")
    return 1 if diffs else 0


def main() -> int:
    if sys.argv[1] == "--compare":
        a = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
        b = json.loads(Path(sys.argv[3]).read_text(encoding="utf-8"))
        return compare(a, b)
    data = snapshot(Path(sys.argv[1]))
    Path(sys.argv[2]).write_text(json.dumps(data, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    print(f"zapisano {len(data)} wycen → {sys.argv[2]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
