# -*- mode: python ; coding: utf-8 -*-
# Budowanie:  pyinstaller phonebot.spec   →  dist/PhoneBot.exe
import sys

from PyInstaller.utils.hooks import collect_submodules

hiddenimports = collect_submodules("phonebot.sources") + ["anthropic", "httpx2", "selectolax.parser"]
# lokalne AI: model tytułów zapisany przez joblib odwołuje się do tych klas przy wczytywaniu
hiddenimports += ["sklearn.pipeline", "sklearn.feature_extraction.text", "sklearn.linear_model._logistic",
                  "phonebot.ml.text_model", "onnxruntime", "PIL.JpegImagePlugin", "PIL.PngImagePlugin",
                  "PIL.WebPImagePlugin"]

a = Analysis(
    ["run_phonebot.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=hiddenimports,
    excludes=["tkinter", "cryptography", "matplotlib", "pandas", "torch", "IPython", "pytest", "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets", "PySide6.Qt3DCore",
              "PySide6.QtQuick", "PySide6.QtQml", "PySide6.QtMultimedia", "PySide6.QtCharts",
              "PySide6.QtDataVisualization", "PySide6.QtPdf"],
    noarchive=False,
)
pyz = PYZ(a.pure)
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
