"""Konfiguracja logów: plik z rotacją + konsola."""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler

from .paths import logs_dir

_FORMAT = "%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"


def setup_logging(level: int = logging.INFO) -> None:
    root = logging.getLogger()
    if any(isinstance(h, RotatingFileHandler) for h in root.handlers):
        return
    root.setLevel(level)
    file_handler = RotatingFileHandler(
        logs_dir() / "phonebot.log", maxBytes=2_000_000, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(file_handler)
    if sys.stderr is not None:  # w PhoneBot.exe (bez konsoli) stderr nie istnieje
        console = logging.StreamHandler()
        console.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(console)
    logging.getLogger("httpx").setLevel(logging.WARNING)
