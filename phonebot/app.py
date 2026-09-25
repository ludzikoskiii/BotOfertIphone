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
    """Sprawdza, czy spakowana aplikacja ma wszystkie moduły i potrafi otworzyć okno."""
    import os
    import tempfile
    from pathlib import Path

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import anthropic  # noqa: F401  (ładowane leniwie w analizie AI)

    from .sources import REGISTRY
    from .ui.main_window import MainWindow

    app = QApplication.instance() or QApplication(sys.argv)
    tmp = Path(tempfile.mkdtemp())
    conn = open_database(tmp / "selftest.sqlite3")
    PartsRepository(conn).seed_defaults_if_empty()
    window = MainWindow(conn, tmp / "selftest.sqlite3", thumbs_dir=tmp)
    window._quitting = True
    window.close()
    conn.close()
    app.processEvents()
    print(f"PhoneBot {__version__} self-test OK; portale: {', '.join(sorted(REGISTRY))}")
    return 0


def main() -> int:
    if "--self-test" in sys.argv:
        return self_test()
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
    code = app.exec()
    conn.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
