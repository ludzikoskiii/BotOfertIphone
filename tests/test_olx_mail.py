"""OLX z powiadomień e-mail: parser maili, skrzynka IMAP (tylko do odczytu), pełna ścieżka i wykrywanie awarii.

Maile testowe odtwarzają typową budowę powiadomień (tabela kart, linki śledzące, logo, „zobacz wszystkie”,
wypisanie się). Test ``test_alert_mail_with_no_offers_is_format_change`` wykrywa, że źródło przestało zwracać
oferty mimo przychodzących powiadomień.
"""
from __future__ import annotations

import asyncio
import imaplib
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from urllib.parse import quote

import httpx
import pytest

from phonebot.core.models import Mode, Verdict
from phonebot.core.settings import Settings
from phonebot.net.http import HostRateLimiter, HttpClient
from phonebot.services.evaluator import Evaluator
from phonebot.services.scanner import Scanner
from phonebot.services.telegram_queue import TelegramQueue
from phonebot.sources import REGISTRY, SOURCE_NAMES
from phonebot.sources.allegro_api import SourceNeedsKeys
from phonebot.sources.base import SearchQuery, SourceFormatChanged, SourceNetworkError
from phonebot.sources.olx_mail import (
    MailCache,
    OlxMailAdapter,
    SourceNoMail,
    ad_link,
    collect,
    guess_imap_host,
    parse_message,
    parse_price_text,
)
from phonebot.storage.db import open_database
from phonebot.storage.repositories import FetchRunRepository, OfferRepository, SettingsRepository

NOW = datetime(2026, 9, 27, 9, 0, tzinfo=UTC)


def track(url: str) -> str:
    return f"https://click.olx.pl/track?u=123&url={quote(url, safe='')}"


def card(title, price, city, ad_id, *, tracked=True, extra=""):
    url = f"https://www.olx.pl/d/oferta/{title.lower().replace(' ', '-')}-CID99-ID{ad_id}.html?utm_source=email"
    href = track(url) if tracked else url
    return f"""
    <tr><td><table><tr>
      <td><a href="{href}"><img src="https://ireland.apollo.olxcdn.com/v1/files/{ad_id}/image;s=200x200"
           alt="{title}"></a></td>
      <td><a href="{href}" style="font-weight:bold">{title}</a>
          <p>{price}</p>{extra}
          <p>{city} - Dzisiaj o 08:15</p></td>
    </tr></table></td></tr>"""


ALERT_HTML = f"""<html><body><table>
  <tr><td><a href="https://www.olx.pl/"><img src="https://static.olx.pl/logo.png" alt="OLX"></a></td></tr>
  <tr><td><h1>Nowe ogłoszenia dla Twojego wyszukiwania „iphone”</h1></td></tr>
  {card("iPhone 13 128GB", "1 850 zł", "Nowy Targ", "abc12", extra="<p>do negocjacji</p>")}
  {card("iPhone 12 Pro 256GB zbity ekran", "1 100 zł", "Kraków, Krowodrza", "Qw9x", tracked=False)}
  {card("Etui iPhone 13 silikonowe", "25 zł", "Zakopane", "etui1")}
  {card("iPhone 14 Pro 128 GB", "3.200,50 zł", "Rabka-Zdrój", "zz77")}
  <tr><td><a href="{track('https://www.olx.pl/elektronika/telefony/q-iphone/')}">Zobacz wszystkie ogłoszenia</a></td></tr>
  <tr><td><a href="https://www.olx.pl/myaccount/settings/?unsubscribe=1">Wypisz się</a></td></tr>
</table></body></html>"""


def mail(subject: str, html: str | None = None, text: str | None = None, when: datetime = NOW,
         sender: str = "OLX <powiadomienia@olx.pl>") -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "jan@gmail.com", subject
    m["Date"] = when.strftime("%a, %d %b %Y %H:%M:%S +0000")
    m.set_content(text or "Wersja tekstowa.")
    if html:
        m.add_alternative(html, subtype="html")
    return bytes(m)


class FakeImap:
    """Skrzynka w pamięci: sprawdza, że program tylko czyta (readonly, BODY.PEEK) i niczego nie zmienia."""

    def __init__(self, messages: dict[bytes, bytes], *, password="dobre", folders=("INBOX",)):
        self.messages, self.password, self.folders = messages, password, folders
        self.fetched: list[bytes] = []
        self.commands: list[str] = []
        self.logged_out = False

    def login(self, user, password):
        if password != self.password:
            raise imaplib.IMAP4.error("[AUTHENTICATIONFAILED] Invalid credentials")
        return "OK", [b"ok"]

    def select(self, folder, readonly=False):
        assert readonly, "skrzynka musi być otwierana tylko do odczytu"
        return ("OK", [b"3"]) if folder.strip('"') in self.folders else ("NO", [b"no such folder"])

    def response(self, code):
        return code, [b"777"]

    def uid(self, command, *args):
        self.commands.append(command)
        if command == "SEARCH":
            assert "FROM" in args and "SINCE" in args
            return "OK", [b" ".join(self.messages)]
        if command == "FETCH":
            uid, what = args
            assert what == "(BODY.PEEK[])", "mail musi zostać nieprzeczytany"
            self.fetched.append(uid)
            return "OK", [(uid + b" (BODY[] {1}", self.messages[uid]), b")"]
        raise AssertionError(f"niedozwolona komenda IMAP: {command}")  # STORE / EXPUNGE / COPY…

    def logout(self):
        self.logged_out = True


def olx_settings(**kw) -> Settings:
    kw = {"olx_mail_user": "jan@gmail.com", "olx_mail_password": "dobre", **kw}
    s = Settings(mode=Mode.RESELL.value, **kw)
    s.enabled_sources.update({k: False for k in s.enabled_sources})
    s.enabled_sources["olx"] = True
    return s


# ---------------------------------------------------------------- parser ---

def test_parse_alert_mail():
    offers, alert, when = parse_message(mail("Nowe ogłoszenia: iphone", ALERT_HTML))
    by_id = {o.ad_id: o for o in offers}
    assert alert and when == NOW
    assert set(by_id) == {"abc12", "Qw9x", "etui1", "zz77"}  # logo, „zobacz wszystkie”, wypisanie — pominięte
    a = by_id["abc12"]
    assert (a.title, a.price, a.city, a.negotiable) == ("iPhone 13 128GB", 1850.0, "Nowy Targ", True)
    assert a.url == "https://www.olx.pl/d/oferta/iphone-13-128gb-CID99-IDabc12.html"  # bez śledzenia
    assert a.photo.startswith("https://ireland.apollo.olxcdn.com/")
    assert by_id["Qw9x"].city == "Kraków" and by_id["Qw9x"].price == 1100.0
    assert by_id["zz77"].price == 3200.5 and by_id["zz77"].city == "Rabka-Zdrój"


def test_plain_text_mail_and_links():
    text = ("Nowe ogłoszenia dla Ciebie\n\niPhone 11 64GB\n650 zł\nNowy Sącz\n"
            "https://www.olx.pl/d/oferta/iphone-11-64gb-CID99-IDt11.html\n\nUstawienia: https://www.olx.pl/myaccount/\n")
    offers, alert, _ = parse_message(mail("Nowe ogłoszenia: iphone", text=text))
    assert alert and [(o.ad_id, o.title, o.price, o.city) for o in offers] == [("t11", "iPhone 11 64GB", 650.0,
                                                                                "Nowy Sącz")]
    assert ad_link("https://www.olx.pl/myaccount/settings/") is None
    assert ad_link("https://example.com/d/oferta/x-IDa.html") is None
    assert ad_link(track("https://www.olx.pl/oferta/iphone-CID99-IDk1.html"))[1] == "k1"
    opaque = ad_link("https://click.olx.pl/ls/click?upn=abcdef")  # link śledzący bez odczytywalnego celu
    assert opaque == ("https://click.olx.pl/ls/click?upn=abcdef", None)
    assert parse_price_text("Cena: 2 499 zł do negocjacji") == (2499.0, True)
    assert parse_price_text("Za darmo") == (None, False)
    assert guess_imap_host("jan@gmail.com") == "imap.gmail.com" and guess_imap_host("a@wp.pl") == "imap.wp.pl"


def test_offer_without_id_gets_stable_key():
    html = ALERT_HTML.replace(track("https://www.olx.pl/d/oferta/iphone-14-pro-128-gb-CID99-IDzz77.html"
                                    "?utm_source=email"), "https://click.olx.pl/ls/click?upn=xyz")
    first = {o.title: o.key() for o in parse_message(mail("Nowe ogłoszenia", html))[0]}
    second = {o.title: o.key() for o in parse_message(mail("Nowe ogłoszenia", html))[0]}
    assert first["iPhone 14 Pro 128 GB"].startswith("m") and first == second


# ---------------------------------------------------------------- skrzynka ---

def _msgs():
    return {b"1": mail("Nowe ogłoszenia: iphone", ALERT_HTML, when=NOW - timedelta(days=1)),
            b"2": mail("Zmiana hasła w OLX", "<p>Twoje hasło zostało zmienione.</p>"),
            b"3": mail("Nowe ogłoszenia: iphone", card("iPhone 13 128GB", "1 850 zł", "Nowy Targ", "abc12"))}


def test_collect_reads_only_and_caches(tmp_path):
    imap = FakeImap(_msgs())
    cache = MailCache(tmp_path / "c.json")
    offers, check = collect(olx_settings(), cache, imap_factory=lambda: imap, now=NOW)
    assert (check.messages, check.alerts, check.downloaded) == (3, 2, 3)
    assert {o.source_id for o in offers} == {"abc12", "Qw9x", "etui1", "zz77"}  # ta sama oferta w 2 mailach = 1
    abc = next(o for o in offers if o.source_id == "abc12")
    assert abc.source == "olx" and abc.created_at == NOW - timedelta(days=1)  # data pierwszego maila
    assert imap.logged_out and set(imap.commands) == {"SEARCH", "FETCH"}
    # drugi przebieg: nic nie jest pobierane ponownie (cache na dysku)
    imap2 = FakeImap(_msgs())
    offers2, check2 = collect(olx_settings(), MailCache(tmp_path / "c.json"), imap_factory=lambda: imap2, now=NOW)
    assert check2.downloaded == 0 and imap2.fetched == [] and len(offers2) == 4


def test_errors_are_categorised(tmp_path):
    cache = MailCache(tmp_path / "c.json")
    with pytest.raises(SourceNeedsKeys, match="hasło aplikacji"):
        collect(olx_settings(olx_mail_password="złe"), cache, imap_factory=lambda: FakeImap(_msgs()), now=NOW)
    with pytest.raises(SourceNeedsKeys, match="folderu"):
        collect(olx_settings(olx_mail_folder="OLX"), cache, imap_factory=lambda: FakeImap(_msgs()), now=NOW)

    def offline():
        raise OSError("getaddrinfo failed")

    with pytest.raises(SourceNetworkError):
        collect(olx_settings(), cache, imap_factory=offline, now=NOW)


def _adapter(tmp_path, messages, **kw):
    return OlxMailAdapter(None, olx_settings(**kw), imap_factory=lambda: FakeImap(messages),
                          cache_path=tmp_path / "c.json")


def test_no_mail_and_price_range(tmp_path):
    q = SearchQuery(mode=Mode.RESELL, price_min=500, price_max=3000)
    with pytest.raises(SourceNoMail, match="powiadomienia e-mail"):
        asyncio.run(_adapter(tmp_path, {}).search(q))
    offers = asyncio.run(_adapter(tmp_path, _msgs()).search(q))
    assert {o.source_id for o in offers} == {"abc12", "Qw9x"}  # etui (25 zł) i 3200 zł poza zakresem


def test_alert_mail_with_no_offers_is_format_change(tmp_path):
    """Wykrywa, że źródło przestało zwracać oferty: przychodzą powiadomienia, ale parser nic w nich nie widzi."""
    changed = {b"9": mail("Nowe ogłoszenia: iphone", "<div>Nowe ogłoszenia czekają w aplikacji OLX.</div>")}
    with pytest.raises(SourceFormatChanged, match="zmienił wygląd maili"):
        asyncio.run(_adapter(tmp_path, changed).search(SearchQuery(mode=Mode.RESELL)))


def test_registered_disabled_until_configured():
    assert "olx" in REGISTRY and SOURCE_NAMES["olx"] == "OLX (e-mail)"
    s = Settings()
    assert s.enabled_sources["olx"] is False and not OlxMailAdapter.configured(s)
    assert OlxMailAdapter.configured(olx_settings())


def test_password_stored_encrypted_not_in_json(tmp_path):
    conn = open_database(tmp_path / "s.sqlite3")
    repo = SettingsRepository(conn)
    repo.save(olx_settings())
    plain = repo.get_value(repo.KEY)
    assert "dobre" not in plain and "jan@gmail.com" not in plain
    loaded = repo.load()
    assert loaded.olx_mail_password == "dobre" and loaded.olx_mail_user == "jan@gmail.com"


# ------------------------------------------------------------ pełna ścieżka ---

def test_full_pipeline_filters_valuation_picked_and_telegram(tmp_path, monkeypatch):
    """Oferty z maili przechodzą przez filtr akcesoriów, wycenę, ochronę przed oszustwami, „Wybrane” i Telegram;
    awaria innego portalu nie blokuje OLX."""
    from .test_scanner import BrokenAdapter

    conn = open_database(tmp_path / "db.sqlite3")
    s = olx_settings(telegram_enabled=True, telegram_bot_token="t", telegram_chat_id="1",
                     telegram_quiet_enabled=False)
    SettingsRepository(conn).save(s)
    q = TelegramQueue(conn, s)
    q.ensure_since(datetime.now(UTC) - timedelta(days=30))
    limiter = HostRateLimiter(0)
    scanner = Scanner(conn, s, limiter,
                      http_factory=lambda: HttpClient(limiter, transport=httpx.MockTransport(
                          lambda r: httpx.Response(500)), wait=lambda x: 0),
                      adapter_factory=lambda http, st: [
                          BrokenAdapter(), OlxMailAdapter(http, st, imap_factory=lambda: FakeImap(_msgs()),
                                                          cache_path=tmp_path / "c.json")])
    report = asyncio.run(scanner.run())
    broken, olx = report.sources
    assert broken.error and olx.ok and olx.kind == "ok"
    assert olx.found == 4 and olx.skipped >= 1  # etui odrzucone przez filtr akcesoriów
    offers = {o.raw.source_id: o for o in OfferRepository(conn).list()}
    assert "etui1" not in offers and {"abc12", "Qw9x", "zz77"} <= set(offers)
    assert offers["abc12"].parsed.model == "iPhone 13" and offers["abc12"].parsed.storage_gb == 128
    ev = Evaluator(conn, s)
    val = ev.evaluate(offers["abc12"])
    assert val.verdict in set(Verdict) and val.risk is not None  # wycena + ocena ryzyka
    runs = {r["source"]: r["status"] for r in FetchRunRepository(conn).last_runs()}
    assert runs["olx"] == "ok" and runs["broken"] == "error"
    # Telegram i „Wybrane”: ta sama ścieżka co dla innych portali (tu wymuszona wycena „KUPUJ”)
    from .test_sorting import _val

    ids = [o.id for o in offers.values()]
    added = q.enqueue_scan(ids, [], lambda o: _val(Verdict.BUY, 600))
    assert added == len(ids)
    assert all(OfferRepository(conn).get(i).picked_at is not None for i in ids)
    conn.close()


def test_status_bar_shows_olx(tmp_path):
    from PySide6.QtWidgets import QApplication

    from phonebot.ui.main_window import MainWindow

    QApplication.instance() or QApplication([])
    path = tmp_path / "w.sqlite3"
    conn = open_database(path)
    s = Settings()
    s.enabled_sources["olx"] = True
    SettingsRepository(conn).save(s)
    win = MainWindow(conn, path, thumbs_dir=tmp_path)
    assert win.source_status.text_of("olx") == "○ OLX (e-mail)"  # włączony, ale bez danych poczty
    assert "hasło aplikacji" in win.source_status._labels["olx"].toolTip()
    FetchRunRepository(conn).finish(FetchRunRepository(conn).start("olx"), found=0, new=0,
                                    error="brak maili od OLX z ostatnich 7 dni", status="empty")
    win.settings.olx_mail_user, win.settings.olx_mail_password = "jan@gmail.com", "x"
    win.refresh_source_status()
    assert win.source_status.text_of("olx") == "⚠ OLX (e-mail): brak ofert"
    assert "brak maili od OLX" in win.source_status._labels["olx"].toolTip()
    win._quitting = True
    win.close()
    conn.close()
