# -*- coding: utf-8 -*-
"""Cada prueba del servidor trabaja en una carpeta de app temporal, nunca en la del repositorio."""
import importlib
import json
import shutil
import sys
from pathlib import Path

import pytest

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))


def estaciones(n=10, fecha="2026-10-10", pais="NL"):
    return [{"id": f"e{i}", "lat": 52 + i / 100, "lon": 5 + i / 100, "price": 2.0 + i / 1000,
             "prices": {"diesel": 2.0 + i / 1000, "e10": 2.1}, "country": pais if i % 3 else "BE",
             "date": fecha, "name": "Prueba", "brand": "Prueba", "source": "Prijzenindex"} for i in range(n)]


def doc(n=10, updated="2026-10-10T05:00:00+00:00", fecha="2026-10-10"):
    return {"updated": updated, "count": n, "fuels": ["diesel", "e10"], "cursor": {"/brandstof/diesel": 3},
            "stations": estaciones(n, fecha)}


@pytest.fixture
def app_dir(tmp_path, monkeypatch):
    """Carpeta de app con lo minimo para arrancar el servidor."""
    d = tmp_path / "app"
    (d / "panel").mkdir(parents=True)
    shutil.copy2(RAIZ / "index.html", d / "index.html")
    shutil.copy2(RAIZ / "panel" / "admin.html", d / "panel" / "admin.html")
    (d / "VERSION").write_text("v2026.10.10-test", encoding="utf-8")
    (d / "config.json").write_text(json.dumps({
        "servidor_clave": "clave-de-prueba-larga", "tankerkoenig_clave": "TK-SECRETA",
        "semilla_github": False, "recoger_cada_horas": 0}), encoding="utf-8")
    (d / "precios.json").write_text(json.dumps(doc(10)), encoding="utf-8")
    monkeypatch.setenv("SURTIDOR_DIR", str(d))
    monkeypatch.setenv("SURTIDOR_TOKEN_LOCAL", "token-local-de-prueba")
    monkeypatch.delenv("SURTIDOR_EXE", raising=False)
    monkeypatch.delenv("SURTIDOR_RECOLECTOR_CMD", raising=False)
    return d


@pytest.fixture
def servidor(app_dir):
    """El modulo servidor recien importado contra la carpeta temporal (admin incluido)."""
    for m in ("servidor", "admin"):
        sys.modules.pop(m, None)
    mod = importlib.import_module("servidor")
    import admin
    admin.SESIONES.clear()
    yield mod
    if mod.RECOLECTOR.en_marcha:
        mod.RECOLECTOR.parar()
    for m in ("servidor", "admin"):
        sys.modules.pop(m, None)
