"""Akcje na ofercie wspólne dla okna programu i wersji na telefon: obserwuj, ukryj, (nie) telefon."""
from __future__ import annotations

import sqlite3

from ..core.models import Offer, OfferStatus
from ..core.settings import Settings
from ..ml.seed_data import LABEL_NAMES
from ..storage.repositories import LabelRepository, OfferRepository, RejectedRepository


def set_status(conn: sqlite3.Connection, settings: Settings, offer: Offer, status: OfferStatus) -> None:
    """Obserwuj / przestań / ukryj / odkryj. Ukrycie to słaba wskazówka dla klasyfikatora, że to nie telefon."""
    OfferRepository(conn).set_status(offer.id, status)
    labels = LabelRepository(conn)
    if status is OfferStatus.HIDDEN and settings.ml.learn_from_hidden:
        labels.add(offer.raw, "accessory", "hidden")
    elif status is not OfferStatus.HIDDEN:
        labels.remove(offer.raw.source, offer.raw.source_id, origin="hidden")
    offer.status = status


def mark_not_phone(conn: sqlite3.Connection, offer: Offer, label: str) -> str:
    """„To nie jest telefon”: oferta do „Odrzucone”, tytuł do nauki klasyfikatora. Zwraca opis dla użytkownika."""
    LabelRepository(conn).add(offer.raw, label, "user")
    RejectedRepository(conn).reject_stored(offer, "manual", f"oznaczone ręcznie: {LABEL_NAMES[label]}")
    return f"„{offer.raw.title[:40]}” przeniesiono do „Odrzucone” ({LABEL_NAMES[label]})."


def mark_phone(conn: sqlite3.Connection, offer: Offer) -> str:
    """„To jest telefon”: Twoja poprawka uczy klasyfikator (np. gdy AI podejrzewało akcesorium)."""
    LabelRepository(conn).add(offer.raw, "phone", "user")
    return f"„{offer.raw.title[:40]}” oznaczono jako telefon."
