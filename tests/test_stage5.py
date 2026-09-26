import json
from types import SimpleNamespace

import httpx
import pytest

from phonebot.core.models import Condition, Defect, RedFlag
from phonebot.core.settings import Settings
from phonebot.services.ai_analysis import AiAnalysisError, AiFinding, AiListing, ClaudeAnalyzer, build_prompt, parse_results
from phonebot.services.notifications import NotificationError, TelegramClient, format_telegram, send_telegram_batch
from phonebot.services.post_scan import run_post_scan
from phonebot.services.scanner import ScanReport
from phonebot.storage.repositories import OfferRepository

from .conftest import make_raw
from .sample_data import build_sample_db

# ------------------------------------------------------------------ AI ---


class FakeMessages:
    def __init__(self, payload=None, stop="end_turn", exc=None):
        self.payload, self.stop, self.exc, self.calls = payload, stop, exc, []

    def create(self, **kw):
        self.calls.append(kw)
        if self.exc:
            raise self.exc
        text = json.dumps(self.payload)
        return SimpleNamespace(stop_reason=self.stop, content=[SimpleNamespace(type="text", text=text)])


def fake_client(**kw):
    messages = FakeMessages(**kw)
    return SimpleNamespace(beta=SimpleNamespace(messages=messages)), messages


def test_parse_results_filters_unknown_values():
    text = json.dumps({"results": [
        {"id": "1", "defects": ["screen", "screen", "bogus"], "red_flags": ["icloud_lock", "no_photos"], "note": "x"},
        {"id": "99", "defects": ["battery"], "red_flags": [], "note": ""},
    ]})
    out = parse_results(text, {"1"})
    assert set(out) == {"1"}
    assert out["1"].defects == [Defect.SCREEN]
    assert out["1"].flags == [RedFlag.ICLOUD_LOCK]  # no_photos nie jest flagą tekstową


def test_claude_analyzer_request_shape():
    client, messages = fake_client(payload={"results": [
        {"id": "7", "defects": ["face_id"], "red_flags": [], "note": "Face ID nie działa"}]})
    analyzer = ClaudeAnalyzer("key", "claude-opus-5", client=client)
    out = analyzer.analyze([AiListing("7", "iPhone 13", "face nie działa po wymianie ekranu")])
    assert out["7"].defects == [Defect.FACE_ID]
    call = messages.calls[0]
    assert call["model"] == "claude-opus-5"
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["fallbacks"] == "default" and call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "face nie działa" in call["messages"][0]["content"]


def test_claude_analyzer_refusal_and_truncation():
    client, _ = fake_client(payload={}, stop="refusal")
    with pytest.raises(AiAnalysisError, match="odmówił"):
        ClaudeAnalyzer("k", "m", client=client).analyze([AiListing("1", "t", "d")])
    client, _ = fake_client(payload={}, stop="max_tokens")
    with pytest.raises(AiAnalysisError, match="ucięta"):
        ClaudeAnalyzer("k", "m", client=client).analyze([AiListing("1", "t", "d")])


def test_prompt_truncates_long_descriptions():
    prompt = build_prompt([AiListing("1", "t", "x" * 5000)])
    assert prompt.count("x") <= 1600


def test_ai_findings_merged_and_reset_on_description_change(conn):
    repo = OfferRepository(conn)
    from phonebot.core.normalizer import parse_offer

    raw = make_raw("iPhone 13 128GB", 1000, description="Telefon ma problem z face, reszta ok")
    res = repo.upsert(raw, parse_offer(raw))
    assert repo.get(res.offer_id).parsed.condition is Condition.GOOD
    repo.save_ai(res.offer_id, [Defect.FACE_ID], [RedFlag.UNTESTED], "Face ID prawdopodobnie nie działa")
    offer = repo.get(res.offer_id)
    assert Defect.FACE_ID in offer.parsed.defects and offer.ai_defects == [Defect.FACE_ID]
    assert RedFlag.UNTESTED in offer.parsed.flags
    assert offer.parsed.condition is Condition.DAMAGED
    assert repo.pending_ai([res.offer_id], 10) == []
    raw.description = "Nowy opis: wszystko sprawne, bateria wymieniona w serwisie"
    repo.upsert(raw, parse_offer(raw))
    offer = repo.get(res.offer_id)
    assert offer.ai_note is None and Defect.FACE_ID not in offer.parsed.defects
    assert len(repo.pending_ai([res.offer_id], 10)) == 1


# ------------------------------------------------------------ Telegram ---


def tg_transport(log, ok=True, updates=None):
    def handler(request):
        method = request.url.path.rsplit("/", 1)[-1]
        log.append((method, json.loads(request.content)))
        if method == "getUpdates":
            return httpx.Response(200, json={"ok": True, "result": updates or []})
        return httpx.Response(200, json={"ok": ok, "description": "Unauthorized" if not ok else None})
    return httpx.MockTransport(handler)


def test_telegram_send_and_errors():
    log = []
    TelegramClient("123:ABC", "42", transport=tg_transport(log)).send("<b>hej</b>")
    assert log == [("sendMessage", {"chat_id": "42", "text": "<b>hej</b>", "parse_mode": "HTML",
                                    "disable_web_page_preview": False})]
    with pytest.raises(NotificationError, match="Unauthorized"):
        TelegramClient("bad", "42", transport=tg_transport([], ok=False)).send("x")
    with pytest.raises(NotificationError, match="tokenu"):
        TelegramClient("  ")
    with pytest.raises(NotificationError, match="chat ID"):
        TelegramClient("t", "", transport=tg_transport([])).send("x")


def test_telegram_find_chat_id():
    updates = [{"update_id": 1, "message": {"chat": {"id": 555}, "text": "/start"}}]
    assert TelegramClient("t", transport=tg_transport([], updates=updates)).find_chat_id() == "555"
    with pytest.raises(NotificationError, match="/start"):
        TelegramClient("t", transport=tg_transport([])).find_chat_id()


# ------------------------------------------------------- po skanowaniu ---


def test_first_scan_is_silent(tmp_path):
    conn, report = build_sample_db(tmp_path / "db.sqlite3")
    assert report.first_scan and report.post is None
    post = run_post_scan(conn, Settings(telegram_enabled=True), report, telegram=object())
    assert post.silent_first_scan and post.green == []
    # zielone oferty są oznaczone jako „zgłoszone”, więc nie wrócą przy kolejnym skanie
    notified = conn.execute("SELECT COUNT(*) FROM offers WHERE notified_at IS NOT NULL").fetchone()[0]
    assert notified > 0


def test_new_green_offer_triggers_notifications(tmp_path):
    conn, _ = build_sample_db(tmp_path / "db.sqlite3")
    repo = OfferRepository(conn)
    from phonebot.core.normalizer import parse_offer

    raw = make_raw("iPhone 13 128GB zbity ekran", 600, description="zbity ekran, reszta sprawna",
                   source="allegro_lokalnie", source_id="NEW1", city="Nowy Targ")
    new_id = repo.upsert(raw, parse_offer(raw)).offer_id
    bad = make_raw("iPhone 13 128GB", 3000, source="allegro_lokalnie", source_id="NEW2")
    bad_id = repo.upsert(bad, parse_offer(bad)).offer_id
    log = []
    settings = Settings(telegram_enabled=True, telegram_bot_token="t", telegram_chat_id="1")
    report = ScanReport(new_offer_ids=[new_id, bad_id])
    tg = TelegramClient("t", "1", transport=tg_transport(log))
    post = run_post_scan(conn, settings, report, telegram=tg)
    assert [g.offer_id for g in post.green] == [new_id]
    assert post.telegram_sent == 1 and "iPhone 13 128 GB" in log[0][1]["text"]
    # drugi raz ta sama oferta nie jest zgłaszana
    again = run_post_scan(conn, settings, report, telegram=tg)
    assert again.green == []


def test_telegram_batch_cap():
    from phonebot.core.models import MarketEstimate, Mode
    from phonebot.core.parts import PartsCatalog, default_parts
    from phonebot.core.valuation import evaluate

    from .conftest import make_offer

    items = []
    for i in range(8):
        o = make_offer("iPhone 13 128GB", 1100 + i)  # bez flagi „podejrzanie tanio” (<50% rynku)
        v = evaluate(o, MarketEstimate(2000, 10, "m", "wysoka", 2000), PartsCatalog(default_parts()), Settings(),
                     Mode.RESELL)
        items.append((o, v, "nowa"))
    log = []
    sent = send_telegram_batch(Settings(notify_max_per_scan=3), items,
                               TelegramClient("t", "1", transport=tg_transport(log)))
    assert sent == 4 and "…i jeszcze 5" in log[-1][1]["text"]
    assert "KUPUJ" in format_telegram(*items[0])


def test_ai_errors_do_not_block_notifications(tmp_path):
    conn, _ = build_sample_db(tmp_path / "db.sqlite3")
    ids = [r[0] for r in conn.execute("SELECT id FROM offers")]
    failing = SimpleNamespace(analyze=lambda listings: (_ for _ in ()).throw(AiAnalysisError("brak klucza")))
    post = run_post_scan(conn, Settings(llm_enabled=True), ScanReport(new_offer_ids=ids), analyzer=failing)
    assert post.ai_error == "brak klucza"


def test_ai_analysis_runs_for_new_offers(tmp_path):
    conn, _ = build_sample_db(tmp_path / "db.sqlite3")
    ids = [r[0] for r in conn.execute("SELECT id FROM offers ORDER BY id")]
    seen = []

    def analyze(listings):
        seen.extend(listings)
        return {x.id: AiFinding([Defect.SPEAKER], [], "głośnik") for x in listings}

    post = run_post_scan(conn, Settings(llm_enabled=True, llm_max_per_scan=5),
                         ScanReport(new_offer_ids=ids), analyzer=SimpleNamespace(analyze=analyze))
    assert post.ai_analyzed == 5 == len(seen)
    offer = OfferRepository(conn).get(int(seen[0].id))
    assert Defect.SPEAKER in offer.parsed.defects and offer.ai_note == "głośnik"
