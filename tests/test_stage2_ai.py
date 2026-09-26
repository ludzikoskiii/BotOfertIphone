"""Etap 2 — lokalne AI: klasyfikator tytułów (scikit-learn), analiza zdjęć (CLIP w onnxruntime), łączenie warstw.

Bez internetu: model zdjęć zastępuje maleńki model ONNX o tym samym wejściu/wyjściu albo atrapa sesji,
a pobieranie — ``httpx.MockTransport``.
"""
import hashlib
import io
import os

import httpx
import numpy as np
import pytest

from phonebot.core.models import AiLayers, MarketEstimate, Mode, OfferStatus, RedFlag, Verdict
from phonebot.core.normalizer import parse_offer
from phonebot.core.parts import PartsCatalog, default_parts
from phonebot.core.settings import MlConfig, Settings
from phonebot.core.valuation import evaluate
from phonebot.ml import combine, photo_model, seed_data, text_model
from phonebot.ml.photo_model import CLASSES, PhotoClassifier, PhotoModelError, download_model, preprocess
from phonebot.ml.selftest import sample_jpeg, tiny_vision_model
from phonebot.ml.text_model import Example, ModelInfo, TextClassifier, dedupe, seed_training_set
from phonebot.services import ai_service
from phonebot.services.ai_service import AiService, PhotoJob, build_training_set, labels_count
from phonebot.storage.repositories import AiRepository, LabelRepository, OfferRepository, RejectedRepository

from .conftest import make_offer, make_raw

PARTS = PartsCatalog(default_parts())


@pytest.fixture(scope="module")
def trained() -> TextClassifier:
    return TextClassifier.train(seed_training_set())


@pytest.fixture(autouse=True)
def _fresh_models():
    """Model tytułów i zdjęć to globalny stan programu — każdy test zaczyna bez wczytanych modeli."""
    text_model.set_classifier(None)
    photo_model.set_photo_classifier(None)
    yield
    text_model.set_classifier(None)
    photo_model.set_photo_classifier(None)


class FakeClassifier:
    """Atrapa klasyfikatora tytułów: stałe prawdopodobieństwa, dowolne ``info``."""

    def __init__(self, probs=None, **info):
        self.probs = probs or {"phone": 0.9, "accessory": 0.05, "part": 0.03, "wanted": 0.02}
        self.info = ModelInfo(trained_at="2026-09-26T10:00:00+00:00", **info)

    def predict(self, titles):
        return [dict(self.probs) for _ in titles]


def store(conn, title, **kw):
    raw = make_raw(title, **kw)
    offer_id = OfferRepository(conn).upsert(raw, parse_offer(raw)).offer_id
    return raw, offer_id


# ------------------------------------------------------ zbiór startowy ---

def test_seed_set_covers_classes_and_neighbour_languages():
    examples = seed_data.seed_examples()
    labels = [label for _, label in examples]
    assert len(examples) >= 600
    assert {label: labels.count(label) >= 60 for label in seed_data.LABELS} == dict.fromkeys(seed_data.LABELS, True)
    text = " ".join(t.lower() for t, _ in examples)
    for word in ("obal", "kryt", "pouzdro", "hülle", "dėklas", "case", "cover", "szkło", "wyświetlacz", "kupię"):
        assert word in text, word
    # zestaw kontrolny (prawdziwe tytuły) nie może trafić do treningu
    bench = {t for t, _ in seed_data.BENCHMARK}
    assert not bench & {t for t, _ in examples}


# ------------------------------------------------- klasyfikator tytułów ---

def test_text_classifier_accuracy(trained):
    info = trained.info
    assert info.accuracy >= 0.9 and info.n_test > 100
    assert info.benchmark_accuracy >= 0.9 and info.benchmark_n == len(seed_data.BENCHMARK)
    assert set(info.per_class) == set(seed_data.LABELS)
    assert info.sources == {"seed": info.n_train}


@pytest.mark.parametrize("title, label", [
    ("Etui silikonowe do iPhone 13 Pro", "accessory"),
    ("13 14 15 terakota skal obal", "accessory"),  # zrzut ekranu z Vinted
    ("Szkło hartowane iPhone 14 Pro Max 9H", "accessory"),
    ("Schutzhülle für iPhone 12", "accessory"),
    ("iPhone 13 128GB czarny, bateria 89%", "phone"),
    ("Apple iPhone 12 mini 64GB", "phone"),
    ("iPhone 11", "phone"),
    ("Wyświetlacz LCD iPhone 12 oryginał", "part"),
    ("Płyta główna iPhone X 64GB", "part"),
    ("Kupię iPhone 13 uszkodzony", "wanted"),
])
def test_text_classifier_examples(trained, title, label):
    probs = trained.predict_one(title)
    assert text_model.top(probs)[0] == label, probs
    assert abs(sum(probs.values()) - 1) < 1e-6


def test_text_classifier_save_load(trained, tmp_path):
    trained.save(tmp_path)
    loaded = TextClassifier.load(tmp_path)
    assert loaded is not None and loaded.info.accuracy == trained.info.accuracy
    titles = ["Etui iPhone 13", "iPhone 13 128GB"]
    assert loaded.predict(titles) == trained.predict(titles)
    # inna wersja modelu albo uszkodzony plik → trening od nowa zamiast błędu
    info = (tmp_path / text_model.INFO_FILE).read_text(encoding="utf-8")
    (tmp_path / text_model.INFO_FILE).write_text(info.replace('"version": 1', '"version": 999'), encoding="utf-8")
    assert TextClassifier.load(tmp_path) is None
    (tmp_path / text_model.INFO_FILE).write_text(info, encoding="utf-8")
    (tmp_path / text_model.MODEL_FILE).write_bytes(b"to nie jest model")
    assert TextClassifier.load(tmp_path) is None


def test_dedupe_prefers_your_label():
    data = dedupe([Example("iPhone 13 obudowa", "part", "seed"), Example("IPHONE 13 OBUDOWA", "phone", "user"),
                   Example("iPhone 13 obudowa", "accessory", "hidden")])
    assert [(e.label, e.origin) for e in data] == [("phone", "user")]


# ----------------------------------------------- dane do nauki z bazy ---

class Judge:
    """Model pomocniczy dla ukrytych ofert: „telefon” dla tytułów z GB, „część” dla reszty."""

    def predict(self, titles):
        return [{"phone": 0.97, "accessory": 0.01, "part": 0.01, "wanted": 0.01} if "GB" in t else
                {"phone": 0.2, "accessory": 0.3, "part": 0.45, "wanted": 0.05} for t in titles]


def test_training_set_from_database(conn):
    settings = Settings()
    rejected = RejectedRepository(conn)
    rejected.add(make_raw("Etui skórzane iPhone 13"), "accessory", "akcesorium", "etui")
    rejected.add(make_raw("iPhone 12 13 14 obal"), "multi_model", "kilka generacji")
    rejected.add(make_raw("Wyświetlacz iPhone 11"), "part", "część", "wyświetlacz")
    rejected.add(make_raw("Kupię iPhone"), "wanted", "kupno", "kupię")
    rejected.add(make_raw("iPhone 13 128GB (CZ)"), "country", "sprzedawca z Czech")  # nie mówi, czym jest przedmiot
    labels = LabelRepository(conn)
    labels.add(make_raw("iPhone 13 pro 256 GB zielony"), "phone", "user")
    labels.add(make_raw("Ramka aparatu iPhone 12"), "accessory", "hidden")  # judge: część
    labels.add(make_raw("iPhone 14 128GB"), "accessory", "hidden")  # judge: telefon → pominięta

    data = build_training_set(conn, settings, judge=Judge())
    extra = {(e.title, e.label, e.origin) for e in data.examples if e.origin != "seed"}
    assert extra == {
        ("Etui skórzane iPhone 13", "accessory", "rejected"), ("iPhone 12 13 14 obal", "accessory", "rejected"),
        ("Wyświetlacz iPhone 11", "part", "rejected"), ("Kupię iPhone", "wanted", "rejected"),
        ("iPhone 13 pro 256 GB zielony", "phone", "user"), ("Ramka aparatu iPhone 12", "part", "hidden"),
    }
    assert data.hidden_skipped == 1
    assert labels_count(conn, settings) == 3

    settings.ml.learn_from_hidden = False
    data = build_training_set(conn, settings, judge=Judge())
    assert not [e for e in data.examples if e.origin == "hidden"] and data.hidden_skipped == 0
    assert labels_count(conn, settings) == 1


def test_hiding_never_overrides_your_label(conn):
    raw = make_raw("iPhone 13 128GB")
    labels = LabelRepository(conn)
    labels.add(raw, "phone", "user")
    labels.add(raw, "accessory", "hidden")
    assert labels.all() == [("iPhone 13 128GB", "phone", "user")]
    labels.add(raw, "part", "user")  # Twoja zmiana zdania — nadpisuje
    assert labels.all() == [("iPhone 13 128GB", "part", "user")]


def test_needs_retraining_after_new_labels_or_rejected(conn, tmp_path, monkeypatch):
    settings = Settings()
    settings.ml.retrain_after_labels = 3
    svc = AiService(conn, settings, tmp_path)
    assert not svc.needs_retraining()  # brak modelu — najpierw trening startowy
    text_model.set_classifier(FakeClassifier(labels_seen=0, rejected_seen=0))
    labels = LabelRepository(conn)
    for i in range(2):
        labels.add(make_raw(f"Etui {i}"), "accessory", "user")
    assert not svc.needs_retraining()
    labels.add(make_raw("Etui 3"), "accessory", "user")
    assert svc.needs_retraining()

    text_model.set_classifier(FakeClassifier(labels_seen=3, rejected_seen=0))
    monkeypatch.setattr(ai_service, "REJECTED_RETRAIN_STEP", 2)
    RejectedRepository(conn).add(make_raw("Etui A"), "accessory", "akcesorium")
    assert not svc.needs_retraining()
    RejectedRepository(conn).add(make_raw("Etui B"), "accessory", "akcesorium")
    assert svc.needs_retraining()
    settings.ml.text_enabled = False
    assert not svc.needs_retraining()


def test_first_training_and_retraining_use_database(conn, tmp_path):
    settings = Settings()
    _, phone_id = store(conn, "iPhone 13 128GB czarny")
    raw_case, _ = store(conn, "Etui MagSafe iPhone 13 przezroczyste")
    LabelRepository(conn).add(make_raw("Ringke Fusion iPhone 14 nowe w folii"), "accessory", "user")
    RejectedRepository(conn).add(make_raw("Etui z klapką iPhone 12"), "accessory", "akcesorium", "etui")
    svc = AiService(conn, settings, tmp_path)

    clf = svc.text_classifier(train_if_missing=True)  # pierwsze uruchomienie: model z danych w bazie
    assert clf is not None and (tmp_path / text_model.MODEL_FILE).exists()
    assert clf.info.sources.get("user") == 1 and clf.info.sources.get("rejected") == 1
    assert clf.info.labels_seen == 1 and clf.info.rejected_seen == 1
    assert not svc.needs_retraining()
    # wynik dla wszystkich aktywnych ofert — z identyfikatorem modelu, żeby nie liczyć drugi raz
    repo = AiRepository(conn)
    assert repo.missing_text(ai_service.model_id(clf.info)) == []
    offers = {o.id: o for o in OfferRepository(conn).list()}
    assert text_model.top(offers[phone_id].layers.text_probs)[0] == "phone"
    assert svc.update_text_predictions() == 0

    info = svc.retrain()
    assert info.trained_at >= clf.info.trained_at and text_model.current_classifier().info is info
    assert repo.text_model_of(raw_case.source, raw_case.source_id) == ai_service.model_id(info)


def test_scanner_stores_title_predictions(trained, tmp_path):
    from .sample_data import build_sample_db

    text_model.set_classifier(trained)
    conn, report = build_sample_db(tmp_path / "t.sqlite3")
    offers = OfferRepository(conn).list()
    assert offers and all(o.layers is not None and o.layers.text_probs for o in offers)
    conn.close()


# ------------------------------------------------------ łączenie warstw ---

def layers(text=None, photo=None, error=None):
    return AiLayers(text_label=text and max(text, key=text.get), text_probs=text or {},
                    photo_label=photo and max(photo, key=photo.get), photo_probs=photo or {}, photo_error=error)


PHONE = {"phone": 0.95, "accessory": 0.03, "part": 0.01, "wanted": 0.01}
UNSURE_T = {"phone": 0.5, "accessory": 0.3, "part": 0.1, "wanted": 0.1}
ACC_T = {"phone": 0.1, "accessory": 0.85, "part": 0.04, "wanted": 0.01}
SMART = {"smartphone": 0.9, "case": 0.05, "screen_protector": 0.03, "box": 0.02}
CASE_85 = {"smartphone": 0.1, "case": 0.85, "screen_protector": 0.03, "box": 0.02}
CASE_75 = {"smartphone": 0.2, "case": 0.75, "screen_protector": 0.03, "box": 0.02}
MIXED = {"smartphone": 0.4, "case": 0.3, "screen_protector": 0.2, "box": 0.1}


@pytest.mark.parametrize("ai, state, flags", [
    (None, "brak danych", []),
    (layers(PHONE), "zgodne", []),
    (layers(PHONE, SMART), "zgodne", []),
    (layers(UNSURE_T, SMART), "zgodne", []),  # zdjęcie potwierdza, gdy tytuł niepewny
    (layers(UNSURE_T), "niska pewność", [RedFlag.AI_LOW_CONFIDENCE]),
    (layers(UNSURE_T, MIXED), "niska pewność", [RedFlag.AI_LOW_CONFIDENCE]),
    (layers(ACC_T, SMART), "sprzeczne", [RedFlag.AI_TEXT_CONFLICT]),
    (layers(PHONE, CASE_85), "sprzeczne", [RedFlag.AI_PHOTO_CONFLICT]),
    (layers(ACC_T, CASE_85), "sprzeczne", [RedFlag.AI_TEXT_CONFLICT, RedFlag.AI_PHOTO_CONFLICT]),
    (layers(PHONE, CASE_75), "zgodne", []),  # etui 75% < progu 80% — to nie sprzeczność
    (layers(PHONE, error="HTTP 404"), "zgodne", []),
])
def test_combine_truth_table(ai, state, flags):
    result = combine.combine(ai, MlConfig())
    assert (result.state, result.flags) == (state, flags)
    assert [layer.name for layer in result.layers] == ["Tytuł (klasyfikator)", "Zdjęcie (CLIP)", "Opis (Ollama)"]


def test_combine_layers_describe_confidence_and_switches():
    result = combine.combine(layers(PHONE, error="HTTP 404"), MlConfig())
    assert result.layers[0].summary.startswith("telefon 95%")
    assert result.layers[1].state == combine.MISSING and "HTTP 404" in result.layers[1].summary
    off = MlConfig(text_enabled=False, photo_enabled=False)
    result = combine.combine(layers(ACC_T, CASE_85), off)
    assert (result.state, result.flags) == ("brak danych", [])
    assert MlConfig().photo_conflict_conf == 0.80  # próg sprawdzony na 80 prawdziwych zdjęciach


def test_ai_conflict_caps_verdict_to_verify():
    settings = Settings()
    market = MarketEstimate(1900, 12, "t", "wysoka", 1900)
    offer = make_offer("iPhone 13 128GB czarny, bateria 90%", price=1100)
    assert evaluate(offer, market, PARTS, settings, Mode.REPAIR).verdict is Verdict.BUY
    offer.layers = layers(PHONE, SMART)
    assert evaluate(offer, market, PARTS, settings, Mode.REPAIR).verdict is Verdict.BUY
    for ai, flag in ((layers(ACC_T), RedFlag.AI_TEXT_CONFLICT), (layers(PHONE, CASE_85), RedFlag.AI_PHOTO_CONFLICT),
                     (layers(UNSURE_T), RedFlag.AI_LOW_CONFIDENCE)):
        offer.layers = ai
        val = evaluate(offer, market, PARTS, settings, Mode.REPAIR)
        assert val.verdict is Verdict.VERIFY and flag in val.flags
        assert any(r.startswith("Werdykt obniżony z KUPUJ na DO WERYFIKACJI") for r in val.reasons)
        assert val.score < settings.score_green


def test_details_show_each_layer_with_confidence():
    from phonebot.ui.details_html import build_details_html

    settings = Settings()
    offer = make_offer("iPhone 13 128GB", price=1100)
    offer.layers = layers(PHONE, CASE_85)
    val = evaluate(offer, MarketEstimate(1900, 12, "t", "wysoka", 1900), PARTS, settings, Mode.REPAIR)
    html = build_details_html(offer, val, settings)
    assert "Ocena warstw (reguły + lokalne AI)" in html
    assert "Reguły (etap 1)" in html and "Tytuł (klasyfikator)" in html and "Zdjęcie (CLIP)" in html
    assert "telefon 95%" in html and "etui 85%" in html and "sprzeczne" in html


# ------------------------------------------------------ analiza zdjęć ---

def test_class_embeddings_verified():
    emb = photo_model.class_embeddings()
    assert emb.shape == (len(CLASSES), 512)
    assert np.allclose(np.linalg.norm(emb, axis=1), 1, atol=1e-5)
    sim = emb @ emb.T
    assert sim[0, 1] == pytest.approx(0.923, abs=0.002)  # smartfon ↔ etui, jak w GitHub Actions


def test_class_embeddings_checksum(monkeypatch):
    raw = bytearray(__import__("base64").b64decode(photo_model._EMB_B64))
    raw[10] ^= 0xFF
    monkeypatch.setattr(photo_model, "_EMB_B64", __import__("base64").b64encode(bytes(raw)).decode())
    with pytest.raises(PhotoModelError):
        photo_model.class_embeddings()


def test_preprocess_like_clip():
    pix = preprocess(sample_jpeg((200, 30, 30), (320, 240)))
    assert pix.shape == (1, 3, 224, 224) and pix.dtype == np.float32
    red = (200 / 255 - photo_model._MEAN[0]) / photo_model._STD[0]
    assert float(pix[0, 0].mean()) == pytest.approx(red, abs=0.05)
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (100, 400), (0, 0, 255, 128)).save(buf, "PNG")  # pionowe, z przezroczystością
    assert preprocess(buf.getvalue()).shape == (1, 3, 224, 224)
    with pytest.raises(PhotoModelError):
        preprocess(b"<html>to nie jest zdjecie</html>")


def test_photo_classifier_runs_onnx_model(tmp_path):
    clf = PhotoClassifier.load(tiny_vision_model(tmp_path))
    probs = clf.classify(sample_jpeg())
    assert set(probs) == set(CLASSES) and sum(probs.values()) == pytest.approx(1)
    assert max(probs, key=probs.get) == "smartphone" and probs["smartphone"] > 0.5


class FakeSession:
    """Sesja onnxruntime zwracająca wektor wybranej klasy (wejście fp16 jak w modelu Xenova)."""

    class _Arg:
        def __init__(self, name, type_="tensor(float16)"):
            self.name, self.type = name, type_

    def __init__(self, cls: str):
        self.vec = photo_model.class_embeddings()[CLASSES.index(cls)]
        self.fed = None

    def get_inputs(self):
        return [self._Arg("pixel_values")]

    def get_outputs(self):
        return [self._Arg("text_embeds"), self._Arg("image_embeds")]

    def run(self, _names, feed):
        self.fed = feed["pixel_values"]
        return [np.zeros((1, 512)), self.vec[None].astype(np.float16)]


def test_photo_classifier_uses_image_embeds_and_fp16():
    session = FakeSession("case")
    probs = PhotoClassifier(session).classify(sample_jpeg())
    assert session.fed.dtype == np.float16 and session.fed.shape == (1, 3, 224, 224)
    assert max(probs, key=probs.get) == "case" and probs["case"] > 0.9


def _model_server(content: bytes, status: int = 200, calls: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        return httpx.Response(status, content=content, headers={"content-length": str(len(content))})

    return httpx.Client(transport=httpx.MockTransport(handler))


def test_download_model_checks_sha256(tmp_path):
    content = b"model" * 1000
    good = hashlib.sha256(content).hexdigest()
    progress = []
    with pytest.raises(PhotoModelError, match="sumę kontrolną"):
        download_model(tmp_path, client=_model_server(content), sha256="0" * 64)
    assert list(tmp_path.iterdir()) == []  # zły plik usunięty, nic nie trafiło na miejsce
    with pytest.raises(PhotoModelError, match="HTTP 404"):
        download_model(tmp_path, client=_model_server(b"", 404), sha256=good)
    path = download_model(tmp_path, client=_model_server(content), sha256=good,
                          progress=lambda done, total: progress.append((done, total)))
    assert path.read_bytes() == content and progress[-1] == (len(content), len(content))
    calls: list[str] = []
    assert download_model(tmp_path, client=_model_server(content, calls=calls), sha256=good) == path
    assert calls == []  # już pobrany — bez ponownego pobierania


def test_photo_model_not_downloaded_without_permission(tmp_path):
    with pytest.raises(PhotoModelError, match="nie jest jeszcze pobrany"):
        photo_model.get_photo_classifier(tmp_path, download=False)
    (tmp_path / photo_model.MODEL_FILE).write_bytes(b"uszkodzony")
    with pytest.raises(PhotoModelError, match="nie da się wczytać"):
        photo_model.get_photo_classifier(tmp_path, download=False)


class FakePhotoClassifier:
    def __init__(self):
        self.seen = 0

    def classify(self, data: bytes):
        self.seen += 1
        preprocess(data)  # błędny plik → PhotoModelError, jak w prawdziwym modelu
        return {"smartphone": 0.1, "case": 0.86, "screen_protector": 0.02, "box": 0.02}


def _cdn(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("missing.jpg"):
        return httpx.Response(404)
    if request.url.path.endswith("broken.jpg"):
        return httpx.Response(200, content=b"<html>")
    return httpx.Response(200, content=sample_jpeg())


def test_analyze_photos_saves_results_and_errors_once(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_service, "PHOTO_HOST_DELAY_S", 0)
    raws = [store(conn, t, photos=[f"https://cdn.example/{name}.jpg"])[0]
            for t, name in (("iPhone 13 etui", "ok"), ("iPhone 12", "missing"), ("iPhone 11", "broken"))]
    fake = FakePhotoClassifier()
    svc = AiService(conn, Settings(), tmp_path)
    jobs = [PhotoJob(r.source, r.source_id, r.photos[0]) for r in raws]
    progress = []
    done = svc.analyze_photos(jobs, classifier=fake, client=httpx.Client(transport=httpx.MockTransport(_cdn)),
                              progress=lambda i, n: progress.append((i, n)))
    assert done == 3 and progress[-1] == (3, 3)
    by_title = {o.raw.title: o.layers for o in OfferRepository(conn).list()}
    assert by_title["iPhone 13 etui"].photo_label == "case" and by_title["iPhone 13 etui"].photo_at is not None
    assert by_title["iPhone 12"].photo_error == "HTTP 404"
    assert "nie da się odczytać" in by_title["iPhone 11"].photo_error
    # wynik oferty (także błąd) zostaje w bazie — okno nie zleca jej drugi raz
    assert all(layer.photo_at is not None for layer in by_title.values())
    offer = next(o for o in OfferRepository(conn).list() if o.raw.title == "iPhone 13 etui")
    assert combine.combine(offer.layers, MlConfig()).flags == [RedFlag.AI_PHOTO_CONFLICT]


def test_analyze_photos_can_be_stopped(conn, tmp_path, monkeypatch):
    monkeypatch.setattr(ai_service, "PHOTO_HOST_DELAY_S", 0)
    jobs = [PhotoJob("test", str(i), f"https://cdn.example/{i}.jpg") for i in range(5)]
    fake = FakePhotoClassifier()
    done = AiService(conn, Settings(), tmp_path).analyze_photos(
        jobs, classifier=fake, client=httpx.Client(transport=httpx.MockTransport(_cdn)), stop=lambda: fake.seen >= 2)
    assert done == 2


class FakeTime:
    def __init__(self):
        self.now, self.slept = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


def test_photos_downloaded_politely(conn, tmp_path, monkeypatch):
    clock = FakeTime()
    monkeypatch.setattr(ai_service, "time", clock)
    svc = AiService(conn, Settings(), tmp_path)
    svc._polite_wait("https://images1.vinted.net/a.jpg")  # pierwsze zdjęcie z serwera — bez czekania
    clock.now += 0.3
    svc._polite_wait("https://images1.vinted.net/b.jpg")  # 0,3 s później → poczekaj jeszcze 0,7 s
    svc._polite_wait("https://a.allegroimg.com/c.jpg")  # inny serwer — bez czekania
    clock.now += 5
    svc._polite_wait("https://images1.vinted.net/d.jpg")  # dawno temu — bez czekania
    assert clock.slept == [pytest.approx(ai_service.PHOTO_HOST_DELAY_S - 0.3)]


# ---------------------------------------------------------- wątek AI ---

def test_ai_worker_trains_on_start_and_retries_photo_model_rarely(tmp_path, monkeypatch):
    from phonebot.storage.db import open_database
    from phonebot.ui import ai_worker as worker_mod

    db = tmp_path / "w.sqlite3"
    conn = open_database(db)
    raw, _ = store(conn, "iPhone 13 128GB czarny")
    conn.close()
    settings = Settings()
    settings.ml.photo_enabled = False
    worker = worker_mod.AiWorker(db, settings, tmp_path / "models")
    updates, statuses = [], []
    worker.text_updated.connect(updates.append)
    worker.status.connect(statuses.append)
    worker.start()
    assert updates == [-1] and (tmp_path / "models" / text_model.MODEL_FILE).exists()  # -1: wszystkie oferty
    assert any("klasyfikator tytułów gotowy" in s for s in statuses)

    attempts = []

    def unavailable(*_a, **_kw):
        attempts.append(1)
        raise PhotoModelError("brak internetu")

    monkeypatch.setattr(photo_model, "get_photo_classifier", unavailable)
    worker.settings.ml.photo_enabled = True
    ready = []
    worker.photo_model_ready.connect(ready.append)
    worker.analyze([PhotoJob(raw.source, raw.source_id, "https://cdn.example/1.jpg")])
    worker.analyze([PhotoJob(raw.source, raw.source_id, "https://cdn.example/1.jpg")])
    assert attempts == [1]  # druga próba pobrania dopiero po kwadransie
    assert ready == [False, False]  # okno zleci te zdjęcia ponownie później
    assert "niedostępna" in statuses[-1]
    worker.close()


def test_ai_worker_analyzes_photos_in_chunks(tmp_path, monkeypatch):
    from phonebot.storage.db import open_database
    from phonebot.ui import ai_worker as worker_mod

    db = tmp_path / "w.sqlite3"
    open_database(db).close()
    calls = []

    def fake_analyze(self, jobs, *, stop=None, progress=None, **_kw):
        calls.append(len(jobs))
        for i in range(len(jobs)):
            if stop and stop():
                return i
            if progress:
                progress(i + 1, len(jobs))
        return len(jobs)

    monkeypatch.setattr(worker_mod.AiService, "analyze_photos", fake_analyze)
    worker = worker_mod.AiWorker(db, Settings(), tmp_path / "models")
    worker._photo_ready = True
    done, statuses = [], []
    worker.photos_done.connect(done.append)
    worker.status.connect(statuses.append)
    worker.analyze([PhotoJob("test", str(i), f"https://cdn.example/{i}.jpg") for i in range(12)])
    while worker._pending:  # w programie porcje wywołuje pętla zdarzeń wątku AI
        worker.process_chunk()
    assert calls == [5, 5, 2] and done == [5, 5, 2]
    assert "AI: analiza zdjęć 12/12" in statuses and statuses[-1] == "AI: przeanalizowano 12 zdjęć"
    # wyłączenie analizy zdjęć w ustawieniach czyści kolejkę od razu
    worker.analyze([PhotoJob("test", str(i), f"https://cdn.example/{i}.jpg") for i in range(12)])
    worker.settings.ml.photo_enabled = False
    assert worker.process_chunk() == 0 and not worker._pending
    worker.close()


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


def test_not_phone_button_rejects_offer_and_teaches_model(window):
    offer_id = window.model.row_at(0)[0].id
    window._select_offer(offer_id)
    assert window.details.offer is not None and window.details.offer.id == offer_id
    title = window.details.offer.raw.title
    menu = window.details.not_phone_btn.menu()
    assert [a.text() for a in menu.actions()][0].startswith("Akcesorium")
    menu.actions()[0].trigger()
    assert window.model.row_of(offer_id) is None  # zniknęła z tabeli
    rejected = RejectedRepository(window.conn).list()
    assert rejected[0].title == title and rejected[0].stage == "manual"
    assert LabelRepository(window.conn).all()[-1] == (title, "accessory", "user")
    # pomyłka? „To jest telefon” w oknie „Odrzucone” przywraca ofertę i zmienia oznaczenie
    RejectedRepository(window.conn).restore(rejected[0].id)
    assert LabelRepository(window.conn).all()[-1] == (title, "phone", "user")


def test_hiding_is_a_weak_label(window):
    offer = window.model.row_at(0)[0]
    labels = LabelRepository(window.conn)
    window.set_offer_status(offer.id, OfferStatus.HIDDEN)
    assert (offer.raw.title, "accessory", "hidden") in labels.all()
    window.show_hidden_action.setChecked(True)
    window.reload()
    window.set_offer_status(offer.id, OfferStatus.NEW)
    assert not [lbl for lbl in labels.all() if lbl[0] == offer.raw.title]


def test_photo_analysis_queued_once_for_candidates(window):
    window.ai_worker = object()  # atrapa wątku AI
    sent = []
    window.ai_photos_requested.connect(sent.append)
    window._queue_photo_analysis()
    window._queue_photo_analysis()  # drugi raz nic — każda oferta raz
    assert len(sent) == 1
    jobs = sent[0]
    by_id = {o.id: (o, v) for o, v in window.model.rows()}
    assert jobs and all(by_id[j.offer_id][1].verdict is not Verdict.SKIP for j in jobs)
    assert all(j.url == by_id[j.offer_id][0].raw.photos[0] for j in jobs)
    skipped = [o for o, v in window.model.rows() if v.verdict is Verdict.SKIP]
    assert not {o.id for o in skipped} & {j.offer_id for j in jobs}
    window.ai_worker = None


def test_ai_settings_tab_shows_accuracy_and_retrains(window, trained):
    from PySide6.QtWidgets import QDoubleSpinBox

    text_model.set_classifier(trained)
    retrain = []
    window.ai_retrain_requested.connect(lambda: retrain.append(1))
    dialog = window.open_settings()
    text = dialog.model_label.text()
    assert f"Skuteczność na danych testowych: {trained.info.accuracy:.0%}" in text
    assert f"na prawdziwych tytułach z portali: {trained.info.benchmark_accuracy:.0%}" in text
    assert dialog.findChild(QDoubleSpinBox, "ml.photo_conflict_conf").value() == 80
    dialog.retrain_btn.click()
    assert retrain == [1] and dialog.training_label.text() == "Douczanie w tle…"
    dialog.set_model_info(trained.info)  # wynik douczania pokazany bez zamykania okna
    assert dialog.training_label.text() == ""
    dialog.reject()
