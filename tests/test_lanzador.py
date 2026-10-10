# -*- coding: utf-8 -*-
"""El lanzador y el empaquetado: lo del usuario no se toca nunca al actualizar."""
import importlib
import json
import sys
import zipfile

import pytest


@pytest.fixture
def lanzador(tmp_path, monkeypatch):
    monkeypatch.setenv("SURTIDOR_DIR", str(tmp_path / "app"))
    sys.modules.pop("surtidor", None)
    mod = importlib.import_module("surtidor")
    yield mod
    sys.modules.pop("surtidor", None)


def hacer_zip(ruta, ficheros):
    with zipfile.ZipFile(ruta, "w") as z:
        for nombre, contenido in ficheros.items():
            z.writestr(nombre, contenido)
    return ruta


def test_orden_de_versiones(lanzador):
    v = lanzador.version_tupla
    assert v("v2026.10.10") > v("v2026.10.9")
    assert v("v2026.10.10.2") > v("v2026.10.10")
    assert v("v2026.11.1") > v("v2026.10.31")
    assert v("dev") == ()


def test_actualizar_no_toca_lo_del_usuario_y_guarda_copia(lanzador, tmp_path):
    app = lanzador.APP
    app.mkdir(parents=True)
    (app / "config.json").write_text('{"tankerkoenig_clave": "MIA"}', encoding="utf-8")
    (app / "precios.json").write_text('{"count": 2697}', encoding="utf-8")
    (app / "servidor.py").write_text("# viejo", encoding="utf-8")
    (app / "VERSION").write_text("v1", encoding="utf-8")
    z = hacer_zip(tmp_path / "app.zip", {
        "servidor.py": "# nuevo", "config.json": '{"pisado": true}', "precios.json": "{}",
        "panel/admin.html": "<html>"})
    lanzador.instalar_zip(z, "v2")
    assert (app / "servidor.py").read_text(encoding="utf-8") == "# nuevo"
    assert json.loads((app / "config.json").read_text(encoding="utf-8")) == {"tankerkoenig_clave": "MIA"}
    assert (app / "precios.json").read_text(encoding="utf-8") == '{"count": 2697}'
    assert (app / "_anterior" / "servidor.py").read_text(encoding="utf-8") == "# viejo"
    assert (app / "VERSION").read_text(encoding="utf-8") == "v2"


def test_version_que_no_arranca_vuelve_atras(lanzador, tmp_path):
    app = lanzador.APP
    app.mkdir(parents=True)
    (app / "servidor.py").write_text("# bueno", encoding="utf-8")
    (app / "VERSION").write_text("v1", encoding="utf-8")
    lanzador.instalar_zip(hacer_zip(tmp_path / "a.zip", {"servidor.py": "# roto"}), "v2")
    assert lanzador.restaurar_anterior() is True
    assert (app / "servidor.py").read_text(encoding="utf-8") == "# bueno"
    assert lanzador.version_instalada() == "v1"
    assert (app / "VERSION_MALA").read_text(encoding="utf-8") == "v2"


def test_zip_que_intenta_salir_de_la_carpeta_se_rechaza(lanzador, tmp_path):
    lanzador.APP.mkdir(parents=True)
    with pytest.raises(ValueError):
        lanzador.instalar_zip(hacer_zip(tmp_path / "malo.zip", {"../fuera.txt": "x"}), "v9")
    assert not (tmp_path / "fuera.txt").exists()


def test_primera_vez_crea_config_con_clave(lanzador):
    lanzador.APP.mkdir(parents=True)
    (lanzador.APP / "config.example.json").write_text('{"recoger_cada_horas": 12}', encoding="utf-8")
    lanzador.config_minima()
    cfg = lanzador.leer_config()
    assert len(cfg["servidor_clave"]) >= 12
    assert cfg["servidor_puerto"] == 8766 and cfg["recoger_cada_horas"] == 12


# ---------------------------------------------------------------- empaquetar
def test_el_zip_lleva_la_app_y_la_semilla_pero_nada_del_usuario(tmp_path):
    import empaquetar
    destino = empaquetar.empaquetar("v2026.10.10-test", tmp_path / "app.zip")
    nombres = set(zipfile.ZipFile(destino).namelist())
    for imprescindible in ("servidor.py", "admin.py", "index.html", "panel/admin.html",
                           "collector/collect.py", "VERSION"):
        assert imprescindible in nombres
    assert "precios.semilla.json" in nombres
    for del_usuario in ("config.json", "precios.json", "historial.jsonl", "precios.anterior.json"):
        assert del_usuario not in nombres
    assert zipfile.ZipFile(destino).read("VERSION").decode().strip() == "v2026.10.10-test"
