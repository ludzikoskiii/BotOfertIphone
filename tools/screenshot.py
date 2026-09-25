"""Zrzut ekranu okna głównego na danych testowych (bez sieci).

    QT_QPA_PLATFORM=offscreen python tools/screenshot.py docs/screenshots/okno.png [docs/screenshots/szczegoly.png]
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tests.sample_data import build_sample_db  # noqa: E402


def save(widget, path: str) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if not widget.grab().save(path):
        raise SystemExit(f"Nie udało się zapisać {path}")
    print("zapisano", path)


def main(out: str, details_out: str | None = None) -> None:
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "demo.sqlite3"
    conn, _ = build_sample_db(db)

    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    from phonebot.ui.main_window import MainWindow

    win = MainWindow(conn, db, thumbs_dir=tmp)
    win.resize(1700, 760)
    win.show()
    app.processEvents()
    save(win, out)
    if details_out:
        dialog = win.show_details(win.proxy.index(0, 0))
        dialog.resize(1000, 900)
        app.processEvents()
        save(dialog, details_out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "screenshot.png", sys.argv[2] if len(sys.argv) > 2 else None)
