"""Lokalizacja danych aplikacji (baza, logi, cache miniatur)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "PhoneBot"


def data_dir() -> Path:
    """%LOCALAPPDATA%\\PhoneBot na Windows, ~/.local/share/phonebot gdzie indziej.
    Można nadpisać zmienną środowiskową PHONEBOT_HOME."""
    override = os.environ.get("PHONEBOT_HOME")
    if override:
        base = Path(override)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / APP_NAME
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / APP_NAME.lower()
    base.mkdir(parents=True, exist_ok=True)
    return base


def db_path() -> Path:
    return data_dir() / "phonebot.sqlite3"


def logs_dir() -> Path:
    path = data_dir() / "logs"
    path.mkdir(exist_ok=True)
    return path


def thumbnails_dir() -> Path:
    path = data_dir() / "thumbnails"
    path.mkdir(exist_ok=True)
    return path
