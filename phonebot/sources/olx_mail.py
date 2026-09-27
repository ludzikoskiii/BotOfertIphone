"""OLX przez powiadomienia e-mail — oficjalny kanał OLX, bez pobierania stron ani wyników z serwisu.

OLX blokuje automatyczne pobieranie (CloudFront „403 Request blocked” — sprawdzone z serwerów GitHuba i z łącza
domowego), a jego Partner API służy tylko do zarządzania własnymi ogłoszeniami. Dlatego program nie pobiera
stron ani wyników wyszukiwania z OLX (miniatury zdjęć z maila ładuje jak program pocztowy). Zamiast tego:

1. Na OLX zapisujesz wyszukiwanie (np. „iPhone” w kategorii telefonów) z powiadomieniami e-mail.
2. OLX wysyła maile o nowych ogłoszeniach.
3. PhoneBot czyta te maile z Twojej skrzynki przez IMAP — tylko do odczytu (``BODY.PEEK``: maile zostają
   nieprzeczytane), jedno połączenie na skan, każdy mail pobierany raz (wynik zapamiętany w pliku cache).

Z maila znamy tytuł, cenę, miasto, link i zdjęcie (bez opisu i sprzedającego) — wycena opiera się na tytule.
Parser nie zakłada konkretnego szablonu maila: szuka linków do ogłoszeń OLX (także opakowanych w linki
śledzące) i wokół każdego z nich — ceny, tytułu i miejscowości. Jeśli są maile z OLX, a parser nie rozpozna
w nich żadnej oferty, portal dostaje status „zmiana formatu” (wykrycie, że źródło przestało działać).
"""
from __future__ import annotations

import asyncio
import email
import email.policy
import hashlib
import imaplib
import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from selectolax.parser import HTMLParser, Node

from ..core.models import RawOffer
from ..core.settings import Settings
from .allegro_api import SourceNeedsKeys
from .base import SearchQuery, SourceAdapter, SourceError, SourceFormatChanged, SourceNetworkError, register

log = logging.getLogger(__name__)

PARSER_VERSION = 1  # zmiana parsera → maile z cache są analizowane ponownie
TIMEOUT_S = 30
_AD_PATH = re.compile(r"/(?:d/)?oferta/", re.I)
_AD_ID = re.compile(r"-ID([0-9A-Za-z]+)\.html", re.I)
_PRICE = re.compile(r"(?<![\d,.])(\d{1,3}(?:[   .]\d{3})+|\d+)(?:,(\d{1,2}))?\s*zł", re.I)
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"')\]]+")
_DATE_WORDS = re.compile(r"\b(?:dzisiaj|wczoraj|odświeżono|\d{1,2}[.:]\d{2}|\d{1,2} \w+ \d{4})\b", re.I)
_SKIP_LINES = re.compile(r"^(?:do negocjacji|za darmo|zamienię|wyróżnione|nowe|zobacz|sprawdź|obserwuj)\b", re.I)
_OLX_SITE = ("olx.pl", "www.olx.pl", "m.olx.pl")
_TRACKING = re.compile(r"click|track|/ls/|redirect|/r/|/c/|/l/", re.I)
_ALERT_WORDS = ("nowe ogłoszenia", "nowych ogłoszeń", "nowe oferty", "wyszukiwani", "pasując")

# typowe serwery IMAP polskich skrzynek (podpowiedź w ustawieniach)
IMAP_HOSTS = {"gmail.com": "imap.gmail.com", "googlemail.com": "imap.gmail.com", "wp.pl": "imap.wp.pl",
              "o2.pl": "poczta.o2.pl", "tlen.pl": "poczta.o2.pl", "onet.pl": "imap.poczta.onet.pl",
              "op.pl": "imap.poczta.onet.pl", "vp.pl": "imap.poczta.onet.pl", "interia.pl": "poczta.interia.pl",
              "interia.eu": "poczta.interia.pl", "icloud.com": "imap.mail.me.com", "me.com": "imap.mail.me.com",
              "outlook.com": "outlook.office365.com", "hotmail.com": "outlook.office365.com"}


def guess_imap_host(address: str) -> str:
    domain = address.rsplit("@", 1)[-1].strip().lower()
    return IMAP_HOSTS.get(domain, f"imap.{domain}" if "." in domain else "")


# ------------------------------------------------------------ parsowanie maila ---

def _unwrap(url: str, depth: int = 0) -> str:
    """Link śledzący (np. ``…/click?url=https%3A%2F%2Fwww.olx.pl%2F…``) → docelowy adres, gdy da się go odczytać."""
    parts = urlsplit(url)
    if parts.hostname and parts.hostname.endswith("olx.pl") and _AD_PATH.search(parts.path):
        return url
    if depth < 3:
        for values in parse_qs(parts.query).values():
            for v in values:
                v = unquote(v)
                if v.startswith("http"):
                    inner = _unwrap(v, depth + 1)
                    if inner != v or "olx.pl" in (urlsplit(v).hostname or ""):
                        return inner
    return url


def ad_link(href: str | None) -> tuple[str, str | None] | None:
    """Link do ogłoszenia OLX → (adres bez parametrów śledzących, ID ogłoszenia albo None).

    ``None`` — link nie prowadzi do ogłoszenia (logo, ustawienia powiadomień, wypisanie się…).
    Link śledzący domeny OLX, którego celu nie da się odczytać, jest akceptowany bez ID."""
    if not href or not href.startswith("http"):
        return None
    url = _unwrap(href.strip())
    parts = urlsplit(url)
    host = parts.hostname or ""
    if host.endswith("olx.pl") and _AD_PATH.search(parts.path):
        m = _AD_ID.search(parts.path)
        clean = f"https://{host}{parts.path}"
        return clean, (m.group(1) if m else parts.path.rstrip("/").rsplit("/", 1)[-1][:80])
    tail = (parts.path + "?" + parts.query).lower()
    if "olx" in host and host not in _OLX_SITE and _TRACKING.search(parts.path) \
            and not any(w in tail for w in ("unsubscribe", "wypisz", "settings", "ustawienia", "logo")):
        return url, None
    return None


def parse_price_text(text: str) -> tuple[float | None, bool]:
    """Pierwsza cena w złotych z tekstu karty + czy „do negocjacji”."""
    m = _PRICE.search(text)
    if not m:
        return None, False
    whole = re.sub(r"[   .]", "", m.group(1))
    value = float(whole) + (float(f"0.{m.group(2)}") if m.group(2) else 0.0)
    return value, "negocjac" in text.lower()


@dataclass
class MailOffer:
    url: str
    ad_id: str | None
    title: str
    price: float
    city: str | None = None
    photo: str | None = None
    negotiable: bool = False

    def key(self) -> str:
        if self.ad_id:
            return self.ad_id
        # bez ID (link śledzący): stały skrót z treści — ta sama oferta w kilku mailach = jedna oferta
        base = f"{self.title.lower()}|{self.price:.0f}|{(self.city or '').lower()}"
        return "m" + hashlib.sha1(base.encode()).hexdigest()[:16]


def _lines(node: Node) -> list[str]:
    return [ln.strip() for ln in node.text(separator="\n").splitlines() if ln.strip()]


def _city_from(lines: list[str], title: str) -> str | None:
    for ln in lines:
        if ln == title or _PRICE.search(ln) or _SKIP_LINES.match(ln) or len(ln) > 60:
            continue
        head = re.split(r"\s+[-–—]\s+", ln)[0].strip()
        head = head.split(",")[0].strip()
        if head and not any(ch.isdigit() for ch in head) and head[0].isupper() and len(head.split()) <= 4 \
                and not _DATE_WORDS.search(head):
            return head
    return None


def _card(anchor: Node, key: str, links_of) -> Node:
    """Najmniejszy element wokół linku, który zawiera cenę i nie zawiera innych ogłoszeń."""
    node, best = anchor, anchor
    for _ in range(10):
        parent = node.parent
        if parent is None or parent.tag in ("body", "html"):
            break
        if {k for k in links_of(parent) if k} - {key}:
            break  # rodzic obejmuje już inne ogłoszenie
        node = best = parent
        if _PRICE.search(parent.text(separator=" ")):
            break
    return best


def offers_from_html(html: str) -> list[MailOffer]:
    tree = HTMLParser(html)

    def key_of(a: Node) -> str | None:
        link = ad_link(a.attributes.get("href"))
        if not link:
            return None
        return link[1] or link[0]

    def links_of(node: Node) -> list[str | None]:
        return [key_of(a) for a in node.css("a[href]")]

    groups: dict[str, list[Node]] = {}
    for a in tree.css("a[href]"):
        k = key_of(a)
        if k:
            groups.setdefault(k, []).append(a)
    out: list[MailOffer] = []
    for key, anchors in groups.items():
        card = _card(anchors[0], key, links_of)
        text = card.text(separator=" ")
        price, negotiable = parse_price_text(text)
        if price is None:
            continue  # link bez ceny (np. „zobacz wszystkie”) — to nie karta ogłoszenia
        link = ad_link(anchors[0].attributes.get("href"))
        titles = [a.text(strip=True) for a in anchors]
        titles += [img.attributes.get("alt") or "" for img in card.css("img")]
        titles = [t for t in titles if t and not _PRICE.search(t) and len(t) >= 4]
        if not titles:
            titles = [ln for ln in _lines(card) if not _PRICE.search(ln) and not _SKIP_LINES.match(ln)]
        if not titles:
            continue
        title = max(titles, key=len)[:200]
        photo = next((img.attributes.get("src") for img in card.css("img")
                      if (img.attributes.get("src") or "").startswith("http")
                      and not re.search(r"logo|icon|pixel|spacer|tracking", img.attributes.get("src") or "", re.I)),
                     None)
        out.append(MailOffer(link[0], link[1], title, price, _city_from(_lines(card), title), photo, negotiable))
    return out


def offers_from_text(text: str) -> list[MailOffer]:
    """Wersja tekstowa maila (bez HTML): tytuł i cena w liniach przed linkiem do ogłoszenia."""
    out: list[MailOffer] = []
    lines = [ln.strip() for ln in text.splitlines()]
    for i, ln in enumerate(lines):
        for url in _URL_IN_TEXT.findall(ln):
            link = ad_link(url)
            if not link:
                continue
            window: list[str] = []
            for x in reversed(lines[max(0, i - 5):i]):  # linie nad linkiem, do pustej linii
                if not x or _URL_IN_TEXT.search(x):
                    break
                window.insert(0, x)
            price, negotiable = parse_price_text(" ".join(window))
            names = [x for x in window if not _PRICE.search(x) and not _SKIP_LINES.match(x)]
            if price is None or not names:
                continue
            out.append(MailOffer(link[0], link[1], names[0][:200], price, _city_from(window[1:], names[0]),
                                 None, negotiable))
    return out


def parse_message(raw: bytes) -> tuple[list[MailOffer], bool, datetime | None]:
    """Mail → (oferty, czy wygląda na powiadomienie o ogłoszeniach, data maila)."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    subject = str(msg.get("Subject") or "")
    try:
        when = parsedate_to_datetime(msg["Date"]) if msg.get("Date") else None
    except (TypeError, ValueError):
        when = None
    html_part = msg.get_body(preferencelist=("html",))
    text_part = msg.get_body(preferencelist=("plain",))
    html = html_part.get_content() if html_part is not None else ""
    text = text_part.get_content() if text_part is not None else ""
    offers = offers_from_html(html) if html else []
    if not offers and text:
        offers = offers_from_text(text)
    unique = {o.key(): o for o in offers}
    # powiadomienie o ogłoszeniach: rozpoznane oferty albo temat w stylu „Nowe ogłoszenia: iPhone” —
    # jeśli takie maile przychodzą, a oferty przestają być rozpoznawane, OLX zmienił wygląd maili
    alert = bool(unique) or any(w in subject.lower() for w in _ALERT_WORDS)
    return list(unique.values()), alert, when


def to_raw(o: MailOffer, when: datetime | None) -> RawOffer:
    return RawOffer(source=OlxMailAdapter.key, source_id=o.key(), url=o.url, title=o.title, price=o.price,
                    city=o.city, photos=[o.photo] if o.photo else [], created_at=when,
                    negotiable=o.negotiable or None, params={"via": "email"})


# ------------------------------------------------------------------- skrzynka ---

class MailCache:
    """Wyniki analizy maili (klucz: skrzynka + folder + UIDVALIDITY + UID) — każdy mail pobierany raz."""

    def __init__(self, path: Path):
        self.path = path
        self.entries: dict[str, dict[str, Any]] = {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if data.get("version") == PARSER_VERSION:
                self.entries = data.get("entries") or {}
        except (OSError, ValueError, AttributeError):
            pass

    def save(self, keep_since: datetime) -> None:
        cutoff = keep_since.isoformat()
        self.entries = {k: v for k, v in self.entries.items() if (v.get("date") or "") >= cutoff}
        try:
            self.path.write_text(json.dumps({"version": PARSER_VERSION, "entries": self.entries},
                                            ensure_ascii=False), encoding="utf-8")
        except OSError:
            log.warning("OLX (e-mail): nie zapisano cache %s", self.path)


@dataclass
class MailCheck:
    messages: int = 0  # maile od OLX w oknie czasu
    alerts: int = 0  # w tym wyglądające na powiadomienia o ogłoszeniach
    downloaded: int = 0  # pobrane w tym przebiegu (reszta z cache)
    offers: int = 0


def _imap_date(d: datetime) -> str:
    months = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
    return f"{d.day:02d}-{months[d.month - 1]}-{d.year}"


def collect(settings: Settings, cache: MailCache, *, imap_factory=None, now: datetime | None = None
            ) -> tuple[list[RawOffer], MailCheck]:
    """Czyta maile od OLX z ostatnich ``olx_mail_days`` dni (tylko do odczytu). Rzuca ``SourceError``."""
    s = settings
    now = now or datetime.now(UTC)
    since = now - timedelta(days=max(1, int(s.olx_mail_days)))
    host = s.olx_mail_host.strip() or guess_imap_host(s.olx_mail_user)
    factory = imap_factory or (lambda: imaplib.IMAP4_SSL(host, int(s.olx_mail_port or 993), timeout=TIMEOUT_S))
    check = MailCheck()
    offers: dict[str, RawOffer] = {}
    try:
        imap = factory()
    except (TimeoutError, OSError, imaplib.IMAP4.error) as e:
        raise SourceNetworkError(f"nie można połączyć się z serwerem poczty {host}: {e}") from e
    try:
        try:
            imap.login(s.olx_mail_user.strip(), s.olx_mail_password)
        except imaplib.IMAP4.error as e:
            raise SourceNeedsKeys(f"poczta odrzuciła logowanie ({e}). W Gmailu i większości skrzynek potrzebne "
                                  "jest „hasło aplikacji” i włączony dostęp IMAP — Ustawienia → Portale") from e
        folder = s.olx_mail_folder.strip() or "INBOX"
        typ, data = imap.select(f'"{folder}"', readonly=True)
        if typ != "OK":
            raise SourceNeedsKeys(f"nie ma folderu „{folder}” w skrzynce — Ustawienia → Portale")
        validity = ""
        try:
            resp = imap.response("UIDVALIDITY")[1]
            validity = (resp[0].decode() if resp and resp[0] else "")
        except Exception:  # noqa: BLE001
            pass
        sender = (s.olx_mail_sender.strip() or "olx.pl").replace('"', "")
        typ, data = imap.uid("SEARCH", None, "SINCE", _imap_date(since), "FROM", f'"{sender}"')
        if typ != "OK":
            raise SourceError(f"serwer poczty nie wykonał wyszukiwania ({typ})")
        uids = (data[0] or b"").split()[-max(1, int(s.olx_mail_max)):]
        check.messages = len(uids)
        prefix = f"{s.olx_mail_user.lower()}|{folder}|{validity}|"
        for uid in uids:
            key = prefix + uid.decode()
            entry = cache.entries.get(key)
            if entry is None:
                typ, parts = imap.uid("FETCH", uid, "(BODY.PEEK[])")  # PEEK — mail zostaje nieprzeczytany
                raw = next((p[1] for p in parts or [] if isinstance(p, tuple) and len(p) > 1), None)
                if typ != "OK" or raw is None:
                    continue
                found, alert, when = parse_message(raw)
                entry = {"date": (when or now).astimezone(UTC).isoformat(), "alert": alert,
                         "offers": [asdict(o) for o in found]}
                cache.entries[key] = entry
                check.downloaded += 1
            check.alerts += bool(entry.get("alert"))
            when = datetime.fromisoformat(entry["date"])
            for d in entry.get("offers") or []:
                raw_offer = to_raw(MailOffer(**d), when)
                offers.setdefault(raw_offer.source_id, raw_offer)  # najstarszy mail = data pojawienia się
    except (TimeoutError, OSError) as e:
        raise SourceNetworkError(f"przerwane połączenie z serwerem poczty: {e}") from e
    except imaplib.IMAP4.error as e:
        raise SourceError(f"błąd serwera poczty: {e}") from e
    finally:
        try:
            imap.logout()
        except Exception:  # noqa: BLE001
            pass
        cache.save(since)
    check.offers = len(offers)
    return list(offers.values()), check


class SourceNoMail(SourceError):
    """Brak maili z OLX — źródło działa, ale nie ma czego czytać (np. wyłączone powiadomienia)."""

    kind = "empty"


@register
class OlxMailAdapter(SourceAdapter):
    key = "olx"
    display_name = "OLX (e-mail)"
    default_enabled = False
    requires_keys = True
    config_hint = "wpisz adres e-mail i hasło aplikacji poczty — Ustawienia → Portale"

    def __init__(self, http, settings: Settings, *, imap_factory=None, cache_path: Path | None = None):
        self.settings = settings
        self._imap_factory = imap_factory
        self._cache_path = cache_path

    @staticmethod
    def configured(settings: Settings) -> bool:
        return bool(settings.olx_mail_user.strip() and settings.olx_mail_password)

    def _cache(self) -> MailCache:
        if self._cache_path is None:
            from ..paths import data_dir

            self._cache_path = data_dir() / "olx_mail_cache.json"
        return MailCache(self._cache_path)

    def check(self) -> tuple[list[RawOffer], MailCheck]:
        return collect(self.settings, self._cache(), imap_factory=self._imap_factory)

    async def search(self, query: SearchQuery) -> list[RawOffer]:
        if not self.configured(self.settings):
            raise SourceNeedsKeys("OLX (e-mail): brak adresu lub hasła poczty — Ustawienia → Portale")
        offers, check = await asyncio.to_thread(self.check)
        days = self.settings.olx_mail_days
        if check.messages == 0:
            raise SourceNoMail(f"brak maili od OLX z ostatnich {days} dni — sprawdź, czy zapisane wyszukiwanie "
                               "na OLX ma włączone powiadomienia e-mail i czy trafiają do wybranego folderu")
        if check.alerts and not offers:
            raise SourceFormatChanged(f"{check.alerts} maili z powiadomieniami OLX, ale żadnej rozpoznanej oferty — "
                                      "OLX zmienił wygląd maili, parser wymaga aktualizacji")
        floor, top = self.price_floor(query), query.price_max
        kept = [o for o in offers if (not floor or o.price >= floor) and (not top or o.price <= top)]
        log.info("OLX (e-mail): %d maili (%d pobranych), %d ofert, %d w zakresie cen",
                 check.messages, check.downloaded, len(offers), len(kept))
        return kept
