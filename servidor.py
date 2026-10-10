# -*- coding: utf-8 -*-
"""
servidor.py - El servidor local de Surtidor: el mapa, los precios y la recogida programada.

Lo arranca Surtidor.exe (surtidor.py --servidor) desde la carpeta de la app. Hace tres cosas:

    mapa                 /            el index.html de siempre, con los ajustes locales inyectados
                                      (claves de Tankerkonig y Open Charge Map, sin ponerlas en la direccion)
    precios              /precios.json lo que ha recogido este PC, no la copia del repositorio
    recogida             cada N horas lanza collector/collect.py en segundo plano y guarda el
                         resultado; si sale mal, se queda con lo anterior

La administracion (panel, ajustes, registro, reinicio) esta en admin.py y se monta aqui.

Donde vive cada cosa (todo en la carpeta de la app, SURTIDOR_DIR si se quiere otra):
    config.json          ajustes y claves; nunca va al repositorio
    precios.json         lo recogido; se acumula entre pasadas (lo hace collect.py)
    precios.anterior.json la copia de antes de la ultima pasada, por si sale mal
    historial.jsonl      una linea por pasada: cuando, cuanto tardo, como acabo

Desarrollo:  python servidor.py   (en http://127.0.0.1:8766)
"""
import collections
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

BASE_DIR = Path(os.environ.get("SURTIDOR_DIR") or Path(__file__).resolve().parent)
CONFIG_PATH = BASE_DIR / "config.json"
PRECIOS = BASE_DIR / "precios.json"
PRECIOS_ANTERIOR = BASE_DIR / "precios.anterior.json"  # lo de antes del ultimo cambio bueno: lo que recupera Deshacer
PRECIOS_EN_CURSO = BASE_DIR / "precios.antes.json"     # copia de la pasada en marcha; solo vive mientras dura
PRECIOS_SEMILLA = BASE_DIR / "precios.semilla.json"   # viaja en el zip de la app, para el primer arranque sin red
HISTORIAL = BASE_DIR / "historial.jsonl"
CODIGO_REINICIO = 75          # el lanzador vuelve a arrancar el servidor si sale con este codigo

DEFECTO = {
    "servidor_puerto": 8766,          # 8765 es el del Catalogator: pueden convivir en el mismo PC
    "servidor_escuchar": "127.0.0.1", # 0.0.0.0 para verlo desde otros equipos de la red de casa
    "servidor_clave": "",
    "recoger_cada_horas": 12,         # 0 = solo a mano, desde el panel
    "recoger_al_arrancar": True,      # si al abrir los datos son mas viejos que lo de arriba
    "recoger_tope_minutos": 150,
    "semilla_github": True,           # el primer arranque se trae del repositorio lo ya recogido
    "repo": "malevolisco/adrigasolineras",
    "tankerkoenig_clave": "",
    "opencharge_clave": "",
}
# lo que nunca sale del servidor hacia el panel: solo se dice si esta puesto o no
SECRETOS = ("servidor_clave", "tankerkoenig_clave", "opencharge_clave")


# ====================================================================== ajustes
_CERROJO_CFG = threading.Lock()


def leer_config():
    try:
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(cfg, dict):
            cfg = {}
    except (OSError, ValueError):
        cfg = {}
    return {**DEFECTO, **cfg}


def guardar_config(cfg):
    with _CERROJO_CFG:
        tmp = CONFIG_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(CONFIG_PATH)


def asegurar_clave():
    """La clave del panel. Si no hay, se crea una; el lanzador entra sin teclearla (token local)."""
    cfg = leer_config()
    if len(str(cfg.get("servidor_clave") or "")) < 12:
        cfg["servidor_clave"] = secrets.token_urlsafe(12)
        guardar_config(cfg)
    return cfg


# ====================================================================== registro en vivo
REGISTRO = collections.deque(maxlen=4000)
_CONTADOR = [0]
_CERROJO_REG = threading.Lock()
NIVEL_ERROR = re.compile(r"\b(error|traceback|exception|no se ha podido|fallo|falla|http 4\d\d|http 5\d\d)\b", re.I)
NIVEL_AVISO = re.compile(r"\b(aviso|tope|sin conexion|sin red|caducad|vacio|omitid|parad[ao])\b", re.I)


def log(texto, origen="servidor"):
    """Todo lo que pasa, para la consola del lanzador y para la pestaña Registro del panel."""
    texto = str(texto).rstrip()
    if not texto:
        return
    if NIVEL_ERROR.search(texto):
        nivel = "error"
    elif NIVEL_AVISO.search(texto):
        nivel = "aviso"
    else:
        nivel = "info"
    with _CERROJO_REG:
        _CONTADOR[0] += 1
        REGISTRO.append({"n": _CONTADOR[0], "t": datetime.now().strftime("%d/%m %H:%M:%S"),
                         "origen": origen, "nivel": nivel, "texto": texto})
    try:
        print(f"[{origen}] {texto}", flush=True)
    except Exception:
        pass


def registro_desde(n):
    with _CERROJO_REG:
        return [r for r in REGISTRO if r["n"] > n], _CONTADOR[0]


# ====================================================================== precios
def leer_precios(ruta=PRECIOS):
    try:
        d = json.loads(Path(ruta).read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else None
    except (OSError, ValueError):
        return None


def fecha_iso(texto):
    try:
        f = datetime.fromisoformat(str(texto).replace("Z", "+00:00"))
        return f if f.tzinfo else f.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def edad_horas(d):
    f = fecha_iso((d or {}).get("updated"))
    if not f:
        return None
    return round((datetime.now(timezone.utc) - f).total_seconds() / 3600, 1)


def cobertura(d):
    """Lo que lleva precios.json, contado: por pais, por carburante y por pais y carburante."""
    st = (d or {}).get("stations") or []
    hoy = max((e.get("date") or "" for e in st), default="")
    por_pais, por_comb, cruce = collections.Counter(), collections.Counter(), collections.Counter()
    for e in st:
        pais = e.get("country") or "?"
        por_pais[pais] += 1
        precios = e.get("prices") or ({"diesel": e["price"]} if e.get("price") is not None else {})
        for c in precios:
            por_comb[c] += 1
            cruce[f"{pais}|{c}"] += 1
    return {
        "estaciones": len(st),
        "actualizado": (d or {}).get("updated"),
        "edad_horas": edad_horas(d),
        "ultimo_dia": hoy,
        "actualizadas_ultimo_dia": sum(1 for e in st if (e.get("date") or "") == hoy) if hoy else 0,
        "por_pais": dict(por_pais),
        "por_carburante": dict(por_comb),
        "cruce": dict(cruce),
        "cursor": (d or {}).get("cursor") or {},
        "etiquetas": (d or {}).get("labels") or {},
    }


def valido(d):
    return isinstance(d, dict) and isinstance(d.get("stations"), list) and len(d["stations"]) > 0


# ====================================================================== historial de pasadas
def apuntar(entrada):
    try:
        with HISTORIAL.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entrada, ensure_ascii=False) + "\n")
    except OSError as e:
        log(f"No se ha podido escribir el historial: {e}")


def historial(n=50):
    try:
        lineas = HISTORIAL.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    salida = []
    for l in lineas[-n:]:
        try:
            salida.append(json.loads(l))
        except ValueError:
            pass
    return list(reversed(salida))


# ====================================================================== la recogida
class Recolector:
    """Lanza collect.py como proceso aparte (con el Python de dentro del exe) y vigila como acaba.

    Antes de empezar guarda una copia de precios.json. Si el proceso falla, se para o deja un fichero que
    no se puede leer, se restaura esa copia: una pasada mala nunca deja el mapa sin datos. Solo si la
    pasada sale bien la copia pasa a ser "la anterior" (la que recupera Deshacer); asi una pasada parada
    o fallida no borra el camino de vuelta a la ultima buena."""

    def __init__(self):
        self.proceso = None
        self.inicio = None
        self.fase = ""
        self.origen = ""
        self.parado_a_mano = False
        self.ultimo_intento = None
        self.no_antes_de = None       # con "recoger al arrancar" apagado, la primera espera un ciclo entero
        self._cerrojo = threading.Lock()

    @property
    def en_marcha(self):
        return self.proceso is not None and self.proceso.poll() is None

    def orden(self):
        a_medida = os.environ.get("SURTIDOR_RECOLECTOR_CMD")     # pruebas
        if a_medida:
            return json.loads(a_medida)
        exe = os.environ.get("SURTIDOR_EXE")
        if exe:                                                   # dentro de Surtidor.exe
            return [exe, "--recolector"]
        return [sys.executable, "-u", str(BASE_DIR / "collector" / "collect.py")]

    def lanzar(self, origen="manual"):
        with self._cerrojo:
            if self.en_marcha:
                return False, "Ya hay una recogida en marcha"
            cfg = leer_config()
            PRECIOS_EN_CURSO.unlink(missing_ok=True)
            if PRECIOS.exists():
                try:
                    shutil.copy2(PRECIOS, PRECIOS_EN_CURSO)
                except OSError as e:
                    log(f"No se ha podido copiar precios.json antes de recoger: {e}")
            env = dict(os.environ, PYTHONUTF8="1", PYTHONUNBUFFERED="1",
                       RECOLECTOR_TOPE_MINUTOS=str(int(cfg.get("recoger_tope_minutos") or 150)))
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
            try:
                self.proceso = subprocess.Popen(self.orden(), cwd=str(BASE_DIR), stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                                                errors="replace", env=env, creationflags=flags)
            except OSError as e:
                log(f"No se ha podido lanzar el recolector: {e}")
                return False, f"No se ha podido lanzar el recolector: {e}"
            self.inicio = datetime.now(timezone.utc)
            self.ultimo_intento = self.inicio
            self.fase = "Arrancando"
            self.origen = origen
            self.parado_a_mano = False
            log(f"Recogida en marcha ({origen})")
            threading.Thread(target=self._vigilar, args=(self.proceso,), daemon=True).start()
            return True, "Recogida en marcha"

    def parar(self):
        if not self.en_marcha:
            return False
        self.parado_a_mano = True
        matar(self.proceso)
        return True

    # Las lineas del recolector que dicen por donde va, para la tarjeta de estado del panel
    _FASES = (
        (re.compile(r"^--- (.+?) \((BE|NL|BE/NL)\) ---$"), lambda m: f"Cadena {m.group(1)}"),
        (re.compile(r"^--- (.+?) en (BE|NL) "), lambda m: f"Índice de {m.group(1)} · {m.group(2)}"),
        (re.compile(r"^vuelta (\d+):"), lambda m: f"Barriendo ciudades · vuelta {m.group(1)}"),
        (re.compile(r"^=== RESUMEN"), lambda m: "Guardando"),
    )

    def _vigilar(self, proceso):
        for linea in proceso.stdout:
            limpia = linea.strip()
            if not limpia:
                continue
            log(limpia, "recolector")
            for patron, texto in self._FASES:
                m = patron.search(limpia)
                if m:
                    self.fase = texto(m)
                    break
        codigo = proceso.wait()
        fin = datetime.now(timezone.utc)
        d = leer_precios()
        if self.parado_a_mano:
            resultado = "parada"
        elif codigo != 0:
            resultado = "error"
        elif not valido(d):
            resultado = "vacia"
        else:
            resultado = "bien"
        try:
            if resultado == "bien":
                if PRECIOS_EN_CURSO.exists():
                    PRECIOS_EN_CURSO.replace(PRECIOS_ANTERIOR)
            elif PRECIOS_EN_CURSO.exists():
                PRECIOS_EN_CURSO.replace(PRECIOS)
                log(f"Recogida {resultado}: se recupera lo que habia antes de empezar")
        except OSError as e:
            log(f"No se ha podido mover la copia de seguridad: {e}")
        d = leer_precios()
        cob = cobertura(d)
        apuntar({
            "inicio": self.inicio.isoformat(timespec="seconds"),
            "fin": fin.isoformat(timespec="seconds"),
            "minutos": round((fin - self.inicio).total_seconds() / 60, 1),
            "origen": self.origen,
            "resultado": resultado,
            "codigo": codigo,
            "estaciones": cob["estaciones"],
            "actualizadas": cob["actualizadas_ultimo_dia"] if resultado == "bien" else 0,
        })
        log(f"Recogida terminada: {resultado} · {cob['estaciones']} estaciones · "
            f"{round((fin - self.inicio).total_seconds() / 60, 1)} min")
        self.fase = ""

    def estado(self):
        return {
            "en_marcha": self.en_marcha,
            "inicio": self.inicio.isoformat(timespec="seconds") if self.en_marcha and self.inicio else None,
            "minutos": round((datetime.now(timezone.utc) - self.inicio).total_seconds() / 60, 1)
                       if self.en_marcha and self.inicio else None,
            "fase": self.fase if self.en_marcha else "",
            "origen": self.origen if self.en_marcha else "",
        }


RECOLECTOR = Recolector()


def matar(proceso):
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proceso.pid)], capture_output=True, timeout=15)
        else:
            proceso.terminate()
        proceso.wait(timeout=10)
    except Exception:
        try:
            proceso.kill()
        except Exception:
            pass


def proxima_recogida():
    """Cuando toca la siguiente pasada automatica (UTC), o None si esta solo a mano."""
    cfg = leer_config()
    try:
        horas = float(cfg.get("recoger_cada_horas") or 0)
    except (TypeError, ValueError):
        horas = 0
    if horas <= 0:
        return None
    base = fecha_iso((leer_precios() or {}).get("updated"))
    ahora = datetime.now(timezone.utc)
    if base is None:
        return ahora
    toca = base + timedelta(hours=horas)
    # tras un intento fallido no se reintenta enseguida: una hora de respiro
    if RECOLECTOR.ultimo_intento and toca <= ahora:
        toca = max(toca, RECOLECTOR.ultimo_intento + timedelta(hours=1))
    if RECOLECTOR.no_antes_de:
        toca = max(toca, RECOLECTOR.no_antes_de)
    return toca


def programador():
    """Cada minuto mira si toca recoger. Arranca tras un margen para que la semilla llegue antes."""
    time.sleep(20)
    primera = True
    while True:
        try:
            cfg = leer_config()
            toca = proxima_recogida()
            if toca is not None and not RECOLECTOR.en_marcha:
                if primera and not cfg.get("recoger_al_arrancar", True):
                    horas = float(cfg.get("recoger_cada_horas") or 0)
                    RECOLECTOR.no_antes_de = datetime.now(timezone.utc) + timedelta(hours=horas)
                elif datetime.now(timezone.utc) >= toca:
                    RECOLECTOR.lanzar("programada")
            primera = False
        except Exception as e:
            log(f"Error en el programador: {type(e).__name__}: {e}")
        time.sleep(60)


# ====================================================================== semilla desde GitHub
def traer_de_github(forzar=False):
    """Se trae del repositorio lo ya recogido si es mas nuevo que lo de este PC. Devuelve un texto."""
    cfg = leer_config()
    if RECOLECTOR.en_marcha:
        return "Hay una recogida en marcha: espera a que termine"
    url = f"https://raw.githubusercontent.com/{cfg.get('repo') or DEFECTO['repo']}/main/precios.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Surtidor"})
        with urllib.request.urlopen(req, timeout=60) as r:
            datos = r.read()
        remoto = json.loads(datos.decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as e:
        log(f"No se ha podido traer precios.json del repositorio: {type(e).__name__}")
        return "Sin conexion con GitHub"
    if not valido(remoto):
        return "El fichero del repositorio esta vacio o no se puede leer"
    local = leer_precios()
    f_rem, f_loc = fecha_iso(remoto.get("updated")), fecha_iso((local or {}).get("updated"))
    if not forzar and valido(local) and f_loc and f_rem and f_rem <= f_loc:
        log("El repositorio no tiene nada mas nuevo que lo de este PC")
        return "Lo de este PC ya es igual o mas nuevo que lo del repositorio"
    if PRECIOS.exists():
        shutil.copy2(PRECIOS, PRECIOS_ANTERIOR)
    PRECIOS.write_bytes(datos)
    log(f"Traido del repositorio: {len(remoto['stations'])} estaciones, de {remoto.get('updated', '?')[:16]}")
    return f"Traidas {len(remoto['stations'])} estaciones del repositorio"


def semilla():
    """Al arrancar: si una pasada se corto a lo bruto (equipo apagado) se recupera su copia; si no hay
    precios.json se pone la copia que viaja en el zip; y luego se intenta traer del repositorio una mas nueva."""
    if PRECIOS_EN_CURSO.exists():
        if not valido(leer_precios()) and valido(leer_precios(PRECIOS_EN_CURSO)):
            PRECIOS_EN_CURSO.replace(PRECIOS)
            log("La ultima recogida se corto a medias: recuperado lo que habia antes")
        else:
            PRECIOS_EN_CURSO.unlink(missing_ok=True)
    if not valido(leer_precios()) and valido(leer_precios(PRECIOS_SEMILLA)):
        shutil.copy2(PRECIOS_SEMILLA, PRECIOS)
        log("Primer arranque: puesta la copia de precios que venia con la app")
    if leer_config().get("semilla_github", True):
        traer_de_github()


# ====================================================================== la aplicacion
app = FastAPI(title="Surtidor", docs_url=None, redoc_url=None, openapi_url=None)


def es_local(request: Request):
    host = request.client.host if request.client else ""
    return host in ("127.0.0.1", "::1", "localhost")


def version_app():
    try:
        return (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return "dev"


@app.get("/", response_class=HTMLResponse)
def mapa(request: Request):
    """El mapa de siempre, con lo de este PC inyectado. Las claves solo van a quien mira desde el propio
    equipo: si el servidor escucha en la red de casa, los demas ven el mapa sin ellas."""
    html = (BASE_DIR / "index.html").read_text(encoding="utf-8")
    cfg = leer_config()
    local = es_local(request)
    ajustes = {
        "local": True,
        "admin": "/admin",
        "version": version_app(),
        "tk": cfg.get("tankerkoenig_clave", "") if local else "",
        "ocm": cfg.get("opencharge_clave", "") if local else "",
    }
    # </ dentro de un <script> lo cerraria: se escapa por si una clave trajera algo raro
    bloque = "<script>window.SURTIDOR=" + json.dumps(ajustes, ensure_ascii=False).replace("</", "<\\/") + ";</script>"
    html = html.replace("</head>", bloque + "\n</head>", 1)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@app.get("/precios.json")
def precios():
    if not PRECIOS.exists():
        return JSONResponse({"updated": None, "count": 0, "stations": []}, headers={"Cache-Control": "no-store"})
    return FileResponse(PRECIOS, media_type="application/json", headers={"Cache-Control": "no-store"})


@app.get("/salud")
def salud():
    return {"ok": True, "version": version_app()}


@app.get("/favicon.ico")
def favicon():
    ico = BASE_DIR / "surtidor.ico"
    if ico.exists():
        return FileResponse(ico, media_type="image/x-icon")
    return Response(status_code=204)


import admin  # noqa: E402  (necesita lo de arriba ya definido)

admin.iniciar(globals())
app.include_router(admin.router)


def main():
    cfg = asegurar_clave()
    log(f"Surtidor {version_app()} · carpeta {BASE_DIR}")
    threading.Thread(target=semilla, daemon=True).start()
    threading.Thread(target=programador, daemon=True).start()
    import uvicorn
    host = str(cfg.get("servidor_escuchar") or "127.0.0.1")
    puerto = int(cfg.get("servidor_puerto") or 8766)
    log(f"Escuchando en http://{host}:{puerto}")
    uvicorn.run(app, host=host, port=puerto, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
