"""Klient Ollama — lokalny model językowy na Twoim komputerze (opcjonalny, domyślnie wyłączony).

Ollama to darmowy program, który uruchamia model (np. Qwen3 8B) na karcie graficznej i udostępnia go
pod adresem ``http://127.0.0.1:11434``. Nic nie wychodzi poza komputer. Program działa normalnie bez
Ollamy — wtedy analiza opisów jest po prostu pomijana.

Używane zapytania API: ``/api/version``, ``/api/tags`` (zainstalowane modele), ``/api/chat`` (odpowiedź
w ściśle określonym formacie JSON: ``format`` = schemat JSON) i ``/api/pull`` (pobranie modelu).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

log = logging.getLogger(__name__)

DEFAULT_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3:8b"
SUGGESTED_MODELS = ("qwen3:8b", "qwen2.5:7b")
DOWNLOAD_URL = "https://ollama.com/download"


class OllamaError(Exception):
    pass


class OllamaNotRunning(OllamaError):
    pass


class OllamaModelMissing(OllamaError):
    pass


@dataclass
class OllamaStatus:
    running: bool
    version: str | None = None
    models: list[str] = field(default_factory=list)
    error: str | None = None

    def has_model(self, name: str) -> bool:
        return model_installed(name, self.models)


def _base(name: str) -> str:
    return name if ":" in name else f"{name}:latest"


def model_installed(name: str, installed: list[str]) -> bool:
    """„qwen3” = „qwen3:latest”; wielkość liter bez znaczenia."""
    want = _base(name.strip().lower())
    return any(_base(m.lower()) == want for m in installed)


class OllamaClient:
    def __init__(self, base_url: str = DEFAULT_URL, *, timeout_s: float = 180.0,
                 transport: httpx.BaseTransport | None = None):
        self.base_url = (base_url or DEFAULT_URL).rstrip("/")
        # trust_env=False: zapytania do własnego komputera nie mogą iść przez serwer pośredniczący (proxy)
        self._client = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(timeout_s, connect=3.0),
                                    trust_env=False, transport=transport)
        self._think_supported = True

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OllamaClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ------------------------------------------------------------ stan ---

    def _get(self, path: str) -> dict[str, Any]:
        try:
            r = self._client.get(path)
        except httpx.ConnectError as e:
            raise OllamaNotRunning(f"Ollama nie działa pod adresem {self.base_url} — uruchom program Ollama") from e
        except httpx.HTTPError as e:
            raise OllamaError(f"brak odpowiedzi od Ollamy: {e.__class__.__name__}") from e
        if r.status_code != 200:
            raise OllamaError(f"Ollama: HTTP {r.status_code}")
        return r.json()

    def status(self) -> OllamaStatus:
        try:
            version = str(self._get("/api/version").get("version") or "?")
            tags = self._get("/api/tags").get("models") or []
        except OllamaError as e:
            return OllamaStatus(False, error=str(e))
        models = [str(m.get("name") or m.get("model")) for m in tags if isinstance(m, dict)]
        return OllamaStatus(True, version, models)

    # ------------------------------------------------------------ czat ---

    def chat_json(self, model: str, system: str, user: str, schema: dict[str, Any], *,
                  options: dict[str, Any] | None = None, keep_alive: str = "5m") -> dict[str, Any]:
        """Jedna odpowiedź modelu w formacie JSON zgodnym ze schematem (bez „myślenia” — szybciej)."""
        body: dict[str, Any] = {
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "stream": False,
            "format": schema,
            "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 512, **(options or {})},
            "keep_alive": keep_alive,
        }
        if self._think_supported:
            body["think"] = False  # Qwen3: bez etapu „myślenia” (kilka razy szybciej, wynik ten sam)
        r = self._post("/api/chat", body)
        if r.status_code == 400 and "think" in r.text.lower() and "think" in body:
            # starsza Ollama albo model bez trybu myślenia — zapytanie bez tego pola
            self._think_supported = False
            body.pop("think")
            r = self._post("/api/chat", body)
        if r.status_code == 404:
            raise OllamaModelMissing(f"model {model} nie jest zainstalowany w Ollamie (Ustawienia → AI lokalne "
                                     "→ „Pobierz model”)")
        if r.status_code != 200:
            raise OllamaError(f"Ollama: HTTP {r.status_code} — {_error_text(r)}")
        content = ((r.json().get("message") or {}).get("content") or "").strip()
        try:
            data = json.loads(content)
        except json.JSONDecodeError as e:
            raise OllamaError("model zwrócił niepoprawny JSON") from e
        if not isinstance(data, dict):
            raise OllamaError("model zwrócił niepoprawny JSON")
        return data

    def _post(self, path: str, body: dict[str, Any]) -> httpx.Response:
        try:
            return self._client.post(path, json=body)
        except httpx.ConnectError as e:
            raise OllamaNotRunning(f"Ollama nie działa pod adresem {self.base_url} — uruchom program Ollama") from e
        except httpx.TimeoutException as e:
            raise OllamaError("Ollama nie odpowiedziała w czasie — model może być za duży dla karty graficznej") from e
        except httpx.HTTPError as e:
            raise OllamaError(f"brak odpowiedzi od Ollamy: {e.__class__.__name__}") from e

    # ------------------------------------------------------- pobieranie ---

    def pull(self, model: str, progress: Callable[[str, int, int], None] | None = None,
             stop: Callable[[], bool] | None = None) -> None:
        """Pobiera model do Ollamy (raz; kilka GB). ``progress(status, zrobione, razem)``."""
        try:
            with self._client.stream("POST", "/api/pull", json={"model": model, "stream": True},
                                     timeout=httpx.Timeout(None, connect=3.0)) as r:
                if r.status_code != 200:
                    raise OllamaError(f"Ollama: HTTP {r.status_code}")
                for line in r.iter_lines():
                    if stop and stop():
                        raise OllamaError("pobieranie przerwane")
                    if not line.strip():
                        continue
                    msg = json.loads(line)
                    if msg.get("error"):
                        raise OllamaError(f"Ollama: {msg['error']}")
                    if progress:
                        progress(str(msg.get("status") or ""), int(msg.get("completed") or 0),
                                 int(msg.get("total") or 0))
                    if msg.get("status") == "success":
                        return
        except httpx.ConnectError as e:
            raise OllamaNotRunning(f"Ollama nie działa pod adresem {self.base_url} — uruchom program Ollama") from e
        except httpx.HTTPError as e:
            raise OllamaError(f"pobieranie modelu przerwane: {e.__class__.__name__}") from e
        raise OllamaError("pobieranie modelu nie zakończyło się poprawnie")


def _error_text(r: httpx.Response) -> str:
    try:
        return str(r.json().get("error") or r.text)[:200]
    except (json.JSONDecodeError, ValueError):
        return r.text[:200]
