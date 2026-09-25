"""Opcjonalna analiza opisów ogłoszeń przez Claude (Anthropic API).

Reguły tekstowe (``normalizer``/``red_flags``) są szybkie i darmowe, ale nie
rozumieją każdego sformułowania. Po włączeniu w ustawieniach nowe oferty z
opisem są wysyłane partiami do modelu, który zwraca listę usterek i czerwonych
flag w ściśle określonym formacie JSON (structured outputs). Wynik trafia do
bazy i jest łączony z wynikiem reguł (suma zbiorów) — AI może tylko dodać
znaleziska, nigdy ich nie usuwa.

Koszty: analizowane są tylko nowe oferty, maksymalnie ``llm_max_per_scan``
na odświeżenie, a każda oferta tylko raz.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from ..core.models import Defect, RedFlag

log = logging.getLogger(__name__)

# Flagi, które da się rozpoznać z tekstu (pozostałe liczy aplikacja).
TEXT_FLAGS = (
    RedFlag.ICLOUD_LOCK, RedFlag.IMEI_BLOCKED, RedFlag.MDM, RedFlag.REPLICA, RedFlag.SIMLOCK,
    RedFlag.NO_SIGNAL, RedFlag.NON_ORIGINAL_PARTS, RedFlag.UNTESTED,
)
MAX_DESCRIPTION_CHARS = 1500

SYSTEM_PROMPT = """Analizujesz polskie ogłoszenia sprzedaży używanych iPhone'ów dla osoby, która kupuje \
telefony, naprawia je i odsprzedaje. Dla każdego ogłoszenia wskaż:
- defects: usterki, które według tytułu lub opisu telefon RZECZYWIŚCIE ma. Nie wpisuj usterek, którym \
sprzedający zaprzecza ("ekran cały", "bez rys", "Face ID działa"), ani części już wymienionych na sprawne.
  Kody: screen (wyświetlacz/szyba/dotyk), back_glass (tylna szyba), battery (słaba bateria, kondycja < 80%, \
komunikat serwisowy), charging_port, camera, camera_lens (szkiełko aparatu), face_id, speaker, microphone, \
buttons, housing (wgniecenia/wygięcia), no_power (nie włącza się, bootloop), water_damage.
- red_flags: sygnały ryzyka dla kupującego:
  icloud_lock (blokada iCloud/aktywacji, nieznane hasło Apple ID), imei_blocked (IMEI zablokowany, \
czarna lista, kradziony), mdm (profil firmowy/MDM), replica (podróbka), simlock, no_signal (brak zasięgu), \
non_original_parts (zamienniki, nieoryginalny ekran/bateria, komunikat o nieznanej części), \
untested ("nie sprawdzałem", "sprzedaję jak jest").
- note: jedno krótkie zdanie po polsku, najważniejsza informacja dla kupującego (lub pusty tekst).
Jeśli nic nie wskazuje na usterkę lub flagę, zwróć puste listy. Zwróć wynik dla każdego id."""


def _schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "results": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "defects": {"type": "array", "items": {"type": "string", "enum": [d.value for d in Defect]}},
                        "red_flags": {"type": "array", "items": {"type": "string",
                                                                 "enum": [f.value for f in TEXT_FLAGS]}},
                        "note": {"type": "string"},
                    },
                    "required": ["id", "defects", "red_flags", "note"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["results"],
        "additionalProperties": False,
    }


@dataclass
class AiListing:
    id: str
    title: str
    description: str


@dataclass
class AiFinding:
    defects: list[Defect]
    flags: list[RedFlag]
    note: str


class AiAnalysisError(Exception):
    pass


def build_prompt(listings: list[AiListing]) -> str:
    items = [
        {"id": x.id, "tytul": x.title, "opis": x.description[:MAX_DESCRIPTION_CHARS]}
        for x in listings
    ]
    return "Ogłoszenia (JSON):\n" + json.dumps(items, ensure_ascii=False)


def parse_results(text: str, ids: set[str]) -> dict[str, AiFinding]:
    data = json.loads(text)
    out: dict[str, AiFinding] = {}
    for r in data.get("results", []):
        rid = str(r.get("id"))
        if rid not in ids:
            continue
        defects = [Defect(d) for d in r.get("defects", []) if d in Defect._value2member_map_]
        flags = [RedFlag(f) for f in r.get("red_flags", []) if f in {t.value for t in TEXT_FLAGS}]
        out[rid] = AiFinding(list(dict.fromkeys(defects)), list(dict.fromkeys(flags)), str(r.get("note") or ""))
    return out


class ClaudeAnalyzer:
    """Wysyła partię ogłoszeń do Claude i zwraca znaleziska per id."""

    def __init__(self, api_key: str | None, model: str, client=None):
        if client is None:
            import anthropic

            # pusty klucz → SDK użyje ANTHROPIC_API_KEY ze zmiennych środowiskowych
            client = anthropic.Anthropic(api_key=api_key or None, timeout=120.0)
        self.client = client
        self.model = model

    def analyze(self, listings: list[AiListing]) -> dict[str, AiFinding]:
        if not listings:
            return {}
        import anthropic

        try:
            response = self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": build_prompt(listings)}],
                output_config={"effort": "low", "format": {"type": "json_schema", "schema": _schema()}},
                # przy odmowie modelu API samo ponawia zapytanie na zalecanym modelu zapasowym
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
            )
        except anthropic.AuthenticationError as e:
            raise AiAnalysisError("nieprawidłowy klucz API Anthropic") from e
        except anthropic.RateLimitError as e:
            raise AiAnalysisError("przekroczony limit zapytań API — spróbuj później") from e
        except anthropic.APIStatusError as e:
            raise AiAnalysisError(f"błąd API ({e.status_code}): {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise AiAnalysisError("brak połączenia z API Anthropic") from e

        if response.stop_reason == "refusal":
            raise AiAnalysisError("model odmówił analizy tej partii ogłoszeń")
        if response.stop_reason == "max_tokens":
            raise AiAnalysisError("odpowiedź modelu została ucięta (za duża partia)")
        text = next((b.text for b in response.content if b.type == "text"), "")
        try:
            return parse_results(text, {x.id for x in listings})
        except (json.JSONDecodeError, ValueError) as e:
            raise AiAnalysisError(f"niepoprawna odpowiedź modelu: {e}") from e
