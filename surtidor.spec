# -*- mode: python ; coding: utf-8 -*-
# PyInstaller: un solo Surtidor.exe con Python, las librerias y una copia del zip de la app por si
# la primera vez no hay red. Lo compila GitHub Actions en Windows (release.yml); a mano:
#     pip install -r requirements.txt pyinstaller
#     python empaquetar.py vX && pyinstaller surtidor.spec
import os

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [("dist/surtidor-app.zip", ".")], [], []
# webview: la ventana de escritorio (pywebview, con WebView2 de Windows por pythonnet)
for paquete in ("webview",):
    d, b, h = collect_all(paquete)
    datas += d; binaries += b; hiddenimports += h
# el servidor y el recolector se ejecutan con runpy desde la carpeta de la app: PyInstaller no ve sus
# imports al analizar surtidor.py, asi que van aqui a mano
hiddenimports += collect_submodules("uvicorn") + collect_submodules("fastapi") + collect_submodules("starlette") \
    + ["html", "http.cookies", "urllib.robotparser", "concurrent.futures", "zoneinfo",
       "tkinter", "tkinter.ttk", "tkinter.scrolledtext", "tkinter.messagebox", "clr", "clr_loader"]

a = Analysis(["surtidor.py"], pathex=["."], binaries=binaries, datas=datas, hiddenimports=hiddenimports,
             hookspath=[], excludes=["matplotlib", "scipy", "pandas", "numpy", "IPython", "pytest"], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name="Surtidor", console=False, upx=False,
          icon="surtidor.ico" if os.path.exists("surtidor.ico") else None)
