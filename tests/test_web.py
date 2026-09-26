"""Zadanie 5: wersja na telefon — serwer, PIN, sesje, CSRF, lista, szczegóły, akcje, bezpieczeństwo."""
from __future__ import annotations

import re

import httpx
import pytest

from phonebot.core.models import OfferStatus, Verdict
from phonebot.core.settings import Settings
from phonebot.storage.repositories import OfferRepository, RejectedRepository
from phonebot.web.auth import LoginThrottle, hash_pin, verify_pin
from phonebot.web.server import WebError, WebServer, allowed_bind

from .sample_data import build_sample_db


@pytest.fixture
def server(tmp_path):
    conn, _ = build_sample_db(tmp_path / "w.sqlite3")
    settings = Settings(web_enabled=True, web_port=0, web_pin_hash=hash_pin("2468"))
    srv = WebServer(tmp_path / "w.sqlite3", settings)
    srv.start(settings)
    yield srv, conn
    srv.stop()
    conn.close()


def _client(srv):
    return httpx.Client(base_url=srv.local_url(), follow_redirects=True, trust_env=False)


def _login(c, pin="2468"):
    return c.post("/login", data={"pin": pin, "next": "/"})


def test_pin_hash_and_bind_rules():
    stored = hash_pin("1234")
    assert verify_pin("1234", stored) and not verify_pin("1235", stored) and "1234" not in stored
    assert allowed_bind("127.0.0.1") and allowed_bind("100.101.102.103")
    for bad in ("0.0.0.0", "192.168.1.10", "10.0.0.5", "8.8.8.8", "::"):
        assert not allowed_bind(bad), bad


def test_no_pin_no_server(tmp_path):
    with pytest.raises(WebError, match="PIN"):
        WebServer(tmp_path / "x.sqlite3", Settings()).start(Settings(web_enabled=True))


def test_login_required_and_listing(server):
    srv, conn = server
    assert srv.host == "127.0.0.1"
    with _client(srv) as c:
        r = c.get("/")
        assert r.url.path == "/login" and "PIN" in r.text
        assert _login(c, "0000").status_code == 401
        r = _login(c)
        assert r.status_code == 200 and "Wszystkie (" in r.text and "Wybrane (" in r.text
        assert 'class="badge buy"' in r.text  # kolorowe werdykty
        picked = c.get("/?lista=picked")
        assert picked.text.count('class="card') < r.text.count('class="card')
        cheap = c.get("/?sort=price&kier=asc")
        prices = [int(p.replace("\xa0", "").replace(" ", "")) for p in
                  re.findall(r'class="price">([\d  ]+) zł', cheap.text)]
        assert prices == sorted(prices) and prices
        only13 = c.get("/", params={"model": "iPhone 13", "cena_max": "1000"})
        models = re.findall(r'class="model">(?:[★⌛] )*([^<]+)</span>', only13.text)
        assert models and all(m.startswith("iPhone 13 ") for m in models)
        found = re.findall(r'class="price">([\d  ]+) zł', only13.text)
        assert all(int(p.replace("\xa0", "").replace(" ", "")) <= 1000 for p in found)


def test_details_message_and_actions(server):
    srv, conn = server
    offer = next(o for o in OfferRepository(conn).list() if o.parsed.model)
    with _client(srv) as c:
        _login(c)
        page = c.get(f"/oferta/{offer.id}")
        assert "Wiadomość do sprzedającego" in page.text and 'id="msg"' in page.text and "Kopiuj" in page.text
        assert "Otwórz ogłoszenie" in page.text
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
        # bez tokenu CSRF — odmowa
        assert c.post(f"/oferta/{offer.id}/akcja", data={"a": "watch"}).status_code == 403
        c.post(f"/oferta/{offer.id}/akcja", data={"a": "watch", "csrf": csrf})
        assert OfferRepository(conn).get(offer.id).status is OfferStatus.WATCHED
        assert srv.app.changes == 1
        c.post(f"/oferta/{offer.id}/akcja", data={"a": "hide", "csrf": csrf})
        assert OfferRepository(conn).get(offer.id).status is OfferStatus.HIDDEN
        r = c.post(f"/oferta/{offer.id}/akcja", data={"a": "not_phone", "label": "accessory", "csrf": csrf})
        assert "Odrzucone" in r.text and OfferRepository(conn).get(offer.id) is None
        assert any(x.source_id == offer.raw.source_id for x in RejectedRepository(conn).list())


def test_negotiation_message_on_phone(server):
    from phonebot.core.normalizer import parse_offer

    from .conftest import make_raw

    srv, conn = server
    neg = None
    for price in range(900, 1700, 20):  # cena tuż nad „max ceną zakupu” → NEGOCJUJ
        raw = make_raw("iPhone 13 128GB niebieski", price, "Bateria 83%, drobne rysy na obudowie, bez pudełka.",
                       source="allegro_lokalnie", source_id="neg1")
        OfferRepository(conn).upsert(raw, parse_offer(raw))
        srv.app.invalidate()
        neg = next((o, v) for o, v in srv.app.rows() if o.raw.source_id == "neg1")
        if neg[1].verdict is Verdict.NEGOTIATE:
            break
    assert neg[1].verdict is Verdict.NEGOTIATE
    with _client(srv) as c:
        _login(c)
        text = c.get(f"/oferta/{neg[0].id}").text
        assert "Proponowana cena" in text and "Kondycja baterii 83%" in text and "Negocjacja — uprzejmy" in text
        assert "Proponuję" in c.get(f"/oferta/{neg[0].id}?styl=concrete").text


def test_security_headers_host_check_and_pwa(server):
    srv, _ = server
    with _client(srv) as c:
        r = c.get("/login")
        assert r.headers["x-frame-options"] == "DENY" and "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert c.get("/login", headers={"Host": "evil.example.com"}).status_code == 400  # DNS rebinding
        assert c.get("/login", headers={"Host": "moj-pc.tail1234.ts.net"}).status_code == 200
        m = c.get("/manifest.webmanifest").json()
        assert m["display"] == "standalone" and m["start_url"] == "/"
        assert c.get("/icon-192.png").content[:4] == b"\x89PNG"
        assert "fetch" in c.get("/sw.js").text
        _login(c)
        cookie = next(h for h in c.cookies.jar)
        assert cookie.has_nonstandard_attr("HttpOnly")


def test_session_survives_restart_and_logout(server, tmp_path):
    srv, _ = server
    with _client(srv) as c:
        _login(c)
        token = c.cookies.get("pb_session")
        srv.stop()
        srv.start(srv.app.settings)
    with httpx.Client(base_url=srv.local_url(), cookies={"pb_session": token}, trust_env=False) as c2:
        page = c2.get("/")
        assert page.status_code == 200
        csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page.text).group(1)
        c2.post("/logout", data={"csrf": csrf})
        assert c2.get("/").status_code == 303


def test_throttle_locks_after_failures():
    now = [0.0]
    t = LoginThrottle(clock=lambda: now[0])
    for _ in range(5):
        t.failure()
    assert t.locked_for() == 300
    now[0] = 301
    assert t.locked_for() == 0


def test_window_switch_pin_and_phone_changes(tmp_path):
    import copy
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    from phonebot.ui.main_window import MainWindow

    conn, _ = build_sample_db(tmp_path / "u.sqlite3")
    win = MainWindow(conn, tmp_path / "u.sqlite3", thumbs_dir=tmp_path)
    try:
        assert win.web is None and win.web_label.isHidden()
        dialog = win.open_settings()
        dialog.findChild(widgets.QCheckBox, "web_enabled").setChecked(True)
        dialog.web_pin.setText("12")
        dialog.web_pin2.setText("12")
        assert dialog._pin_error() and dialog.result_settings().web_pin_hash == ""  # za krótki PIN
        dialog.web_pin.setText("135790")
        dialog.web_pin2.setText("135790")
        result = dialog.result_settings()
        dialog.reject()
        assert verify_pin("135790", result.web_pin_hash)
        result.web_port = 0  # wolny port w teście
        win.apply_settings(copy.deepcopy(result))
        assert win.web.running and win.web.host == "127.0.0.1" and win.web_label.text() == "📱 Telefon: działa"
        offer_id = win.model.row_at(0)[0].id
        with httpx.Client(base_url=win.web.local_url(), follow_redirects=True, trust_env=False) as c:
            _login(c, "135790")
            page = c.get(f"/oferta/{offer_id}").text
            csrf = re.search(r'name="csrf" value="([0-9a-f]+)"', page).group(1)
            c.post(f"/oferta/{offer_id}/akcja", data={"a": "watch", "csrf": csrf})
        win._web_poll()  # zmiana z telefonu → tabela w oknie odświeżona
        row = win.model.row_of(offer_id)
        assert win.model.row_at(row)[0].status is OfferStatus.WATCHED
        # PIN zapisany zaszyfrowanym polem, nie w JSON-ie ustawień
        from phonebot.storage.repositories import SettingsRepository

        assert "pbkdf2" not in SettingsRepository(conn).get_value("app")
        off = copy.deepcopy(result)
        off.web_enabled = False
        win.apply_settings(off)
        assert not win.web.running and win.web_label.isHidden()
    finally:
        win._quitting = True
        win.close()
        conn.close()
        app.processEvents()
