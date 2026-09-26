"""Zadanie 3: gotowa wiadomość negocjacyjna — style, tylko prawdziwe argumenty, 3–5 zdań, edycja przed kopią."""
from __future__ import annotations

import json
import os
import re

import pytest

from phonebot.core import messages
from phonebot.core.models import Verdict
from phonebot.core.settings import Settings

from .test_stage3 import valued

DESC = "Bateria 83%, drobne rysy na obudowie, bez pudełka."


def _sentences(text: str) -> int:
    body = "\n".join(line for line in text.splitlines() if not line.startswith("Pozdrawiam"))
    return len(re.findall(r"[.!?](?=\s|$)", body))


@pytest.fixture
def negotiate():
    offer, val = valued("iPhone 13 128GB", 1650, DESC)
    assert val.verdict is Verdict.NEGOTIATE
    return offer, val


@pytest.mark.parametrize("style", ["polite", "concrete", "pickup"])
@pytest.mark.parametrize("distance", [None, 20, 300])
def test_every_style_has_price_true_arguments_and_3_to_5_sentences(negotiate, style, distance):
    offer, val = negotiate
    offer.distance_km = distance
    key, text = messages.compose(offer, val, messages.DEFAULT_TEMPLATES, style=style, pickup_km=50)
    assert key == messages.NEGOTIATION_STYLES[style]
    assert messages.zl(messages.opening_price(offer, val)) in text
    assert 3 <= _sentences(text) <= 5, text
    assert "{" not in text and text.startswith("Dzień dobry")
    assert "Kondycja baterii 83%" in text  # najsilniejszy prawdziwy argument zawsze jest
    # gotówka przy odbiorze tylko w promieniu odbioru
    assert ("gotówką" in text) == (distance == 20)


def test_arguments_come_only_from_the_listing():
    offer, val = valued("iPhone 13 128GB", 1650, DESC)
    args = messages.arguments(offer, val)
    assert any("rysy" in a for a in args) and any("bez pudełka" in a for a in args)
    clean, cval = valued("iPhone 13 128GB", 1650, "Bez rys, bateria 95%, komplet z pudełkiem.")
    cargs = messages.arguments(clean, cval)
    assert not any("rysy" in a or "pudełka" in a or "bateri" in a for a in cargs)
    assert messages.no_box(valued("iPhone 13", 1650, "Sprzedam sam telefon")[0])


def test_variables_in_user_template(negotiate):
    offer, val = negotiate
    text = messages.render("{model} {pamięć} za {cena}, proponuję {propozycja}. {argumenty}", offer, val,
                           key="negotiate")
    assert text.startswith("iPhone 13 128 GB za 1 650 zł, proponuję")
    assert "Kondycja baterii" in text


def test_old_default_template_upgraded_but_custom_kept():
    old = json.loads(Settings().to_json())
    old["settings_version"] = 2
    old["message_templates"] = {"negotiate": messages.OLD_NEGOTIATE_TEMPLATE, "buy": "Moje: {telefon}"}
    s = Settings.from_json(json.dumps(old))
    assert s.message_templates["negotiate"] == messages.DEFAULT_TEMPLATES["negotiate"]
    assert s.message_templates["buy"] == "Moje: {telefon}"
    assert set(s.message_templates) == set(messages.TEMPLATE_KEYS)
    old["message_templates"] = {"negotiate": "Mój własny {propozycja}"}
    assert Settings.from_json(json.dumps(old)).message_templates["negotiate"] == "Mój własny {propozycja}"


# ------------------------------------------------------------------ okno ---

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def test_details_panel_shows_editable_message(negotiate, tmp_path):
    widgets = pytest.importorskip("PySide6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    from phonebot.storage.db import open_database
    from phonebot.storage.repositories import OfferRepository
    from phonebot.ui.images import ThumbnailCache
    from phonebot.ui.offer_details import OfferDetailsView

    conn = open_database(tmp_path / "m.sqlite3")
    settings = Settings(negotiation_style="concrete")
    view = OfferDetailsView(settings, OfferRepository(conn), ThumbnailCache(tmp_path))
    offer, val = negotiate
    view.set_offer(offer, val)
    assert view.msg_template.currentData() == "negotiate_concrete"
    assert view.message_text().startswith("Dzień dobry, piszę w sprawie: iPhone 13 128 GB.")
    # poprawka przed skopiowaniem trafia do schowka bez zmian
    view.msg_edit.setPlainText(view.message_text() + "\nMogę też dziś po 18.")
    assert view.copy_message().endswith("Mogę też dziś po 18.")
    assert widgets.QApplication.clipboard().text().endswith("Mogę też dziś po 18.")
    view.reset_msg_btn.click()  # od nowa — z szablonu
    assert "po 18" not in view.message_text()
    view.msg_template.setCurrentIndex(view.msg_template.findData("negotiate_pickup"))
    assert "Czy za" in view.message_text()
    view.deleteLater()
    app.processEvents()
    conn.close()
