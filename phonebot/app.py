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


def main() -> int:
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

    window = MainWindow(conn, path)
    window.show()
    code = app.exec()
    conn.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
