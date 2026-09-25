"""Zrzut ekranu okna głównego na danych testowych (bez sieci).

    QT_QPA_PLATFORM=offscreen python tools/screenshot.py docs/screenshots/okno.png [docs/screenshots/szczegoly.png]
"""
from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx
from PySide6.QtWidgets import QApplication

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from phonebot.core.settings import Settings  # noqa: E402
from phonebot.net.http import HostRateLimiter, HttpClient  # noqa: E402
from phonebot.services.scanner import Scanner  # noqa: E402
from phonebot.storage.db import open_database  # noqa: E402
from phonebot.storage.repositories import PartsRepository, SettingsRepository  # noqa: E402

FIX = ROOT / "tests" / "fixtures"


def main(out: str, details_out: str | None = None) -> None:
    pages = {False: json.loads((FIX / "olx_page1.json").read_text(encoding="utf-8")),
             True: json.loads((FIX / "olx_page2.json").read_text(encoding="utf-8"))}
    tmp = Path(tempfile.mkdtemp())
    db = tmp / "demo.sqlite3"
    conn = open_database(db)
    PartsRepository(conn).seed_defaults_if_empty()
    settings = Settings(max_pages_per_query=2)
    SettingsRepository(conn).save(settings)
    limiter = HostRateLimiter(0)

    def handler(request):
        return httpx.Response(200, json=pages["offset=40" in str(request.url)])

    scanner = Scanner(conn, settings, limiter,
                      http_factory=lambda: HttpClient(limiter, transport=httpx.MockTransport(handler)))
    asyncio.run(scanner.run())

    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    from phonebot.ui.main_window import MainWindow

    win = MainWindow(conn, db, thumbs_dir=tmp)
    win.resize(1500, 620)
    win.show()
    app.processEvents()
    win.grab().save(out)
    print("zapisano", out)
    if details_out:
        dialog = win.show_details(win.proxy.index(0, 0))
        dialog.resize(1000, 900)
        app.processEvents()
        dialog.grab().save(details_out)
        print("zapisano", details_out)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "screenshot.png", sys.argv[2] if len(sys.argv) > 2 else None)
