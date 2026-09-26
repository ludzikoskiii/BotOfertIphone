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
    import anthropic  # noqa: F401  (ładowane leniwie w analizie AI)

    from .ml import selftest
    from .sources import REGISTRY
    from .ui.main_window import MainWindow

    ai = selftest.run()

    app = QApplication.instance() or QApplication(sys.argv)
    tmp = Path(tempfile.mkdtemp())
    conn = open_database(tmp / "selftest.sqlite3")
    PartsRepository(conn).seed_defaults_if_empty()
    window = MainWindow(conn, tmp / "selftest.sqlite3", thumbs_dir=tmp)
    window._quitting = True
    window.close()
    conn.close()
    app.processEvents()
    return f"PhoneBot {__version__} self-test OK; portale: {', '.join(sorted(REGISTRY))}; {ai}"


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
