# -*- coding: utf-8 -*-
"""El recolector: lo basico que, si se rompe, deja el mapa sin precios."""
import importlib.util
import json
from datetime import date

import pytest

from conftest import RAIZ


@pytest.fixture
def collect(monkeypatch):
    monkeypatch.delenv("RECOLECTOR_TOPE_MINUTOS", raising=False)
    spec = importlib.util.spec_from_file_location("collect", RAIZ / "collector" / "collect.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_la_lista_de_cadenas_existe(collect):
    # el 29 de septiembre una reescritura la borro y la Action fallo con NameError
    assert collect.CADENAS and collect.CADENAS[0]["marca"] == "Prijzenindex"
    assert len(collect.PI_FUENTES) == 5


def test_tope_de_tiempo_configurable(monkeypatch):
    monkeypatch.setenv("RECOLECTOR_TOPE_MINUTOS", "35")
    spec = importlib.util.spec_from_file_location("collect35", RAIZ / "collector" / "collect.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.TOPE_MINUTOS == 35


def test_glp_barato_no_se_descarta(collect):
    assert collect.a_float("0.759", "glp") == 0.759
    assert collect.a_float("0.759", "diesel") is None


def test_la_misma_estacion_en_dos_listados_se_funde(collect):
    a = collect.normaliza({"lat": 52.1, "lon": 5.1, "prices": {"diesel": 2.1}, "name": "Gasolinera",
                           "brand": "Gasolinera", "country": "NL", "source": "Prijzenindex"})
    b = collect.normaliza({"lat": 52.10001, "lon": 5.1, "prices": {"e10": 2.2}, "name": "Tinq",
                           "brand": "Tinq", "country": "NL", "source": "Prijzenindex"})
    out = collect.fusiona([a, b], "2026-10-10")
    assert len(out) == 1
    assert out[0]["prices"] == {"diesel": 2.1, "e10": 2.2} and out[0]["name"] == "Tinq"


def test_escribe_de_golpe(collect, tmp_path, monkeypatch):
    """main() escribe a un temporal y lo cambia: nunca deja precios.json a medias."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(collect, "CADENAS", [])
    monkeypatch.setattr(collect, "descubrir_marcas", lambda: None)
    (tmp_path / "precios.json").write_text(json.dumps({"stations": [
        {"lat": 52.1, "lon": 5.1, "price": 2.0, "name": "X", "brand": "X", "country": "NL",
         "source": "Prijzenindex", "date": date.today().isoformat()}]}), encoding="utf-8")
    assert collect.main() == 0
    assert not (tmp_path / "precios.json.tmp").exists()
    assert json.loads((tmp_path / "precios.json").read_text(encoding="utf-8"))["count"] == 1


def test_sin_tiempo_se_saltan_las_cadenas_pero_se_guarda(collect, tmp_path, monkeypatch):
    """Con el presupuesto agotado no se visita ninguna cadena mas, y aun asi se escribe precios.json."""
    import time
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(collect, "ARRANQUE", time.monotonic() - 10 ** 6)
    monkeypatch.setattr(collect, "descubrir_marcas", lambda: None)
    visitadas = []
    monkeypatch.setattr(collect, "procesar", lambda c: visitadas.append(c["marca"]) or [])
    monkeypatch.setattr(collect, "CADENAS", [{"marca": "Tango", "pais": "NL", "base": "https://x"}])
    assert collect.main() == 0
    assert visitadas == []
    assert (tmp_path / "precios.json").exists()
