"""Odpowiedzialny klient HTTP: limit zapytań na host, ponawianie prób, cache.

- ``HostRateLimiter`` pilnuje minimalnego odstępu między zapytaniami do tego
  samego hosta (z losowym rozrzutem), wspólnie dla wszystkich skanów.
- ``ResponseCache`` trzyma odpowiedzi przez krótki czas, żeby wielokrotne
  „Odśwież" nie generowało ruchu.
- Błędy sieci, 429 i 5xx są ponawiane z rosnącym opóźnieniem (z uwzględnieniem
  nagłówka Retry-After); 403/404 kończą się od razu błędem źródła.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import threading
import time
from typing import Any
from urllib.parse import urlsplit

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    retry_if_exception,
    stop_after_attempt,
)

log = logging.getLogger(__name__)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "pl-PL,pl;q=0.9,en;q=0.6",
}


# Ślady ochrony antybotowej w odpowiedzi (nagłówki / treść).
_BLOCK_MARKERS = ("captcha-delivery.com", "datadome", "cf-chl", "challenge-platform", "attention required",
                  "px-captcha", "are you a robot", "jestes robotem", "jesteś robotem")


def looks_blocked(response: httpx.Response) -> bool:
    if response.status_code in (401, 403, 429):
        return True
    headers = " ".join(f"{k}:{v}" for k, v in response.headers.items()).lower()
    if "datadome" in headers and response.status_code >= 400:
        return True
    ctype = response.headers.get("content-type", "")
    if "html" in ctype and response.status_code >= 400:
        body = response.text[:5000].lower()
        return any(m in body for m in _BLOCK_MARKERS)
    return False


class HttpError(Exception):
    def __init__(self, message: str, status: int | None = None, retry_after: float | None = None,
                 *, blocked: bool = False, network: bool = False):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after
        self.blocked = blocked
        self.network = network

    @property
    def retryable(self) -> bool:
        if self.status == 429:
            return True
        if self.blocked:
            return False
        return self.network or (self.status is not None and self.status >= 500)


class HostRateLimiter:
    """Minimalny odstęp między zapytaniami do hosta; bezpieczny między wątkami i pętlami asyncio."""

    def __init__(self, delay_s: float, jitter: float = 0.25, clock=time.monotonic):
        self.delay_s = delay_s
        self.jitter = jitter
        self._clock = clock
        self._next: dict[str, float] = {}
        self._lock = threading.Lock()

    def reserve(self, host: str) -> float:
        """Rezerwuje termin kolejnego zapytania i zwraca, ile trzeba poczekać."""
        with self._lock:
            now = self._clock()
            slot = max(now, self._next.get(host, now))
            spacing = self.delay_s * random.uniform(1 - self.jitter, 1 + self.jitter)
            self._next[host] = slot + spacing
            return slot - now

    async def wait(self, host: str) -> None:
        delay = self.reserve(host)
        if delay > 0:
            await asyncio.sleep(delay)


class ResponseCache:
    def __init__(self, ttl_s: float = 120.0, max_entries: int = 500, clock=time.monotonic):
        self.ttl_s = ttl_s
        self.max_entries = max_entries
        self._clock = clock
        self._data: dict[str, tuple[float, str]] = {}
        self._lock = threading.Lock()

    def get(self, key: str) -> str | None:
        with self._lock:
            item = self._data.get(key)
            if item and self._clock() - item[0] < self.ttl_s:
                return item[1]
            self._data.pop(key, None)
            return None

    def put(self, key: str, body: str) -> None:
        with self._lock:
            if len(self._data) >= self.max_entries:
                oldest = min(self._data, key=lambda k: self._data[k][0])
                del self._data[oldest]
            self._data[key] = (self._clock(), body)


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value and value.isdigit():
        return min(float(value), 120.0)
    return None


def _wait(state: RetryCallState) -> float:
    exc = state.outcome.exception() if state.outcome else None
    if isinstance(exc, HttpError) and exc.retry_after:
        return exc.retry_after
    return min(60.0, 5.0 * 2 ** (state.attempt_number - 1)) * random.uniform(0.8, 1.2)


class HttpClient:
    """Asynchroniczny klient używany przez adaptery. Tworzony na czas jednego skanu."""

    def __init__(
        self,
        limiter: HostRateLimiter,
        cache: ResponseCache | None = None,
        *,
        attempts: int = 3,
        timeout_s: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        wait=_wait,
    ):
        self.limiter = limiter
        self.cache = cache
        self.attempts = attempts
        self._wait = wait
        self._client = httpx.AsyncClient(
            headers=DEFAULT_HEADERS, timeout=timeout_s, follow_redirects=True, transport=transport
        )
        #: gdy lista — każde zapytanie jest do niej dopisywane (tryb diagnostyki)
        self.trace: list[dict[str, Any]] | None = None

    @property
    def cookies(self) -> httpx.Cookies:
        return self._client.cookies

    async def request(self, method: str, url: str, **kw: Any) -> httpx.Response:
        """Pojedyncze zapytanie (z limitem tempa, bez ponawiania i cache) — zwraca pełną odpowiedź."""
        request = self._client.build_request(method, url, **kw)
        await self.limiter.wait(request.url.host)
        started = time.monotonic()
        try:
            response = await self._client.send(request)
        except httpx.TransportError as e:
            self._record(request, None, started, error=e.__class__.__name__)
            raise HttpError(f"Błąd sieci ({request.url.host}): {e.__class__.__name__}", network=True) from e
        self._record(request, response, started)
        return response

    def _record(self, request: httpx.Request, response: httpx.Response | None, started: float,
                error: str | None = None) -> None:
        if self.trace is None:
            return
        entry: dict[str, Any] = {"method": request.method, "url": str(request.url),
                                 "ms": round((time.monotonic() - started) * 1000)}
        if error:
            entry["error"] = error
        if response is not None:
            entry.update(status=response.status_code, content_type=response.headers.get("content-type", ""),
                         bytes=len(response.content), server=response.headers.get("server", ""),
                         blocked=looks_blocked(response),
                         set_cookies=sorted({c.split("=", 1)[0] for c in response.headers.get_list("set-cookie")}),
                         snippet=response.text[:300].replace("\n", " "))
        self.trace.append(entry)

    async def __aenter__(self) -> HttpClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_text(self, url: str, *, params: dict[str, Any] | None = None,
                       headers: dict[str, str] | None = None, use_cache: bool = True) -> str:
        request = self._client.build_request("GET", url, params=params, headers=headers)
        key = str(request.url)
        if use_cache and self.cache and (hit := self.cache.get(key)) is not None:
            log.debug("cache: %s", key)
            return hit

        async for attempt in AsyncRetrying(
            stop=stop_after_attempt(self.attempts),
            wait=self._wait,
            retry=retry_if_exception(lambda e: isinstance(e, HttpError) and e.retryable),
            reraise=True,
        ):
            with attempt:
                body = await self._send(request)
        if self.cache and use_cache:
            self.cache.put(key, body)
        return body

    async def get_json(self, url: str, **kw: Any) -> Any:
        text = await self.get_text(url, **kw)
        try:
            return json.loads(text)
        except json.JSONDecodeError as e:
            raise HttpError(f"Niepoprawny JSON z {urlsplit(url).netloc}: {e}") from e

    async def _send(self, request: httpx.Request) -> str:
        host = request.url.host
        await self.limiter.wait(host)
        started = time.monotonic()
        try:
            response = await self._client.send(request)
        except httpx.TransportError as e:
            self._record(request, None, started, error=e.__class__.__name__)
            log.warning("Błąd sieci %s: %s", host, e)
            raise HttpError(f"Błąd sieci ({host}): {e.__class__.__name__}", network=True) from e
        self._record(request, response, started)
        if response.status_code >= 400:
            blocked = looks_blocked(response)
            log.warning("HTTP %d z %s%s", response.status_code, request.url, " (blokada)" if blocked else "")
            raise HttpError(
                f"HTTP {response.status_code} z {host}", response.status_code, _retry_after(response),
                blocked=blocked,
            )
        return response.text
