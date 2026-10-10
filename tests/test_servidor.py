# -*- coding: utf-8 -*-
"""El servidor local: mapa, entrada al panel, ajustes, recogida y copias de seguridad."""
import json
import sys
import time

from fastapi.testclient import TestClient

from conftest import doc


def cliente(servidor, local=True):
    return TestClient(servidor.app, client=("127.0.0.1" if local else "192.168.1.40", 50000))


def entrar(c):
    r = c.post("/api/admin/entrar", json={"clave": "clave-de-prueba-larga"})
    assert r.status_code == 200
    return c


def esperar_fin(servidor, tope=20):
    t = time.time()
    while servidor.RECOLECTOR.en_marcha and time.time() - t < tope:
        time.sleep(0.1)
    # el hilo que vigila apunta el historial justo despues de que el proceso acabe
    while not servidor.historial(1) and time.time() - t < tope:
        time.sleep(0.05)
    time.sleep(0.2)


def recolector_falso(servidor, tmp_path, monkeypatch, codigo):
    """Un recolector de mentira: con el codigo de dentro de la cadena."""
    script = tmp_path / "falso.py"
    script.write_text(codigo, encoding="utf-8")
    monkeypatch.setenv("SURTIDOR_RECOLECTOR_CMD", json.dumps([sys.executable, str(script)]))


# ---------------------------------------------------------------- mapa
def test_mapa_lleva_las_claves_solo_para_este_pc(servidor):
    html = cliente(servidor).get("/").text
    assert '"tk": "TK-SECRETA"' in html and '"local": true' in html
    de_fuera = cliente(servidor, local=False).get("/").text
    assert "TK-SECRETA" not in de_fuera and '"local": true' in de_fuera


def test_mapa_no_se_rompe_con_una_clave_rara(servidor, app_dir):
    cfg = json.loads((app_dir / "config.json").read_text(encoding="utf-8"))
    cfg["tankerkoenig_clave"] = "</script><script>alert(1)</script>"
    (app_dir / "config.json").write_text(json.dumps(cfg), encoding="utf-8")
    html = cliente(servidor).get("/").text
    assert "</script><script>alert(1)" not in html


def test_precios_sale_lo_de_este_pc(servidor):
    r = cliente(servidor).get("/precios.json")
    assert r.status_code == 200 and r.json()["count"] == 10
    assert r.headers["cache-control"] == "no-store"


# ---------------------------------------------------------------- entrar
def test_el_panel_pide_entrar(servidor):
    c = cliente(servidor)
    assert c.get("/api/admin/resumen").status_code == 401
    assert c.post("/api/admin/entrar", json={"clave": "mala"}).status_code == 401
    entrar(c)
    assert c.get("/api/admin/resumen").status_code == 200


def test_token_local_abre_sesion_y_uno_malo_no(servidor):
    c = cliente(servidor)
    c.get("/local?t=otro&a=/admin")
    assert c.get("/api/admin/resumen").status_code == 401
    r = c.get("/local?t=token-local-de-prueba&a=/admin", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/admin"
    assert c.get("/api/admin/resumen").status_code == 200


def test_token_local_no_redirige_fuera(servidor):
    r = cliente(servidor).get("/local?t=token-local-de-prueba&a=https://otro.sitio", follow_redirects=False)
    assert r.headers["location"] == "/"


# ---------------------------------------------------------------- resumen
def test_resumen_cuenta_por_pais_y_carburante(servidor):
    r = entrar(cliente(servidor)).get("/api/admin/resumen").json()
    d = r["datos"]
    assert d["estaciones"] == 10
    assert d["por_carburante"] == {"diesel": 10, "e10": 10}
    assert sum(d["por_pais"].values()) == 10
    assert d["cruce"]["BE|diesel"] == d["por_pais"]["BE"]
    assert r["version"] == "v2026.10.10-test"


# ---------------------------------------------------------------- ajustes
def test_las_claves_nunca_vuelven_al_panel(servidor):
    a = entrar(cliente(servidor)).get("/api/admin/ajustes").json()
    assert a["tankerkoenig_clave"] == {"puesta": True}
    assert a["servidor_clave"] == {"puesta": True}
    assert "TK-SECRETA" not in json.dumps(a)


def test_clave_vacia_no_la_borra_y_borrar_si(servidor, app_dir):
    c = entrar(cliente(servidor))
    c.post("/api/admin/ajustes", json={"tankerkoenig_clave": "", "recoger_cada_horas": 6})
    cfg = json.loads((app_dir / "config.json").read_text(encoding="utf-8"))
    assert cfg["tankerkoenig_clave"] == "TK-SECRETA" and cfg["recoger_cada_horas"] == 6
    c.post("/api/admin/ajustes", json={"tankerkoenig_clave": {"borrar": True}})
    cfg = json.loads((app_dir / "config.json").read_text(encoding="utf-8"))
    assert cfg["tankerkoenig_clave"] == ""


def test_ajuste_fuera_de_rango_no_guarda_nada(servidor, app_dir):
    antes = (app_dir / "config.json").read_text(encoding="utf-8")
    r = entrar(cliente(servidor)).post("/api/admin/ajustes", json={"recoger_cada_horas": 3, "servidor_puerto": 80})
    assert r.status_code == 400
    assert (app_dir / "config.json").read_text(encoding="utf-8") == antes


def test_cambiar_puerto_pide_reinicio(servidor):
    r = entrar(cliente(servidor)).post("/api/admin/ajustes", json={"servidor_puerto": 8777}).json()
    assert r["reiniciar"] is True


def test_la_clave_solo_se_ensena_en_este_pc(servidor):
    assert entrar(cliente(servidor)).get("/api/admin/clave").json()["clave"] == "clave-de-prueba-larga"
    assert entrar(cliente(servidor, local=False)).get("/api/admin/clave").status_code == 403


def test_clave_corta_no_vale(servidor):
    r = entrar(cliente(servidor)).post("/api/admin/cambiar-clave", json={"nueva": "corta"})
    assert r.status_code == 400


# ---------------------------------------------------------------- recogida
BUENA = """
import json
d = json.load(open("precios.json", encoding="utf-8"))
d["updated"] = "2026-10-10T09:00:00+00:00"
d["stations"].append(dict(d["stations"][0], id="nueva", lat=53.0))
json.dump(d, open("precios.json", "w", encoding="utf-8"))
print("=== RESUMEN prueba ===")
"""
ROTA = """
open("precios.json", "w").write("{roto")
raise SystemExit(1)
"""


def test_recogida_buena_guarda_copia_y_apunta_historial(servidor, app_dir, tmp_path, monkeypatch):
    recolector_falso(servidor, tmp_path, monkeypatch, BUENA)
    c = entrar(cliente(servidor))
    assert c.post("/api/admin/recoger").status_code == 200
    esperar_fin(servidor)
    assert json.loads((app_dir / "precios.json").read_text(encoding="utf-8"))["updated"].startswith("2026-10-10T09")
    assert json.loads((app_dir / "precios.anterior.json").read_text(encoding="utf-8"))["updated"].startswith("2026-10-10T05")
    assert not (app_dir / "precios.antes.json").exists()
    p = c.get("/api/admin/historial").json()["pasadas"][0]
    assert p["resultado"] == "bien" and p["estaciones"] == 11


def test_recogida_que_rompe_el_fichero_se_deshace_sola(servidor, app_dir, tmp_path, monkeypatch):
    recolector_falso(servidor, tmp_path, monkeypatch, ROTA)
    c = entrar(cliente(servidor))
    c.post("/api/admin/recoger")
    esperar_fin(servidor)
    assert json.loads((app_dir / "precios.json").read_text(encoding="utf-8"))["count"] == 10
    assert c.get("/api/admin/historial").json()["pasadas"][0]["resultado"] == "error"
    # una pasada mala no convierte nada en "la anterior"
    assert not (app_dir / "precios.anterior.json").exists()


def test_parar_no_borra_el_camino_a_la_ultima_buena(servidor, app_dir, tmp_path, monkeypatch):
    recolector_falso(servidor, tmp_path, monkeypatch, BUENA)
    c = entrar(cliente(servidor))
    c.post("/api/admin/recoger")
    esperar_fin(servidor)
    recolector_falso(servidor, tmp_path, monkeypatch, "import time\ntime.sleep(30)\n")
    c.post("/api/admin/recoger")
    time.sleep(0.5)
    assert c.post("/api/admin/recoger/parar").status_code == 200
    t = time.time()
    while len(servidor.historial(5)) < 2 and time.time() - t < 15:
        time.sleep(0.1)
    # deshacer vuelve a lo de antes de la pasada BUENA, no a la parada
    assert c.post("/api/admin/deshacer").status_code == 200
    assert json.loads((app_dir / "precios.json").read_text(encoding="utf-8"))["updated"].startswith("2026-10-10T05")


def test_no_se_lanzan_dos_a_la_vez(servidor, tmp_path, monkeypatch):
    recolector_falso(servidor, tmp_path, monkeypatch, "import time\ntime.sleep(5)\n")
    c = entrar(cliente(servidor))
    assert c.post("/api/admin/recoger").status_code == 200
    assert c.post("/api/admin/recoger").status_code == 409


# ---------------------------------------------------------------- arranque
def test_primer_arranque_usa_la_semilla(servidor, app_dir):
    (app_dir / "precios.json").unlink()
    (app_dir / "precios.semilla.json").write_text(json.dumps(doc(7)), encoding="utf-8")
    servidor.semilla()
    assert json.loads((app_dir / "precios.json").read_text(encoding="utf-8"))["count"] == 7


def test_pasada_cortada_por_apagon_se_recupera(servidor, app_dir):
    (app_dir / "precios.antes.json").write_text(json.dumps(doc(9)), encoding="utf-8")
    (app_dir / "precios.json").write_text("{a medias", encoding="utf-8")
    servidor.semilla()
    assert json.loads((app_dir / "precios.json").read_text(encoding="utf-8"))["count"] == 9
    assert not (app_dir / "precios.antes.json").exists()


def test_programador_respeta_solo_a_mano(servidor):
    assert servidor.proxima_recogida() is None          # recoger_cada_horas = 0 en la prueba
