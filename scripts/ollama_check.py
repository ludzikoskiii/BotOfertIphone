"""Sprawdzenie analizy opisów PROGRAMU na prawdziwej Ollamie z modelem qwen3:8b (GitHub Actions, CPU).

Kod programu (``OllamaClient`` + ``desc_model.analyze``) czyta 10 przykładowych opisów z pułapkami
(zaprzeczenia, wymienione części, „kupię”, samo etui) i porównuje wynik z oczekiwanym. Na procesorze
serwera CI jest wolniej niż na karcie graficznej — czas podany jest informacyjnie.
"""
from __future__ import annotations

import os
import sys
import time

from phonebot.core.models import Defect, RedFlag
from phonebot.ml.desc_model import analyze
from phonebot.ml.ollama import OllamaClient

MODEL = os.environ.get("OLLAMA_MODEL", "qwen3:8b")

# (tytuł, opis, model z tytułu, oczekiwane pola — tylko te, które da się jednoznacznie ocenić)
CASES = [
    ("iPhone 13 128GB", "Sprzedam iPhone 13 128 GB w kolorze niebieskim. Kondycja baterii 86%. Telefon w pełni "
     "sprawny, Face ID działa, bez blokad. Na ekranie drobne rysy.", "iPhone 13",
     {"is_phone": True, "storage_gb": 128, "battery_health": 86, "defects": set(), "flags": set()}),
    ("iPhone 12 Pro", "Telefon na części. Nie włącza się po zalaniu. Blokada iCloud, nie znam hasła.", "iPhone 12 Pro",
     {"is_phone": True, "for_parts": True, "defects": {Defect.NO_POWER, Defect.WATER_DAMAGE},
      "flags": {RedFlag.ICLOUD_LOCK}}),
    ("iPhone 11 64GB", "Zbity ekran, dotyk działa. Face ID nie działa po upadku. Reszta sprawna.", "iPhone 11",
     {"is_phone": True, "defects": {Defect.SCREEN, Defect.FACE_ID}, "flags": set()}),
    ("Etui iPhone 14 Pro", "Sprzedam etui silikonowe do iPhone 14 Pro, nowe, nieużywane.", "iPhone 14 Pro",
     {"is_phone": False}),
    ("iPhone 13 mini", "Sprzedam iPhone 13 mini 256 GB. Wymieniony ekran na zamiennik, telefon pokazuje komunikat "
     "o nieoryginalnej części. Bateria 79%.", "iPhone 13 mini",
     {"is_phone": True, "storage_gb": 256, "battery_health": 79, "flags": {RedFlag.NON_ORIGINAL_PARTS}}),
    ("iPhone XR", "Telefon z simlockiem na Orange, poza tym w pełni sprawny, bateria 90%.", "iPhone XR",
     {"is_phone": True, "battery_health": 90, "flags": {RedFlag.SIMLOCK}}),
    ("iPhone 14 128 GB", "Kupię iPhone 14 w dobrym stanie, płacę gotówką, odbiór osobisty.", "iPhone 14",
     {"is_phone": False}),
    ("iPhone 12", "Sprzedaję jak jest, nie sprawdzałem. Leżał w szufladzie.", "iPhone 12",
     {"is_phone": True, "flags": {RedFlag.UNTESTED}}),
    ("iPhone 13 Pro Max", "Stan idealny, 1 TB pamięci, bateria 91%, komplet z pudełkiem.", "iPhone 13 Pro Max",
     {"is_phone": True, "storage_gb": 1024, "battery_health": 91, "defects": set()}),
    ("iPhone 15", "Ekran cały, bez rys. Face ID działa. Tylna szyba pęknięta.", "iPhone 15",
     {"is_phone": True, "defects": {Defect.BACK_GLASS}}),
    # — dodatkowe przypadki (nie służyły do dopracowania promptu) —
    ("iPhone 12 mini 64GB", "Bateria 84%, wymieniona w serwisie Apple. Drobne rysy na obudowie. Wszystko działa.",
     "iPhone 12 mini", {"is_phone": True, "battery_health": 84, "defects": set(), "flags": set()}),
    ("iPhone 11 Pro", "Telefon firmowy z profilem MDM, nie da się usunąć. Poza tym działa bez zarzutu.", "iPhone 11 Pro",
     {"is_phone": True, "flags": {RedFlag.MDM}, "defects": set()}),
    ("iPhone 13", "Nie ładuje, gniazdo do wymiany. Głośnik trzeszczy. Ekran i obudowa w dobrym stanie.", "iPhone 13",
     {"is_phone": True, "defects": {Defect.CHARGING_PORT, Defect.SPEAKER}}),
    ("iPhone X 256GB", "Face ID działa, True Tone działa. Bateria nieoryginalna, telefon pokazuje komunikat "
     "o nieznanej części.", "iPhone X", {"is_phone": True, "storage_gb": 256, "flags": {RedFlag.NON_ORIGINAL_PARTS}}),
    ("Pudełko iPhone 13 Pro", "Samo pudełko po iPhone 13 Pro, bez telefonu. Stan idealny.", "iPhone 13 Pro",
     {"is_phone": False}),
    ("iPhone 14 Pro", "Zamienię iPhone 14 Pro na Samsunga S23 Ultra, bez dopłat.", "iPhone 14 Pro",
     {"is_phone": False}),
]


def main() -> int:
    with OllamaClient(os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434"), timeout_s=900) as client:
        status = client.status()
        print(f"Ollama {status.version}, modele: {status.models}", flush=True)
        if not status.has_model(MODEL):
            print(f"BRAK MODELU {MODEL}")
            return 1
        results = [run(client, think) for think in (False, True)]
    for think, (ok, total, times) in zip((False, True), results, strict=True):
        print(f"PODSUMOWANIE {'z myśleniem ' if think else 'bez myślenia'}: zgodność pól {ok}/{total} = "
              f"{ok / total:.0%}, czas na opis (CPU serwera CI): mediana {sorted(times)[len(times) // 2]:.1f} s")
    return 0


def run(client, think: bool) -> tuple[int, int, list[float]]:
    print(f"\n========== TRYB: {'z myśleniem' if think else 'bez myślenia'} ==========", flush=True)
    ok_fields = total_fields = 0
    times = []
    for title, desc, phone, expected in CASES:
        t = time.perf_counter()
        found = analyze(client, MODEL, title=title, description=desc, phone_model=phone, think=think)
        times.append(time.perf_counter() - t)
        got = {"is_phone": found.is_phone, "storage_gb": found.storage_gb, "battery_health": found.battery_health,
               "for_parts": found.for_parts, "defects": set(found.defects), "flags": set(found.flags)}
        marks = []
        for key, want in expected.items():
            total_fields += 1
            good = got[key] == want
            ok_fields += good
            marks.append(f"{'✔' if good else '✖'} {key}={_fmt(got[key])}" + ("" if good else f" (oczekiwano {_fmt(want)})"))
        print(f"\n[{times[-1]:5.1f} s] {title}: {desc[:70]}…", flush=True)
        print("   " + " | ".join(marks))
        if found.note:
            print(f"   uwaga: {found.note}")
        if found.rejected:
            print(f"   odrzucone przez program: {found.rejected}")
    print(f"\nZGODNOŚĆ PÓL: {ok_fields}/{total_fields} = {ok_fields / total_fields:.0%}", flush=True)
    return ok_fields, total_fields, times[1:] if not think else times


def _fmt(value) -> str:
    if isinstance(value, set):
        return "{" + ", ".join(sorted(v.value for v in value)) + "}"
    return str(value)


if __name__ == "__main__":
    sys.exit(main())
