"""Wersja na Linux: powiadomienia bez zasobnika, otwieranie raportów, skrót w menu i skrypt instalacyjny."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from phonebot.ui.main_window import _notify_send as real_notify_send  # przed podmianą w conftest

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging" / "linux"
posix_bash = pytest.mark.skipif(os.name != "posix" or shutil.which("bash") is None, reason="skrypt powłoki Linux")


class Recorder:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, args, **kwargs):
        self.calls.append(list(args))


def test_notify_send_only_on_linux(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "Popen", rec)
    monkeypatch.setattr(sys, "platform", "linux")
    assert real_notify_send("PhoneBot: 1 nowa zielona oferta", "iPhone 13 · zysk 500 zł")
    assert rec.calls == [["/usr/bin/notify-send", "-a", "PhoneBot", "-i", "phonebot",
                          "PhoneBot: 1 nowa zielona oferta", "iPhone 13 · zysk 500 zł"]]
    monkeypatch.setattr(sys, "platform", "win32")
    assert not real_notify_send("t", "b") and len(rec.calls) == 1


def test_desktop_without_tray_uses_system_notification(monkeypatch, tmp_path):
    from PySide6.QtWidgets import QApplication

    QApplication.instance() or QApplication([])

    from phonebot.services.post_scan import GreenOffer
    from phonebot.storage.db import open_database
    from phonebot.ui import main_window
    from phonebot.ui.main_window import MainWindow

    sent = []
    monkeypatch.setattr(main_window, "_notify_send", lambda title, body: sent.append((title, body)) or True)
    path = tmp_path / "n.sqlite3"
    win = MainWindow(open_database(path), path, thumbs_dir=tmp_path)
    win.tray = None  # np. GNOME bez rozszerzenia zasobnika
    win.settings.notify_desktop = True
    win.notify_green([GreenOffer(1, "iPhone 13 128 GB — 600 zł", 812.0, "u", "nowa")])
    assert sent and "1 nowa zielona oferta" in sent[0][0] and "zysk 812 zł" in sent[0][1]
    win.settings.notify_desktop = False
    win.notify_green([GreenOffer(2, "iPhone 12 — 500 zł", None, "u", "nowa")])
    assert len(sent) == 1
    win._quitting = True
    win.close()


def test_report_opens_in_default_editor_on_linux(monkeypatch, tmp_path, capsys):
    from phonebot.app import _open_report

    rec = Recorder()
    report = tmp_path / "diagnostyka.txt"
    report.write_text("Raport PhoneBot", encoding="utf-8")
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(subprocess, "Popen", rec)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.delenv("DISPLAY", raising=False)
    _open_report(report)  # bez pulpitu (np. przez SSH): tylko wypis w terminalu
    assert "Raport PhoneBot" in capsys.readouterr().out and rec.calls == []
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    _open_report(report)
    assert rec.calls == [["/usr/bin/xdg-open", str(report)]]


def test_desktop_entry_matches_application():
    entry = dict(line.split("=", 1) for line in (PKG / "phonebot.desktop").read_text(encoding="utf-8").splitlines()
                 if "=" in line)
    assert entry["Type"] == "Application" and entry["Exec"] == "@EXEC@" and entry["Terminal"] == "false"
    # nazwa pliku .desktop = app.setDesktopFileName(), ikona = -i w notify-send, klasa okna = nazwa aplikacji
    assert entry["Icon"] == "phonebot" and entry["StartupWMClass"] == "PhoneBot"
    assert 'setDesktopFileName("phonebot")' in (ROOT / "phonebot" / "app.py").read_text(encoding="utf-8")


def _fake_bundle(tmp_path: Path) -> Path:
    """Folder jak rozpakowana paczka PhoneBot-linux (zamiast prawdziwego programu krótki skrypt)."""
    src = tmp_path / "Pobrane" / "PhoneBot"
    (src / "_internal").mkdir(parents=True)
    (src / "PhoneBot").write_text("#!/bin/sh\necho PhoneBot\n")
    (src / "PhoneBot").chmod(0o755)
    for name in ("install.sh", "phonebot.desktop"):
        shutil.copy(PKG / name, src / name)
    shutil.copy(ROOT / "assets" / "phonebot.png", src / "phonebot.png")
    return src


@posix_bash
def test_install_script_per_user_and_uninstall_keeps_data(tmp_path):
    src = _fake_bundle(tmp_path)
    home = tmp_path / "dom z spacją"
    data = home / ".local" / "share" / "phonebot"
    data.mkdir(parents=True)
    (data / "phonebot.sqlite3").write_text("oferty")
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}

    run = subprocess.run(["bash", str(src / "install.sh"), "--autostart"], env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    dest = home / ".local" / "opt" / "PhoneBot"
    link = home / ".local" / "bin" / "phonebot"
    desktop = home / ".local" / "share" / "applications" / "phonebot.desktop"
    assert (dest / "PhoneBot").is_file() and link.is_symlink() and link.resolve() == (dest / "PhoneBot").resolve()
    assert f'Exec="{dest}/PhoneBot"' in desktop.read_text(encoding="utf-8")
    assert (home / ".config" / "autostart" / "phonebot.desktop").is_file()
    assert (home / ".local" / "share" / "icons" / "hicolor" / "256x256" / "apps" / "phonebot.png").is_file()
    assert subprocess.run([str(link)], capture_output=True, text=True).stdout.strip() == "PhoneBot"

    (src / "PhoneBot").write_text("#!/bin/sh\necho PhoneBot 2\n")  # aktualizacja: nowa wersja zastępuje starą
    assert subprocess.run(["bash", str(src / "install.sh")], env=env, capture_output=True).returncode == 0
    assert "PhoneBot 2" in (dest / "PhoneBot").read_text() and not (dest.parent / "PhoneBot.new").exists()

    run = subprocess.run(["bash", str(dest / "install.sh"), "--uninstall"], env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    assert not dest.exists() and not link.is_symlink() and not desktop.exists()
    assert not (home / ".config" / "autostart" / "phonebot.desktop").exists()
    assert (data / "phonebot.sqlite3").read_text() == "oferty"  # dane zostają


@posix_bash
def test_install_script_refuses_wrong_folder_and_unknown_option(tmp_path):
    env = {"HOME": str(tmp_path / "home"), "PATH": os.environ.get("PATH", "/usr/bin:/bin"), "LANG": "C.UTF-8"}
    alone = tmp_path / "sam_skrypt"
    alone.mkdir()
    shutil.copy(PKG / "install.sh", alone / "install.sh")
    run = subprocess.run(["bash", str(alone / "install.sh")], env=env, capture_output=True, text=True)
    assert run.returncode != 0 and "rozpakowanego folderu" in run.stderr
    run = subprocess.run(["bash", str(_fake_bundle(tmp_path) / "install.sh"), "--cos"], env=env,
                         capture_output=True, text=True)
    assert run.returncode != 0 and "nieznana opcja" in run.stderr
    assert not (tmp_path / "home" / ".local" / "opt" / "PhoneBot").exists()


@posix_bash
def test_install_script_names_missing_system_libraries(tmp_path):
    src = _fake_bundle(tmp_path)
    plugin = src / "_internal" / "PySide6" / "Qt" / "plugins" / "platforms" / "libqxcb.so"
    plugin.parent.mkdir(parents=True)
    plugin.write_bytes(b"")
    tools = tmp_path / "narzedzia"
    tools.mkdir()
    (tools / "ldd").write_text("#!/bin/sh\necho '\tlibxcb-cursor.so.0 => not found'\necho '\tlibc.so.6 => /lib/libc.so.6'\n")
    (tools / "ldd").chmod(0o755)
    env = {"HOME": str(tmp_path / "home"), "PATH": f"{tools}:{os.environ.get('PATH', '/usr/bin:/bin')}",
           "LANG": "C.UTF-8"}
    run = subprocess.run(["bash", str(src / "install.sh")], env=env, capture_output=True, text=True)
    assert run.returncode == 0, run.stderr  # instalacja się udaje, a brakujące biblioteki są wymienione z poleceniem
    assert "libxcb-cursor.so.0" in run.stdout and "sudo apt install libxcb-cursor0" in run.stdout
    assert "libc.so.6" not in run.stdout
