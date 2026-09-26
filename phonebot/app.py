"""Punkt startowy aplikacji: ``python -m phonebot``."""
from __future__ import annotations

import logging
import sys

from PySide6.QtCore import QLibraryInfo, QLocale, QTranslator
from PySide6.QtWidgets import QApplication

from . import __version__
from .logging_setup import setup_logging
from .paths import db_path
from .storage.db import open_database
from .storage.repositories import PartsRepository

log = logging.getLogger(__name__)


def self_test() -> int:
    """Sprawdza, czy spakowana aplikacja ma wszystkie moduły, lokalne AI działa, a okno się otwiera.

    Program okienkowy nie ma konsoli — wynik (albo błąd) trafia też do pliku z ``PHONEBOT_SELFTEST_LOG``.
    """
    import os

    try:
        summary, code = _self_test(), 0
    except Exception:  # noqa: BLE001 — kod wyjścia zamiast okna błędu (budowanie w CI czeka na wynik)
        import traceback

        summary, code = traceback.format_exc(), 1
    print(summary)
    if log_path := os.environ.get("PHONEBOT_SELFTEST_LOG"):
        with open(log_path, "w", encoding="utf-8") as f:
            f.write(summary + "\n")
    return code


def _self_test() -> str:
    import os
    import tempfile
    from pathlib import Path

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from .ml import selftest
    from .sources import REGISTRY
    from .ui.main_window import MainWindow

    ai = selftest.run()
    from .core.secret_store import is_encrypted, protect, unprotect

    stored = protect("123:SELFTEST")
    if unprotect(stored) != "123:SELFTEST" or (is_encrypted() and not stored.startswith("dpapi:")):
        raise RuntimeError("magazyn sekretów (DPAPI) nie działa")
    secrets = "sekrety: DPAPI" if is_encrypted() else "sekrety: bez szyfrowania (nie Windows)"
    import io

    from PIL import Image

    from .services.photo_hash import analyze

    buf = io.BytesIO()
    Image.new("RGB", (120, 120), "white").save(buf, "JPEG")
    if not analyze(buf.getvalue())[1]:
        raise RuntimeError("analiza zdjęć (oszustwa) nie działa")

    app = QApplication.instance() or QApplication(sys.argv)
    tmp = Path(tempfile.mkdtemp())
    conn = open_database(tmp / "selftest.sqlite3")
    PartsRepository(conn).seed_defaults_if_empty()
    window = MainWindow(conn, tmp / "selftest.sqlite3", thumbs_dir=tmp)
    window._quitting = True
    window.close()
    web = _web_self_test(tmp / "selftest.sqlite3")
    conn.close()
    app.processEvents()
    return f"PhoneBot {__version__} self-test OK; portale: {', '.join(sorted(REGISTRY))}; {ai}; {secrets}; {web}"


def _web_self_test(db_path) -> str:
    """Wersja na telefon: serwer na 127.0.0.1, logowanie PIN-em, strona listy, ikona PWA."""
    import httpx

    from .core.settings import Settings
    from .web.auth import hash_pin
    from .web.server import WebServer

    settings = Settings(web_enabled=True, web_port=0, web_pin_hash=hash_pin("2468"))
    server = WebServer(db_path, settings)
    server.start(settings)
    try:
        with httpx.Client(base_url=server.local_url(), follow_redirects=True, trust_env=False, timeout=10) as c:
            if c.post("/login", data={"pin": "2468"}).status_code != 200 or c.get("/icon-192.png").content[:4] != b"\x89PNG":
                raise RuntimeError("wersja na telefon nie działa")
    finally:
        server.stop()
    return "telefon (www) OK"


def diagnose_cli() -> int:
    """PhoneBot.exe --diagnose: raport do %LOCALAPPDATA%\\PhoneBot\\diagnostyka.txt i otwarcie go."""
    import asyncio
    import os

    from .diagnose import diagnose, format_report
    from .paths import data_dir

    setup_logging()
    path = data_dir() / "diagnostyka.txt"
    path.write_text(format_report(asyncio.run(diagnose())), encoding="utf-8")
    if sys.platform == "win32":
        os.startfile(path)  # type: ignore[attr-defined]
    elif sys.stdout is not None:
        print(path.read_text(encoding="utf-8"))
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
    if "--diagnose" in sys.argv:
        return diagnose_cli()
    setup_logging()
    log.info("PhoneBot %s — start", __version__)
    path = db_path()
    conn = open_database(path)
    seeded = PartsRepository(conn).seed_defaults_if_empty()
    if seeded:
        log.info("Wczytano domyślną tabelę części (%d pozycji)", seeded)

    app = QApplication(sys.argv)
    app.setApplicationName("PhoneBot")
    app.setStyle("Fusion")
    QLocale.setDefault(QLocale(QLocale.Language.Polish, QLocale.Country.Poland))
    translator = QTranslator(app)
    if translator.load("qtbase_pl", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
        app.installTranslator(translator)

    from .ui.main_window import MainWindow

    # aplikacja żyje w zasobniku — kończy ją „Zakończ” albo zamknięcie okna (gdy zasobnik wyłączony)
    app.setQuitOnLastWindowClosed(False)
    window = MainWindow(conn, path)
    window.quit_on_close = True
    window.show()
    window.start_ai()  # lokalne AI w osobnym wątku: modele ładowane raz, analiza w tle
    code = app.exec()
    try:
        conn.execute("PRAGMA optimize")  # aktualizuje statystyki zapytań SQLite (szybkie, raz przy wyjściu)
    except Exception:  # noqa: BLE001
        log.debug("PRAGMA optimize nie powiodło się", exc_info=True)
    conn.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
