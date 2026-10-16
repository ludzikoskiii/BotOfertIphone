# -*- mode: python ; coding: utf-8 -*-
# Budowanie:  pyinstaller phonebot.spec
#   Windows → dist/PhoneBot.exe (jeden plik)
#   Linux   → dist/PhoneBot/ (folder: program PhoneBot + biblioteki; start bez rozpakowywania przy każdym uruchomieniu)
import sys

from PyInstaller.utils.hooks import collect_submodules

LINUX = sys.platform.startswith("linux")

hiddenimports = collect_submodules("phonebot.sources") + collect_submodules("phonebot.web") + ["selectolax.parser"]
# lokalne AI: model tytułów zapisany przez joblib odwołuje się do tych klas przy wczytywaniu
hiddenimports += ["sklearn.pipeline", "sklearn.feature_extraction.text", "sklearn.linear_model._logistic",
                  "phonebot.ml.text_model", "onnxruntime", "PIL.JpegImagePlugin", "PIL.PngImagePlugin",
                  "PIL.WebPImagePlugin"]
if LINUX:  # sekrety w pęku kluczy systemu (Secret Service przez D-Bus)
    hiddenimports += collect_submodules("secretstorage") + collect_submodules("jeepney")
excludes = ["tkinter", "matplotlib", "pandas", "torch", "IPython", "pytest", "PySide6.QtWebEngineCore",
            "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore", "PySide6.QtQuick", "PySide6.QtQml",
            "PySide6.QtMultimedia", "PySide6.QtCharts", "PySide6.QtDataVisualization", "PySide6.QtPdf"]
if not LINUX:
    excludes.append("cryptography")  # potrzebna tylko do pęku kluczy na Linux

a = Analysis(
    ["run_phonebot.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
)
if LINUX:
    # Bez motywu GTK3 dla Qt (okno i tak ma styl Fusion) i bez całego GTK, które ciągnie: stara kopia GTK z paczki
    # wczytuje moduły, motywy i metody wprowadzania z nowszego systemu, co potrafi wywrócić start programu.
    # libstdc++ / libgcc_s bierzemy z systemu: sterowniki grafiki (Mesa) wymagają wersji co najmniej tak nowej
    # jak systemowa, a paczka budowana na Ubuntu 22.04 miałaby starszą.
    _SYSTEM_ONLY = ("libstdc++.so", "libgcc_s.so", "libgtk-3.so", "libgdk-3.so", "libgdk_pixbuf-2.0.so",
                    "libatk-1.0.so", "libatk-bridge-2.0.so", "libatspi.so", "libcairo.so", "libcairo-gobject.so",
                    "libpango-1.0.so", "libpangocairo-1.0.so", "libpangoft2-1.0.so", "libepoxy.so", "libthai.so",
                    "libdatrie.so", "libfribidi.so", "libgraphite2.so", "libpixman-1.so")
    a.binaries = [b for b in a.binaries
                  if "platformthemes/libqgtk3" not in b[0].replace("\\", "/")
                  and not b[0].rsplit("/", 1)[-1].startswith(_SYSTEM_ONLY)]
pyz = PYZ(a.pure)
if LINUX:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="PhoneBot",
        debug=False,
        strip=False,
        upx=False,
        console=False,
    )
    coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name="PhoneBot")
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name="PhoneBot",
        debug=False,
        strip=False,
        upx=False,
        console=False,
        icon="assets/phonebot.ico" if sys.platform == "win32" else None,
    )
