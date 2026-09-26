"""Serwer wersji na telefon — w tle programu, na tej samej bazie.

Bezpieczeństwo (serwer NIE jest wystawiany do internetu):

* nasłuch wyłącznie na ``127.0.0.1`` (dostęp z telefonu przez ``tailscale serve`` — HTTPS tylko w Twojej
  sieci Tailscale) albo na adresie Tailscale ``100.x.y.z`` (sieć prywatna WireGuard). Inny adres — np.
  ``0.0.0.0`` albo adres w sieci domowej/publicznej — jest odrzucany w kodzie;
* PIN (hash PBKDF2), sesje z losowym tokenem (w bazie tylko skróty), ochrona CSRF formularzy,
  blokada po 5 błędnych PIN-ach, sprawdzanie nagłówka ``Host`` (ochrona przed DNS rebinding);
* nie używaj „Tailscale Funnel” — to wystawia usługę publicznie.

Bez zewnętrznych bibliotek (``http.server`` z biblioteki standardowej) — mniejszy plik exe i nic do instalowania.
"""
from __future__ import annotations

import io
import ipaddress
import json
import logging
import re
import socket
import sqlite3
import threading
import time
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlencode, urlparse

from ..core.models import Offer, OfferStatus, Valuation
from ..core.selection import is_picked
from ..core.settings import Settings
from ..core.sorting import FIELDS, level, sort_rows, spec_from_json
from ..core.view_filter import ViewFilter, matches
from ..services.evaluator import Evaluator
from ..services.offer_actions import mark_not_phone, mark_phone, set_status
from ..storage.db import connect
from ..storage.repositories import OfferRepository
from . import pages
from .auth import MIN_PIN_LEN, LoginThrottle, Sessions, csrf_token, verify_pin

log = logging.getLogger(__name__)

BIND_MODES = {
    "localhost": "Tylko ten komputer — telefon przez „tailscale serve” (HTTPS, zalecane)",
    "tailscale": "Adres Tailscale 100.x.y.z (telefon łączy się bezpośrednio, HTTP w tunelu Tailscale)",
}
DEFAULT_PORT = 8765
COOKIE = "pb_session"
CACHE_S = 20
PAGE_SIZE = 100
_TAILNET = ipaddress.ip_network("100.64.0.0/10")
_TAILNET6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
_OFFER_PATH = re.compile(r"^/oferta/(\d+)(/akcja)?$")


class WebError(Exception):
    pass


def tailscale_ip() -> str | None:
    """Adres tego komputera w sieci Tailscale (bez wysyłania pakietów: „połączenie” UDP tylko wybiera trasę)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("100.100.100.100", 53))
            ip = s.getsockname()[0]
    except OSError:
        return None
    return ip if ipaddress.ip_address(ip) in _TAILNET else None


def allowed_bind(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return ip.is_loopback or ip in _TAILNET or ip in _TAILNET6


def resolve_bind(mode: str) -> str:
    if mode == "tailscale":
        ip = tailscale_ip()
        if ip is None:
            raise WebError("nie znaleziono adresu Tailscale (100.x.y.z) — zainstaluj i uruchom Tailscale "
                           "albo wybierz „Tylko ten komputer” i „tailscale serve”")
        return ip
    return "127.0.0.1"


# ---------------------------------------------------------------- dane ---

class WebApp:
    """Stan serwera: ustawienia (podmieniane przez okno programu), sesje, pamięć podręczna wycen."""

    def __init__(self, db_path, settings: Settings):
        self.db_path = db_path
        self.settings = settings
        self.sessions = Sessions(self.connect)
        self.throttle = LoginThrottle()
        self.changes = 0  # licznik zmian z telefonu — okno programu odświeża wtedy tabelę
        self.allowed_hosts: set[str] = {"127.0.0.1", "localhost"}
        self._cache: tuple[float, list[tuple[Offer, Valuation]]] | None = None
        self._lock = threading.Lock()
        self._icons: dict[int, bytes] = {}

    def connect(self) -> sqlite3.Connection:
        return connect(self.db_path)

    def invalidate(self) -> None:
        with self._lock:
            self._cache = None

    def rows(self) -> list[tuple[Offer, Valuation]]:
        """Oferty z wyceną (jak w oknie programu) — liczone najwyżej co ``CACHE_S`` sekund."""
        with self._lock:
            if self._cache and time.monotonic() - self._cache[0] < CACHE_S:
                return self._cache[1]
        conn = self.connect()
        try:
            repo = OfferRepository(conn)
            offers = repo.list() + repo.list_picked_inactive()
            rows = Evaluator(conn, self.settings).evaluate_all(offers)
        finally:
            conn.close()
        with self._lock:
            self._cache = (time.monotonic(), rows)
        return rows

    def offer(self, offer_id: int) -> tuple[Offer, Valuation] | None:
        for o, v in self.rows():
            if o.id == offer_id:
                return o, v
        conn = self.connect()
        try:
            offer = OfferRepository(conn).get(offer_id)
            return (offer, Evaluator(conn, self.settings).evaluate(offer)) if offer else None
        finally:
            conn.close()

    def icon(self, size: int) -> bytes:
        if size not in self._icons:
            self._icons[size] = _draw_icon(size)
        return self._icons[size]


def _draw_icon(size: int) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (size, size), "#2b8a3e")
    d = ImageDraw.Draw(img)
    s = size / 256
    d.rounded_rectangle((58 * s, 14 * s, 198 * s, 242 * s), radius=int(30 * s), fill="#212529")
    d.rounded_rectangle((70 * s, 30 * s, 186 * s, 226 * s), radius=int(18 * s), fill="#2b8a3e")
    try:
        font = ImageFont.load_default(size=int(70 * s))
    except TypeError:  # starszy Pillow
        font = ImageFont.load_default()
    d.text((128 * s, 128 * s), "zł", fill="white", font=font, anchor="mm")
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


# -------------------------------------------------------------- żądania ---

def make_handler(app: WebApp):
    class Handler(BaseHTTPRequestHandler):
        server_version = "PhoneBot"
        sys_version = ""

        def log_message(self, fmt, *args):  # bez PIN-ów i tokenów w logach
            log.debug("www %s %s", self.command, self.path.split("?")[0])

        # --- pomocnicze ---

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
            return host in app.allowed_hosts or host.endswith(".ts.net")

        def _session(self) -> str | None:
            jar = cookies.SimpleCookie(self.headers.get("Cookie") or "")
            token = jar[COOKIE].value if COOKIE in jar else None
            return token if app.sessions.valid(token) else None

        def _send(self, status: int, body: bytes | str, ctype: str = "text/html; charset=utf-8",
                  headers: dict | None = None, cache: bool = False) -> None:
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src * data:; style-src 'unsafe-inline'; "
                                                        "script-src 'unsafe-inline' 'self'; form-action 'self'; "
                                                        "frame-ancestors 'none'")
            self.send_header("Cache-Control", "public, max-age=86400" if cache else "no-store")
            for k, v in (headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(data)

        def _redirect(self, location: str, headers: dict | None = None) -> None:
            self._send(303, b"", headers={"Location": location, **(headers or {})})

        def _cookie(self, value: str, max_age: int) -> str:
            secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
            return f"{COOKIE}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

        def _form(self) -> dict[str, str]:
            length = min(int(self.headers.get("Content-Length") or 0), 16_384)
            data = parse_qs(self.rfile.read(length).decode("utf-8", "replace"))
            return {k: v[0] for k, v in data.items()}

        # --- GET ---

        def do_HEAD(self):  # noqa: N802
            self.do_GET()

        def do_GET(self):  # noqa: N802
            if not self._host_ok():
                return self._send(400, "Nieznany adres")
            url = urlparse(self.path)
            path = url.path
            if path == "/manifest.webmanifest":
                return self._send(200, json.dumps(_MANIFEST, ensure_ascii=False), "application/manifest+json", cache=True)
            if path == "/sw.js":
                return self._send(200, _SERVICE_WORKER, "text/javascript; charset=utf-8")
            if path in ("/icon-192.png", "/icon-512.png"):
                return self._send(200, app.icon(192 if "192" in path else 512), "image/png", cache=True)
            if path == "/health":
                return self._send(200, "ok", "text/plain; charset=utf-8")
            if path == "/login":
                nxt = parse_qs(url.query).get("next", ["/"])[0]
                return self._send(200, pages.login_page(locked_for=app.throttle.locked_for(), next_url=nxt))
            session = self._session()
            if session is None:
                return self._redirect("/login?" + urlencode({"next": self.path}))
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
            if path == "/":
                return self._send(200, self._list(params, session))
            m = _OFFER_PATH.match(path)
            if m and not m.group(2):
                found = app.offer(int(m.group(1)))
                if found is None:
                    return self._send(404, pages.message_page("Tej oferty już nie ma w bazie."))
                return self._send(200, pages.details_page(*found, app.settings, csrf=csrf_token(session),
                                                          style=params.get("styl"), key=params.get("szablon")))
            return self._send(404, pages.message_page("Nie ma takiej strony."))

        def _list(self, params: dict[str, str], session: str) -> str:
            s = app.settings
            list_key = "picked" if params.get("lista") in ("picked", "wybrane") else "all"
            rows = app.rows()
            f = ViewFilter(models=[params["model"]] if params.get("model") else [],
                           sources=[params["portal"]] if params.get("portal") else [], text=params.get("q", ""))
            try:
                f.price_max = float(params.get("cena_max") or 0)
                if params.get("zysk_min"):
                    f.min_profit_enabled, f.min_profit = True, float(params["zysk_min"])
            except ValueError:
                pass
            verdict = params.get("werdykt")

            def in_list(key, o, v):
                return is_picked(o, v, s.selection) if key == "picked" else o.active

            visible = [(o, v) for o, v in rows if matches(o, v, f) and (not verdict or v.verdict.value == verdict)]
            counts = {k: sum(1 for o, v in visible if in_list(k, o, v)) for k in ("all", "picked")}
            chosen = [(o, v) for o, v in visible if in_list(list_key, o, v)]
            if params.get("sort") in FIELDS:
                spec = [level(params["sort"], params.get("kier"))]
                if params["sort"] != "profit":
                    spec.append(level("profit", "desc"))
            else:
                spec = list(spec_from_json(s.table_sort.get(list_key)))
            sort_rows(chosen, spec)
            try:
                limit = max(PAGE_SIZE, min(int(params.get("limit") or PAGE_SIZE), 2000))
            except ValueError:
                limit = PAGE_SIZE
            models = sorted({o.parsed.model for o, _ in rows if o.parsed.model})
            return pages.list_page(chosen[:limit], list_key=list_key, counts=counts, params={**params, "lista": list_key},
                                   sort_spec=spec, models=models, shown=min(limit, len(chosen)), total=len(chosen),
                                   csrf=csrf_token(session))

        # --- POST ---

        def do_POST(self):  # noqa: N802
            if not self._host_ok():
                return self._send(400, "Nieznany adres")
            path = urlparse(self.path).path
            form = self._form()
            if path == "/login":
                return self._login(form)
            session = self._session()
            if session is None:
                return self._redirect("/login")
            if form.get("csrf") != csrf_token(session):
                return self._send(403, pages.message_page("Formularz wygasł — odśwież stronę."))
            if path == "/logout":
                app.sessions.revoke(session)
                return self._redirect("/login", {"Set-Cookie": self._cookie("", 0)})
            m = _OFFER_PATH.match(path)
            if m and m.group(2):
                return self._action(int(m.group(1)), form)
            return self._send(404, pages.message_page("Nie ma takiej strony."))

        def _login(self, form: dict[str, str]) -> None:
            wait = app.throttle.locked_for()
            if wait:
                return self._send(429, pages.login_page(locked_for=wait))
            stored = app.settings.web_pin_hash
            if not stored or not verify_pin(form.get("pin", ""), stored):
                app.throttle.failure()
                return self._send(401, pages.login_page("Nieprawidłowy PIN.", app.throttle.locked_for(),
                                                        next_url=form.get("next", "/")))
            app.throttle.success()
            token = app.sessions.create()
            nxt = form.get("next") or "/"
            if not nxt.startswith("/") or nxt.startswith("//") or "\\" in nxt:
                nxt = "/"
            return self._redirect(nxt, {"Set-Cookie": self._cookie(token, 30 * 86400)})

        def _action(self, offer_id: int, form: dict[str, str]) -> None:
            found = app.offer(offer_id)
            if found is None:
                return self._send(404, pages.message_page("Tej oferty już nie ma w bazie."))
            offer, _ = found
            action = form.get("a", "")
            conn = app.connect()
            try:
                if action in ("watch", "unwatch", "hide", "unhide"):
                    status = {"watch": OfferStatus.WATCHED, "hide": OfferStatus.HIDDEN}.get(action, OfferStatus.NEW)
                    set_status(conn, app.settings, offer, status)
                    target = f"/oferta/{offer_id}"
                elif action == "phone":
                    mark_phone(conn, offer)
                    target = f"/oferta/{offer_id}"
                elif action == "not_phone" and form.get("label") in ("accessory", "part", "wanted"):
                    text = mark_not_phone(conn, offer, form["label"])
                    app.invalidate()
                    app.changes += 1
                    return self._send(200, pages.message_page(text))
                else:
                    return self._send(400, pages.message_page("Nieznana akcja."))
            finally:
                conn.close()
            app.invalidate()
            app.changes += 1
            return self._redirect(target)

    return Handler


_MANIFEST = {
    "name": "PhoneBot — opłacalne iPhone'y", "short_name": "PhoneBot", "start_url": "/", "scope": "/",
    "display": "standalone", "background_color": "#f4f5f7", "theme_color": "#2b8a3e", "lang": "pl",
    "icons": [{"src": "/icon-192.png", "sizes": "192x192", "type": "image/png"},
              {"src": "/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any maskable"}],
}
# bez zapisywania stron w pamięci telefonu (zawsze aktualne dane); obsługa „fetch” jest potrzebna do instalacji
_SERVICE_WORKER = "self.addEventListener('install',e=>self.skipWaiting());" \
                  "self.addEventListener('fetch',e=>{});"


# --------------------------------------------------------------- serwer ---

class WebServer:
    """Uruchamiany i zatrzymywany przez okno programu (przełącznik w Ustawieniach → Telefon)."""

    def __init__(self, db_path, settings: Settings):
        self.app = WebApp(db_path, settings)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self.host: str | None = None
        self.port: int | None = None

    @property
    def running(self) -> bool:
        return self._httpd is not None

    def local_url(self) -> str | None:
        return f"http://{self.host}:{self.port}" if self.running else None

    def start(self, settings: Settings) -> str:
        """Uruchamia serwer; zwraca adres. ``WebError`` z opisem, gdy się nie da."""
        self.stop()
        if not settings.web_pin_hash:
            raise WebError(f"ustaw PIN (co najmniej {MIN_PIN_LEN} znaki) — bez PIN-u serwer się nie uruchomi")
        host = resolve_bind(settings.web_bind)
        if not allowed_bind(host):  # zabezpieczenie: nigdy 0.0.0.0 ani adres w sieci lokalnej/publicznej
            raise WebError(f"adres {host} jest niedozwolony — tylko 127.0.0.1 albo Tailscale 100.x.y.z")
        self.app.settings = settings
        self.app.allowed_hosts = {"127.0.0.1", "localhost", host}
        if settings.web_url:
            self.app.allowed_hosts.add((urlparse(settings.web_url).hostname or "").lower())
        try:
            httpd = ThreadingHTTPServer((host, int(settings.web_port)), make_handler(self.app))
        except OSError as e:
            raise WebError(f"nie można nasłuchiwać na {host}:{settings.web_port} ({e.strerror or e})") from e
        httpd.daemon_threads = True
        self._httpd, self.host, self.port = httpd, host, httpd.server_address[1]
        self._thread = threading.Thread(target=httpd.serve_forever, name="phonebot-www", daemon=True)
        self._thread.start()
        log.info("Wersja na telefon: %s", self.local_url())
        return self.local_url() or ""

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
            self._thread = None

    def update_settings(self, settings: Settings) -> None:
        self.app.settings = settings
        self.app.invalidate()

