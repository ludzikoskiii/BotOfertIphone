"""Etap 3 — lokalny model językowy (Ollama) czyta opisy ofert „DO WERYFIKACJI”, szablony wiadomości.

Bez internetu i bez Ollamy: serwer Ollamy i strony portali zastępuje ``httpx.MockTransport``.
"""
import importlib.util
import json
import os
from pathlib import Path

import httpx
import pytest

from phonebot.core import messages
from phonebot.core.models import AiLayers, Condition, Defect, MarketEstimate, Mode, RedFlag, Verdict
from phonebot.core.normalizer import parse_offer
from phonebot.core.parts import PartsCatalog, default_parts
from phonebot.core.settings import MlConfig, Settings
from phonebot.core.valuation import evaluate
from phonebot.ml import combine, desc_model
from phonebot.ml.desc_model import DescFindings, apply_to_offer, text_hash, validate
from phonebot.ml.ollama import OllamaClient, OllamaError, OllamaModelMissing, OllamaNotRunning, model_installed
from phonebot.net.http import HostRateLimiter
from phonebot.services.ai_service import AiService, DescJob
from phonebot.sources.pages import PageFetcher, extract_description
from phonebot.storage.repositories import AiRepository, OfferRepository, SettingsRepository

from .conftest import make_offer, make_raw

PARTS = PartsCatalog(default_parts())
MARKET = MarketEstimate(1900, 12, "t", "wysoka", 1900)


# ------------------------------------------------------- usunięty Claude ---

def test_paid_claude_analysis_removed():
    assert importlib.util.find_spec("phonebot.services.ai_analysis") is None
    assert not hasattr(Settings(), "anthropic_api_key")
    root = Path(__file__).resolve().parents[1]
    assert "anthropic" not in (root / "requirements.txt").read_text(encoding="utf-8")
    assert "anthropic" not in (root / "pyproject.toml").read_text(encoding="utf-8")


def test_old_api_key_scrubbed_from_database(conn):
    stored = json.loads(Settings().to_json())
    stored.update(anthropic_api_key="sk-ant-secret", llm_enabled=True, llm_model="claude-opus-5")
    SettingsRepository(conn).set_value("app", json.dumps(stored))
    settings = SettingsRepository(conn).load()
    raw = SettingsRepository(conn).get_value("app")
    assert "sk-ant-secret" not in raw and "anthropic_api_key" not in json.loads(raw)
    assert settings.ml.llm_enabled is False  # nowa, lokalna opcja — domyślnie wyłączona


def test_migration_v7_clears_claude_results(tmp_path):
    import sqlite3

    from phonebot.storage.db import MIGRATIONS, connect, migrate

    path = tmp_path / "v6.sqlite3"
    old = sqlite3.connect(path)
    for script in MIGRATIONS[:6]:
        old.executescript(script)
    old.execute("PRAGMA user_version = 6")
    old.execute("INSERT INTO offers (source, source_id, url, title, price, condition, first_seen, last_seen, "
                "ai_note, ai_checked_at) VALUES ('vinted', '1', 'u', 'iPhone 13', 1000, 'good', "
                "'2026-09-20T00:00:00+00:00', '2026-09-20T00:00:00+00:00', 'uwaga Claude', '2026-09-20')")
    old.commit()
    old.close()
    conn = connect(path)
    assert migrate(conn) == len(MIGRATIONS)
    row = conn.execute("SELECT ai_note, ai_checked_at, page_description FROM offers").fetchone()
    assert (row["ai_note"], row["ai_checked_at"], row["page_description"]) == (None, None, None)
    offer = OfferRepository(conn).get(1)
    assert offer.ai_note is None and offer.layers is None
    conn.close()


# ------------------------------------------------------------ Ollama ---

def ollama(handler):
    return OllamaClient("http://127.0.0.1:11434", transport=httpx.MockTransport(handler))


def chat_reply(content: dict | str, status: int = 200):
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(status, json={"message": {"role": "assistant", "content": text}, "done": True})


def test_ollama_status_and_models():
    def handler(r):
        if r.url.path == "/api/version":
            return httpx.Response(200, json={"version": "0.12.3"})
        return httpx.Response(200, json={"models": [{"name": "qwen3:8b"}, {"name": "llama3.1:latest"}]})

    status = ollama(handler).status()
    assert status.running and status.version == "0.12.3"
    assert status.has_model("qwen3:8b") and status.has_model("LLAMA3.1") and not status.has_model("qwen2.5:7b")
    assert model_installed("qwen3", ["qwen3:latest"])


def test_ollama_not_running():
    def handler(r):
        raise httpx.ConnectError("odmowa połączenia", request=r)

    status = ollama(handler).status()
    assert not status.running and "uruchom program Ollama" in status.error
    with pytest.raises(OllamaNotRunning):
        ollama(handler).chat_json("qwen3:8b", "s", "u", {})


def test_ollama_chat_request_shape_and_fallbacks():
    seen = []

    def handler(r):
        body = json.loads(r.content)
        seen.append(body)
        if "think" in body and len(seen) == 1:
            return httpx.Response(400, json={"error": "\"llama3\" does not support thinking"})
        return chat_reply({"is_phone": True})

    client = ollama(handler)
    assert client.chat_json("llama3", "system", "user", {"type": "object"}) == {"is_phone": True}
    first, retry = seen
    assert first["format"] == {"type": "object"} and first["stream"] is False and first["think"] is False
    assert first["options"]["temperature"] == 0 and first["messages"][0]["role"] == "system"
    assert "think" not in retry
    client.chat_json("llama3", "s", "u", {})
    assert "think" not in seen[-1]  # zapamiętane: ten serwer nie przyjmuje pola „think”

    with pytest.raises(OllamaModelMissing, match="Pobierz model"):
        ollama(lambda r: httpx.Response(404, json={"error": "model not found"})).chat_json("x", "s", "u", {})
    with pytest.raises(OllamaError, match="niepoprawny JSON"):
        ollama(lambda r: chat_reply("to nie jest json")).chat_json("x", "s", "u", {})


def test_ollama_pull_progress_and_errors():
    lines = [{"status": "pulling manifest"}, {"status": "downloading", "completed": 50, "total": 100},
             {"status": "success"}]

    def handler(r):
        assert json.loads(r.content) == {"model": "qwen3:8b", "stream": True}
        return httpx.Response(200, content="\n".join(json.dumps(x) for x in lines).encode())

    progress = []
    ollama(handler).pull("qwen3:8b", lambda s, d, t: progress.append((s, d, t)))
    assert progress == [("pulling manifest", 0, 0), ("downloading", 50, 100), ("success", 0, 0)]
    bad = httpx.Response(200, content=json.dumps({"error": "brak miejsca"}).encode())
    with pytest.raises(OllamaError, match="brak miejsca"):
        ollama(lambda r: bad).pull("qwen3:8b")
    with pytest.raises(OllamaError, match="przerwane"):
        ollama(handler).pull("qwen3:8b", stop=lambda: True)


# --------------------------------------------------- odczyt opisu (AI) ---

@pytest.mark.parametrize("answer, expected", [
    ({"storage_gb": 128}, {"storage_gb": 128}),
    ({"storage_gb": 256}, {"storage_gb": None}),  # 256 nie występuje w tekście — model zgadł
    ({"storage_gb": 1024}, {"storage_gb": None}),  # iPhone 13 nie ma 1 TB
    ({"battery_health": 87}, {"battery_health": 87}),
    ({"battery_health": 95}, {"battery_health": None}),  # nie ma w tekście
    ({"for_parts": True}, {"for_parts": False}),  # tekst nie mówi „na części”
    ({"defects": ["screen", "nieznana"], "red_flags": ["icloud_lock", "no_photos"]},
     {"defects": [Defect.SCREEN], "flags": [RedFlag.ICLOUD_LOCK]}),
])
def test_validate_rejects_values_without_evidence(answer, expected):
    title, desc = "iPhone 13 zbity ekran", "Pamięć 128 GB, bateria 87%, blokada iCloud."
    found = validate(answer, title=title, description=desc, phone_model="iPhone 13")
    for key, value in expected.items():
        assert getattr(found, key) == value, key


def test_validate_for_parts_and_note():
    found = validate({"for_parts": True, "note": "  Telefon   na części.  " * 20}, title="iPhone 12",
                     description="Sprzedam na części, nie włącza się", phone_model="iPhone 12")
    assert found.for_parts and len(found.note) <= 200 and "  " not in found.note


def test_text_hash_follows_description():
    assert text_hash("t", "opis") == text_hash("t", "opis") != text_hash("t", "inny opis")


def desc_offer(title="iPhone 13", description="Stan dobry, pamięć 128 GB, bateria 79%", price=900, **findings):
    offer = make_offer(title, price=price, description=description)
    data = DescFindings(**findings).to_json()
    offer.layers = AiLayers(desc=data, desc_hash=text_hash(title, description), desc_model="qwen3:8b")
    return offer


def test_description_fills_what_rules_missed():
    offer = desc_offer("iPhone 13", "Opis sprzedawcy bez liczb", storage_gb=128, battery_health=None)
    assert offer.parsed.storage_gb is None
    apply_to_offer(offer)
    assert offer.parsed.storage_gb == 128 and offer.ai_filled == ["storage"]
    apply_to_offer(offer)  # drugi raz nic nie zmienia
    assert offer.ai_filled == ["storage"]

    offer = desc_offer(storage_gb=None, battery_health=79, defects=[Defect.FACE_ID], flags=[RedFlag.UNTESTED],
                       note="Face ID nie działa")
    offer.parsed.battery_health = None
    apply_to_offer(offer, battery_threshold=80)
    assert offer.parsed.battery_health == 79 and "battery" in offer.ai_filled
    assert Defect.FACE_ID in offer.parsed.defects and Defect.BATTERY in offer.parsed.defects
    assert RedFlag.UNTESTED in offer.parsed.flags and offer.parsed.condition is Condition.DAMAGED
    assert offer.ai_note == "Face ID nie działa"


def test_stale_or_disabled_description_result_is_ignored():
    offer = desc_offer("iPhone 13", "Opis bez liczb", storage_gb=128)
    offer.raw.description = "Nowy opis od sprzedawcy"  # opis zmieniony po analizie
    apply_to_offer(offer)
    assert offer.parsed.storage_gb is None and offer.layers.desc is None
    offer = desc_offer("iPhone 13", "bez liczb", storage_gb=128)
    apply_to_offer(offer, enabled=False)
    assert offer.parsed.storage_gb is None


def test_description_can_lift_verify_verdict():
    settings = Settings()
    settings.ml.llm_enabled = True
    offer = desc_offer("iPhone 13", "Telefon sprawny, bateria 90%, pamięć sto dwadzieścia osiem", price=1100,
                       storage_gb=None)
    assert evaluate(offer, MARKET, PARTS, settings, Mode.REPAIR).verdict is Verdict.VERIFY  # nieznana pamięć
    offer = desc_offer("iPhone 13", "Telefon sprawny, bateria 90%, 128 GB pamięci", price=1100, storage_gb=128)
    offer.parsed = parse_offer(make_raw("iPhone 13", 1100, description=""))  # reguły nie widziały opisu
    apply_to_offer(offer)
    val = evaluate(offer, MARKET, PARTS, settings, Mode.REPAIR)
    assert val.verdict is Verdict.BUY and RedFlag.STORAGE_UNKNOWN not in val.flags


def test_description_saying_not_a_phone_keeps_verify():
    settings = Settings()
    settings.ml.llm_enabled = True
    offer = desc_offer("iPhone 13 128GB", "Sprzedam etui do iPhone 13", price=1100, is_phone=False,
                       note="Sprzedawane jest samo etui")
    apply_to_offer(offer)
    val = evaluate(offer, MARKET, PARTS, settings, Mode.REPAIR)
    assert val.verdict is Verdict.VERIFY and RedFlag.AI_DESC_CONFLICT in val.flags
    layer = combine.combine(offer.layers, settings.ml).layers[2]
    assert layer.state == combine.CONFLICT and "etui" in layer.summary


def test_description_layer_in_combine():
    cfg = MlConfig(llm_enabled=True)
    unsure = {"phone": 0.5, "accessory": 0.3, "part": 0.1, "wanted": 0.1}
    ai = AiLayers(text_label="phone", text_probs=unsure)
    assert combine.combine(ai, cfg).flags == [RedFlag.AI_LOW_CONFIDENCE]
    ai.desc = DescFindings(storage_gb=128, battery_health=88).to_json()
    result = combine.combine(ai, cfg)  # opis potwierdza telefon → bez „niskiej pewności”
    assert (result.state, result.flags) == ("zgodne", [])
    assert result.layers[2].summary.startswith("telefon; pamięć 128 GB, bateria 88%")
    assert combine.combine(ai, MlConfig()).layers[2].summary == "analiza opisów wyłączona"
    ai.desc, ai.desc_error = None, "Ollama: HTTP 500"
    assert "nie udało się" in combine.combine(ai, cfg).layers[2].summary


# ---------------------------------------------------- strony ofert ---

SPRZEDAJEMY_PAGE = """<html><head><meta property="og:description" content="400 zł: Sprzedam iPhone 13 mini">
<script type="application/ld+json">{"@type": "Product", "name": "iPhone 13 mini",
"description": "Sprzedam iPhone 13 mini 128 Rok produkcji 2021. Bateria 87%. Etui gratis."}</script></head></html>"""
ALLEGRO_PAGE = """<html><head><meta name="description" content="Kup teraz: iPhone 13 za 850 zł i odbierz w mieście">
</head><body><div><p class="desc-p">Dzień dobry, mam na sprzedaż telefon iPhone 13.</p>
<p class="desc-p">Bateria 89%, &quot;ekran&quot; bez rys.</p></div></body></html>"""
VINTED_PAGE = """<html><head><meta property="og:description" content="iPhone 13 Pro - Nothing is wrong with the phone.">
<script type="application/ld+json">{"@type": "Product", "description": "Nothing is wrong with the phone. Unlocked."}
</script></head><body>""" + "x" * 1000


def test_description_from_offer_pages():
    assert extract_description("sprzedajemy", SPRZEDAJEMY_PAGE).startswith("Sprzedam iPhone 13 mini 128")
    assert extract_description("allegro_lokalnie", ALLEGRO_PAGE) == \
        'Dzień dobry, mam na sprzedaż telefon iPhone 13.\nBateria 89%, "ekran" bez rys.'
    assert extract_description("vinted", VINTED_PAGE) == "Nothing is wrong with the phone. Unlocked."
    # bez JSON-LD: og:description bez ceny (Sprzedajemy) i bez tytułu (Vinted)
    no_ld = SPRZEDAJEMY_PAGE.split("<script")[0] + "</head></html>"
    assert extract_description("sprzedajemy", no_ld) == "Sprzedam iPhone 13 mini"
    vinted_og = VINTED_PAGE.split("<script")[0]
    assert extract_description("vinted", vinted_og, title="iPhone 13 Pro") == "Nothing is wrong with the phone."
    # Allegro: ogólnikowy meta opis portalu nie jest opisem sprzedającego
    assert extract_description("allegro_lokalnie", ALLEGRO_PAGE.replace("desc-p", "x")) == ""


def test_page_fetcher_limits_and_respects_blocks():
    calls, slept = [], []

    def handler(r):
        calls.append(str(r.url))
        if "blokada" in r.url.path:
            return httpx.Response(403, text="captcha-delivery.com")
        if "brak" in r.url.path:
            return httpx.Response(404)
        return httpx.Response(200, text=SPRZEDAJEMY_PAGE, headers={"content-type": "text/html; charset=utf-8"})

    clock = [0.0]
    limiter = HostRateLimiter(4.0, jitter=0, clock=lambda: clock[0])
    fetcher = PageFetcher(limiter, client=httpx.Client(transport=httpx.MockTransport(handler)),
                          sleep=lambda s: (slept.append(s), clock.__setitem__(0, clock[0] + s)))
    ok = fetcher.fetch("sprzedajemy", "https://sprzedajemy.pl/iphone-nr1")
    assert ok.description.startswith("Sprzedam iPhone 13 mini") and ok.error is None
    assert fetcher.fetch("sprzedajemy", "https://sprzedajemy.pl/brak-nr2").error == "ogłoszenie już nie istnieje"
    assert sum(slept) == pytest.approx(4.0)  # drugie zapytanie do tego samego portalu po 4 s
    blocked = fetcher.fetch("sprzedajemy", "https://sprzedajemy.pl/blokada-nr3")
    assert blocked.blocked and "zablokował" in blocked.error
    before = len(calls)
    again = fetcher.fetch("sprzedajemy", "https://sprzedajemy.pl/iphone-nr4")
    assert again.blocked and len(calls) == before  # po blokadzie żadnych kolejnych zapytań do tego portalu
    assert fetcher.fetch("olx", "https://olx.pl/x").error == "portal nie jest obsługiwany"


# ------------------------------------------- analiza opisów w usłudze ---

class FakeOllama:
    def __init__(self, answer=None, exc=None):
        self.answer, self.exc, self.calls = answer or {"is_phone": True}, exc, []

    def chat_json(self, model, system, user, schema):
        self.calls.append(user)
        if self.exc:
            raise self.exc
        return dict(self.answer)


class FakeFetcher:
    def __init__(self, description="", blocked=False):
        self.description, self.blocked, self.calls = description, blocked, []

    def fetch(self, source, url, title=""):
        from phonebot.sources.pages import PageResult

        self.calls.append(url)
        if self.blocked:
            return PageResult(error="portal zablokował pobieranie (HTTP 403)", blocked=True)
        return PageResult(description=self.description, error=None if self.description else "brak opisu")


def stored(conn, title="iPhone 13", description="", source="vinted"):
    raw = make_raw(title, 1000, description=description, source=source)
    offer_id = OfferRepository(conn).upsert(raw, parse_offer(raw)).offer_id
    return DescJob(raw.source, raw.source_id, offer_id, raw.title, raw.description, raw.url, "iPhone 13")


def test_analyze_descriptions_fetches_page_and_saves_result(conn, tmp_path):
    settings = Settings()
    settings.ml.llm_enabled = True
    job = stored(conn)
    llm = FakeOllama({"is_phone": True, "storage_gb": 128, "battery_health": 86, "for_parts": False,
                      "defects": [], "red_flags": [], "note": "Telefon sprawny."})
    fetcher = FakeFetcher("Sprzedam iPhone 13, pamięć 128 GB, bateria 86%, sprawny.")
    svc = AiService(conn, settings, tmp_path)
    assert svc.analyze_descriptions([job], client=llm, fetcher=fetcher) == 1
    assert fetcher.calls == [job.url] and "pamięć 128 GB" in llm.calls[0]
    offer = OfferRepository(conn).get(job.offer_id)
    # opis ze strony zostaje przy ofercie, a reguły od razu go czytają (pamięć i bateria z opisu)
    assert offer.desc_from_page and offer.raw.description.startswith("Sprzedam iPhone 13")
    assert offer.parsed.storage_gb == 128 and offer.parsed.battery_health == 86
    assert offer.layers.desc_hash == text_hash(offer.raw.title, offer.raw.description)
    assert offer.layers.desc["note"] == "Telefon sprawny." and offer.layers.desc_model == "qwen3:8b"


def test_analyze_descriptions_errors(conn, tmp_path):
    settings = Settings()
    svc = AiService(conn, settings, tmp_path)
    job = stored(conn, description="Opis sprzedawcy: telefon działa")
    assert svc.analyze_descriptions([job], client=FakeOllama(exc=OllamaError("model zwrócił niepoprawny JSON")),
                                    fetcher=FakeFetcher()) == 1
    assert OfferRepository(conn).get(job.offer_id).layers.desc_error == "model zwrócił niepoprawny JSON"
    # Ollama wyłączona / brak modelu → przerwanie partii bez zapisu (oferty wrócą później)
    job2 = stored(conn, description="inny opis")
    with pytest.raises(OllamaNotRunning):
        svc.analyze_descriptions([job2], client=FakeOllama(exc=OllamaNotRunning("nie działa")), fetcher=FakeFetcher())
    assert OfferRepository(conn).get(job2.offer_id).layers is None
    # portal blokuje strony ofert → nic nie jest zapisywane, bez zapytania do modelu
    job3, llm = stored(conn), FakeOllama()
    assert svc.analyze_descriptions([job3], client=llm, fetcher=FakeFetcher(blocked=True)) == 0
    assert llm.calls == [] and OfferRepository(conn).get(job3.offer_id).layers is None
    # brak opisu nawet na stronie → zapis „brak opisu” (bez ponownego czytania tej samej treści)
    job4 = stored(conn)
    assert svc.analyze_descriptions([job4], client=FakeOllama(), fetcher=FakeFetcher("")) == 1
    assert OfferRepository(conn).get(job4.offer_id).layers.desc_error == "brak opisu w ogłoszeniu"


def test_page_description_survives_next_scan(tmp_path):
    from .sample_data import build_sample_db, mock_portals  # noqa: F401

    conn, _ = build_sample_db(tmp_path / "t.sqlite3")
    vinted = next(o for o in OfferRepository(conn).list() if o.raw.source == "vinted")
    assert vinted.raw.description == ""
    raw = make_raw(vinted.raw.title, vinted.price, description="Bateria 91%, pamięć 128 GB")
    OfferRepository(conn).save_page_description(vinted.id, raw.description, parse_offer(raw))
    import asyncio

    from phonebot.net.http import HostRateLimiter as Limiter
    from phonebot.net.http import HttpClient
    from phonebot.services.scanner import Scanner

    from .sample_data import mock_portals as handler

    limiter = Limiter(0)
    scanner = Scanner(conn, SettingsRepository(conn).load(), limiter,
                      http_factory=lambda: HttpClient(limiter, transport=httpx.MockTransport(handler), wait=lambda s: 0))
    asyncio.run(scanner.run())
    again = OfferRepository(conn).get(vinted.id)
    assert again.raw.description == "Bateria 91%, pamięć 128 GB" and again.parsed.battery_health == 91
    conn.close()


# --------------------------------------------------------- wiadomości ---

def valued(title, price, description="", market=MARKET):
    offer = make_offer(title, price=price, description=description)
    return offer, evaluate(offer, market, PARTS, Settings(), Mode.REPAIR)


def test_message_negotiation_has_arguments_and_opening_price():
    offer, val = valued("iPhone 13 128GB zbity ekran", 1200, "Zbity ekran, bateria 82%, reszta działa.")
    text = messages.render(messages.DEFAULT_TEMPLATES["negotiate"], offer, val)
    assert "iPhone 13 128 GB" in text and "Wyświetlacz / szyba do naprawy — to koszt ok." in text
    assert "Kondycja baterii 82%" in text
    assert f"Czy cena {messages.zl(messages.opening_price(offer, val))}" in text
    assert messages.opening_price(offer, val) % 10 == 0 and messages.opening_price(offer, val) <= offer.price
    assert "{" not in text and "paczkomat" in text


def test_message_questions_match_offer():
    offer, val = valued("iPhone 13", 120)  # „iPhone 13 ?” za 120 zł — DO WERYFIKACJI
    assert val.verdict is Verdict.VERIFY and messages.template_for(val.verdict) == "verify"
    text = messages.render(messages.DEFAULT_TEMPLATES["verify"], offer, val)
    assert "- Jaka jest pojemność pamięci?" in text and "blokady iCloud" in text
    assert "Czy to oryginalny iPhone?" in text
    offer.parsed.flags.append(RedFlag.AI_PHOTO_CONFLICT)
    val.flags.append(RedFlag.AI_PHOTO_CONFLICT)
    assert "cały telefon" in messages.render("{pytania}", offer, val)


def test_message_market_argument_only_when_it_helps():
    cheap_market = MarketEstimate(1000, 12, "t", "wysoka", 1000)
    offer, val = valued("iPhone 13 128GB", 1300, market=cheap_market)
    assert "Podobne egzemplarze sprzedają się za ok. 1 000 zł." in messages.arguments(offer, val)
    offer, val = valued("iPhone 13 128GB", 1100)
    assert not [a for a in messages.arguments(offer, val) if a.startswith("Podobne")]
    assert messages.render("{nieznane} {telefon}", offer, val) == "{nieznane} iPhone 13 128 GB"


def test_default_templates_in_settings_roundtrip():
    s = Settings()
    s.message_templates["buy"] = "Biorę {telefon}!"
    loaded = Settings.from_json(s.to_json())
    assert loaded.message_templates["buy"] == "Biorę {telefon}!"
    assert set(loaded.message_templates) == set(messages.TEMPLATE_KEYS)


# -------------------------------------------------------------- okno ---

QtWidgets = pytest.importorskip("PySide6.QtWidgets")
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture
def window(tmp_path):
    from phonebot.ui.main_window import MainWindow

    from .sample_data import build_sample_db

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
    conn, _ = build_sample_db(tmp_path / "t.sqlite3")
    win = MainWindow(conn, tmp_path / "t.sqlite3", thumbs_dir=tmp_path)
    yield win
    win._quitting = True
    win.close()
    conn.close()
    app.processEvents()


def test_only_verify_offers_go_to_ollama_once(window):
    raw = make_raw("iPhone 13", 1100, source="vinted")  # nieznana pamięć → najwyżej DO WERYFIKACJI
    OfferRepository(window.conn).upsert(raw, parse_offer(raw))
    window.reload()
    window.settings.ml.llm_enabled = True
    window.ai_worker = object()  # atrapa wątku AI
    sent = []
    window.ai_desc_requested.connect(sent.append)
    window._queue_desc_analysis()
    window._queue_desc_analysis()
    verify = {o.id for o, v in window.model.rows() if v.verdict is Verdict.VERIFY}
    assert verify and len(sent) == 1 and {j.offer_id for j in sent[0]} == verify
    # przeczytany opis (ten sam skrót treści) nie wraca do kolejki
    job = sent[0][0]
    AiRepository(window.conn).save_desc(job.source, job.source_id, text_hash(job.title, job.description),
                                        DescFindings().to_json(), "qwen3:8b")
    window._desc_queued.clear()
    window.reload()
    assert job.offer_id not in {j.offer_id for batch in sent[1:] for j in batch}
    window.ai_worker = None


def test_copy_message_button(window):
    offer_id = window.model.row_at(0)[0].id
    window._select_offer(offer_id)
    view = window.details
    assert [a.text() for a in view.copy_btn.menu().actions()] == list(messages.TEMPLATE_NAMES.values())
    shown = []
    view.message_copied.connect(shown.append)
    text = view.copy_message()
    assert text and QtWidgets.QApplication.clipboard().text() == text
    assert view.offer.parsed.model in text and "Skopiowano wiadomość" in shown[0]
    view.copy_btn.menu().actions()[2].trigger()  # pytania przed zakupem
    assert "Zanim kupię" in QtWidgets.QApplication.clipboard().text()
    window.settings.message_templates["verify"] = ""  # pusty szablon → domyślny
    assert "Zanim kupię" in view.copy_message("verify")


def test_settings_ollama_and_messages(window):
    from PySide6.QtWidgets import QCheckBox, QPlainTextEdit

    dialog = window.open_settings()
    assert dialog.llm_model.currentText() == "qwen3:8b" and dialog.llm_url.text() == "http://127.0.0.1:11434"
    dialog.findChild(QCheckBox, "ml.llm_enabled").setChecked(True)
    dialog.llm_model.setCurrentText("qwen2.5:7b")
    dialog.findChild(QPlainTextEdit, "template_buy").setPlainText("Biorę {telefon} za {cena}.")
    result = dialog.result_settings()
    assert result.ml.llm_enabled and result.ml.llm_model == "qwen2.5:7b"
    assert result.message_templates["buy"] == "Biorę {telefon} za {cena}."
    dialog.reject()


def test_details_show_description_layer_and_ai_marks():
    from phonebot.ui.details_html import build_details_html

    settings = Settings()
    settings.ml.llm_enabled = True
    offer = desc_offer("iPhone 13", "Opis bez liczb", price=1100, storage_gb=128, note="Wszystko sprawne.")
    offer.parsed.storage_gb = None
    apply_to_offer(offer)
    offer.desc_from_page = True
    html = build_details_html(offer, evaluate(offer, MARKET, PARTS, settings, Mode.REPAIR), settings)
    assert "Opis (Ollama)" in html and "128 GB (AI z opisu)" in html
    assert "Opis wg AI (Ollama)" in html and "Wszystko sprawne." in html
    assert "(pobrany ze strony oferty)" in html


def test_desc_model_prompt_is_polish_and_bounded():
    prompt = desc_model.build_user_prompt("iPhone 13", "x" * 10_000, "iPhone 13")
    assert prompt.startswith("Model rozpoznany z tytułu: iPhone 13") and prompt.count("x") == 2500
    assert "Niczego nie zgadujesz" in desc_model.SYSTEM_PROMPT
    assert set(desc_model.SCHEMA["required"]) == {"is_phone", "storage_gb", "battery_health", "for_parts",
                                                  "defects", "red_flags", "note"}


def test_ai_worker_description_queue(tmp_path, monkeypatch):
    from phonebot.ml.ollama import OllamaStatus
    from phonebot.storage.db import open_database
    from phonebot.ui import ai_worker as worker_mod

    db = tmp_path / "w.sqlite3"
    conn = open_database(db)
    job = stored(conn, description="Sprzedam iPhone 13, sprawny")
    conn.close()
    settings = Settings()
    settings.ml.llm_enabled = True
    worker = worker_mod.AiWorker(db, settings, tmp_path / "models")
    answers = iter([OllamaStatus(False, error="odmowa połączenia"), OllamaStatus(True, "0.12.3", ["qwen3:8b"])])

    class FakeClient:
        base_url = "http://127.0.0.1:11434"
        checks = 0

        def status(self):
            FakeClient.checks += 1
            return next(answers)

        def close(self):
            pass

    monkeypatch.setattr(worker, "_ollama", lambda: FakeClient())
    states, done = [], []
    worker.llm_state.connect(lambda ok, msg: states.append((ok, msg)))
    worker.desc_done.connect(done.append)
    worker.analyze_desc([job])
    assert states == [(False, "Ollama nie działa — uruchom program Ollama (odmowa połączenia)")]
    worker.analyze_desc([job])  # w ciągu minuty bez ponownego sprawdzania
    assert FakeClient.checks == 1 and not worker._desc_pending
    worker.reset_llm_check()  # np. zmiana ustawień Ollamy
    worker.analyze_desc([job])
    assert states[-1][0] is True and len(worker._desc_pending) == 1

    calls = []
    monkeypatch.setattr(worker_mod.AiService, "analyze_descriptions",
                        lambda self, jobs, **kw: calls.append(jobs) or len(jobs))
    assert worker.process_desc() == 1 and done == [1] and calls == [[job]]

    def not_running(self, jobs, **kw):
        raise OllamaNotRunning("Ollama nie działa pod adresem …")

    monkeypatch.setattr(worker_mod.AiService, "analyze_descriptions", not_running)
    worker._desc_pending.extend([job, job])
    assert worker.process_desc() == 0 and not worker._desc_pending and states[-1][0] is False
    worker.close()


def test_polish_plural_in_status_messages():
    from phonebot.core.text import plural

    assert [plural(n, "opis", "opisy", "opisów") for n in (1, 2, 5, 12, 22, 104)] == \
        ["1 opis", "2 opisy", "5 opisów", "12 opisów", "22 opisy", "104 opisy"]
