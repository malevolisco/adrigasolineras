# -*- coding: utf-8 -*-
"""
admin.py - La administracion de Surtidor: lo que usa el panel (/admin).

    entrada               /local?t=...  la ventana de escritorio entra sin teclear la clave
                          /api/admin/entrar con la clave, desde un navegador cualquiera
    resumen               estado de la recogida, cobertura de los datos, proxima pasada
    recoger / parar       lanzar una pasada ahora o cortar la que esta en marcha
    traer del repositorio el precios.json que publica la Action, si es mas nuevo
    registro en vivo      todo lo que escriben el servidor y el recolector
    historial             una linea por pasada
    ajustes               config.json en un formulario; las claves nunca vuelven al panel
    diagnostico           si cada fuente de precios responde desde este PC, y cuanto tarda
    reiniciar             el servidor sale con el codigo 75 y el lanzador lo vuelve a arrancar

servidor.py llama a iniciar(globals()) y monta router.
"""
import os
import secrets
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

router = APIRouter()
S = {}                        # los globales de servidor.py
COOKIE = "surtidor_sesion"
SESIONES = set()


def iniciar(globales):
    S.clear()
    S.update(globales)


def g(nombre):
    return S[nombre]


# ====================================================================== entrar
def autorizado(request: Request):
    return request.cookies.get(COOKIE) in SESIONES


def exigir(request: Request):
    if not autorizado(request):
        raise HTTPException(status_code=401, detail="Hace falta entrar")


def _con_sesion(resp, request):
    token = secrets.token_urlsafe(24)
    SESIONES.add(token)
    resp.set_cookie(COOKIE, token, httponly=True, samesite="strict",
                    secure=request.url.scheme == "https", max_age=60 * 60 * 24 * 30)
    return resp


@router.get("/local")
def entrada_local(request: Request, t: str = "", a: str = "/"):
    """La ventana de Surtidor.exe entra aqui con un token de un solo arranque: deja la sesion abierta y
    lleva al mapa (o a donde diga a=), sin pedir la clave."""
    esperado = os.environ.get("SURTIDOR_TOKEN_LOCAL", "")
    destino = a if a in ("/", "/admin") else "/"
    if not esperado or not secrets.compare_digest(t, esperado):
        return RedirectResponse(destino, status_code=303)
    return _con_sesion(RedirectResponse(destino, status_code=303), request)


@router.post("/api/admin/entrar")
async def entrar(request: Request):
    try:
        datos = await request.json()
    except ValueError:
        datos = {}
    clave = str(datos.get("clave") or "")
    buena = str(g("leer_config")().get("servidor_clave") or "")
    if not buena or not secrets.compare_digest(clave, buena):
        time.sleep(0.6)                 # que probar claves a ciegas salga caro
        raise HTTPException(status_code=401, detail="Clave incorrecta")
    return _con_sesion(JSONResponse({"ok": True}), request)


@router.post("/api/admin/salir")
def salir(request: Request):
    SESIONES.discard(request.cookies.get(COOKIE))
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(COOKIE)
    return resp


# ====================================================================== el panel
@router.get("/admin", response_class=HTMLResponse)
def panel():
    """La pagina siempre se sirve; si no hay sesion, ella misma pide la clave."""
    html = (g("BASE_DIR") / "panel" / "admin.html").read_text(encoding="utf-8")
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


@router.get("/api/admin/resumen")
def resumen(request: Request):
    exigir(request)
    cfg = g("leer_config")()
    d = g("leer_precios")()
    proxima = g("proxima_recogida")()
    hist = g("historial")(1)
    return {
        "version": g("version_app")(),
        "lanzador": os.environ.get("SURTIDOR_LANZADOR", "dev"),
        "carpeta": str(g("BASE_DIR")),
        "recolector": g("RECOLECTOR").estado(),
        "ultima": hist[0] if hist else None,
        "proxima": proxima.isoformat(timespec="seconds") if proxima else None,
        "cada_horas": cfg.get("recoger_cada_horas"),
        "datos": g("cobertura")(d),
        "hay_copia_anterior": g("PRECIOS_ANTERIOR").exists(),
    }


@router.post("/api/admin/recoger")
def recoger(request: Request):
    exigir(request)
    ok, texto = g("RECOLECTOR").lanzar("manual")
    if not ok:
        raise HTTPException(status_code=409, detail=texto)
    return {"ok": True, "texto": texto}


@router.post("/api/admin/recoger/parar")
def parar(request: Request):
    exigir(request)
    if not g("RECOLECTOR").parar():
        raise HTTPException(status_code=409, detail="No hay ninguna recogida en marcha")
    return {"ok": True, "texto": "Recogida parada: se mantiene lo que habia antes de empezar"}


@router.post("/api/admin/github")
def github(request: Request):
    exigir(request)
    return {"ok": True, "texto": g("traer_de_github")(forzar=False)}


@router.post("/api/admin/deshacer")
def deshacer(request: Request):
    """Vuelve a la copia de antes de la ultima pasada (o de la ultima vez que se trajo del repositorio)."""
    exigir(request)
    if g("RECOLECTOR").en_marcha:
        raise HTTPException(status_code=409, detail="Hay una recogida en marcha")
    anterior, actual = g("PRECIOS_ANTERIOR"), g("PRECIOS")
    if not g("valido")(g("leer_precios")(anterior)):
        raise HTTPException(status_code=404, detail="No hay copia anterior que recuperar")
    tmp = actual.with_suffix(".cambio")
    if actual.exists():
        shutil.copy2(actual, tmp)
    shutil.copy2(anterior, actual)
    if tmp.exists():
        tmp.replace(anterior)          # la que habia pasa a ser la copia: se puede volver a deshacer
    g("log")("Recuperada la copia anterior de precios.json")
    return {"ok": True, "texto": "Recuperada la copia anterior"}


@router.get("/api/admin/registro")
def registro(request: Request, desde: int = 0):
    exigir(request)
    lineas, ultimo = g("registro_desde")(desde)
    return {"lineas": lineas[-1500:], "ultimo": ultimo}


@router.get("/api/admin/historial")
def historial(request: Request):
    exigir(request)
    return {"pasadas": g("historial")(60)}


@router.get("/api/admin/precios.json")
def descargar_precios(request: Request):
    exigir(request)
    ruta = g("PRECIOS")
    if not ruta.exists():
        raise HTTPException(status_code=404, detail="Todavia no hay precios recogidos")
    nombre = f"precios-{datetime.now():%Y%m%d-%H%M}.json"
    return FileResponse(ruta, media_type="application/json", filename=nombre)


# ====================================================================== ajustes
# tipo de cada ajuste que se puede tocar desde el panel; lo demas de config.json se respeta tal cual
TIPOS = {
    "recoger_cada_horas": (float, 0, 168),
    "recoger_al_arrancar": (bool, None, None),
    "recoger_tope_minutos": (int, 5, 600),
    "semilla_github": (bool, None, None),
    "repo": (str, None, None),
    "tankerkoenig_clave": (str, None, None),
    "opencharge_clave": (str, None, None),
    "servidor_puerto": (int, 1024, 65535),
    "servidor_escuchar": (str, None, None),
}
NECESITAN_REINICIO = ("servidor_puerto", "servidor_escuchar")


@router.get("/api/admin/ajustes")
def ver_ajustes(request: Request):
    exigir(request)
    cfg = g("leer_config")()
    salida = {}
    for k in TIPOS:
        if k in g("SECRETOS"):
            salida[k] = {"puesta": bool(cfg.get(k))}
        else:
            salida[k] = cfg.get(k)
    salida["servidor_clave"] = {"puesta": bool(cfg.get("servidor_clave"))}
    return salida


@router.post("/api/admin/ajustes")
async def guardar_ajustes(request: Request):
    """Solo cambia lo que llega. Una clave vacia significa "dejala como esta"; para borrarla hay que
    mandar borrar: true en esa clave."""
    exigir(request)
    try:
        datos = await request.json()
    except ValueError:
        raise HTTPException(status_code=400, detail="Datos ilegibles")
    cfg = g("leer_config")()
    cambiados, errores = [], []
    for k, v in (datos or {}).items():
        if k not in TIPOS:
            continue
        tipo, minimo, maximo = TIPOS[k]
        if k in g("SECRETOS"):
            if isinstance(v, dict) and v.get("borrar"):
                if cfg.get(k):
                    cfg[k] = ""
                    cambiados.append(k)
                continue
            v = str(v or "").strip()
            if not v:
                continue                                   # vacio = no tocar
            if cfg.get(k) != v:
                cfg[k] = v
                cambiados.append(k)
            continue
        try:
            if tipo is bool:
                nuevo = v if isinstance(v, bool) else str(v).lower() in ("1", "true", "si", "sí", "on")
            elif tipo is str:
                nuevo = str(v).strip()
            else:
                nuevo = tipo(v)
                if (minimo is not None and nuevo < minimo) or (maximo is not None and nuevo > maximo):
                    raise ValueError
        except (TypeError, ValueError):
            errores.append(f"{k}: tiene que estar entre {minimo} y {maximo}")
            continue
        if k == "servidor_escuchar" and nuevo not in ("127.0.0.1", "0.0.0.0"):
            errores.append("servidor_escuchar: 127.0.0.1 (solo este PC) o 0.0.0.0 (la red de casa)")
            continue
        if k == "repo" and nuevo.count("/") != 1:
            errores.append("repo: tiene que ser usuario/repositorio")
            continue
        if cfg.get(k) != nuevo:
            cfg[k] = nuevo
            cambiados.append(k)
    if errores:
        raise HTTPException(status_code=400, detail="; ".join(errores))
    if cambiados:
        g("guardar_config")(cfg)
        g("log")("Ajustes cambiados: " + ", ".join(cambiados))
    return {"ok": True, "cambiados": cambiados,
            "reiniciar": any(k in NECESITAN_REINICIO for k in cambiados)}


@router.post("/api/admin/cambiar-clave")
async def cambiar_clave(request: Request):
    exigir(request)
    try:
        datos = await request.json()
    except ValueError:
        datos = {}
    nueva = str((datos or {}).get("nueva") or "").strip()
    if len(nueva) < 12:
        raise HTTPException(status_code=400, detail="La clave tiene que tener al menos 12 caracteres")
    cfg = g("leer_config")()
    cfg["servidor_clave"] = nueva
    g("guardar_config")(cfg)
    # las demas sesiones quedan fuera; la de quien la cambia sigue
    actual = request.cookies.get(COOKIE)
    SESIONES.intersection_update({actual})
    g("log")("Clave del panel cambiada")
    return {"ok": True}


@router.get("/api/admin/clave")
def ver_clave(request: Request):
    """La clave solo se ensena a quien ya ha entrado desde este mismo PC (para apuntarla y usarla en un
    navegador o en otro equipo de casa)."""
    exigir(request)
    if not g("es_local")(request):
        raise HTTPException(status_code=403, detail="Solo desde este PC")
    return {"clave": g("leer_config")().get("servidor_clave", "")}


# ====================================================================== sistema
FUENTES = [
    ("España · MITECO", "https://sedeaplicaciones.minetur.gob.es/ServiciosRESTCarburantes/PreciosCarburantes/"
                        "EstacionesTerrestres/FiltroProvincia/28"),
    ("Francia · Ministerio de Economía", "https://data.economie.gouv.fr/api/explore/v2.1/catalog/datasets/"
                                        "prix-des-carburants-en-france-flux-instantane-v2/records?limit=1"),
    ("Países Bajos y Bélgica · Prijzenindex", "https://prijzenindex.nl/brandstof"),
    ("Países Bajos · CBS", "https://opendata.cbs.nl/ODataApi/OData/80416ned/TableInfos?$format=json"),
    ("OpenStreetMap · Overpass", "https://overpass-api.de/api/status"),
    ("Medias por país · OpenVan", "https://openvan.camp/api/fuel/prices?source=ruta-diesel"),
    ("Repositorio · GitHub", "https://raw.githubusercontent.com/malevolisco/adrigasolineras/main/estado.json"),
]


def _probar(nombre, url):
    t0 = time.monotonic()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Surtidor (diagnostico)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read(2048)
            codigo = r.status
    except urllib.error.HTTPError as e:
        codigo = e.code
    except Exception as e:
        motivo = getattr(e, "reason", None) or e
        return {"fuente": nombre, "ok": False, "ms": None,
                "detalle": f"{type(e).__name__}: {str(motivo)[:70]}"}
    ms = round((time.monotonic() - t0) * 1000)
    return {"fuente": nombre, "ok": 200 <= codigo < 400, "ms": ms, "detalle": f"HTTP {codigo}"}


@router.get("/api/admin/diagnostico")
def diagnostico(request: Request):
    exigir(request)
    with ThreadPoolExecutor(max_workers=len(FUENTES)) as ex:
        fuentes = list(ex.map(lambda f: _probar(*f), FUENTES))
    base = g("BASE_DIR")
    try:
        libre = round(shutil.disk_usage(base).free / 1024 ** 3, 1)
    except OSError:
        libre = None
    return {
        "fuentes": fuentes,
        "python": sys.version.split()[0],
        "sistema": sys.platform,
        "disco_libre_gb": libre,
        "precios_mb": round(g("PRECIOS").stat().st_size / 1024 ** 2, 2) if g("PRECIOS").exists() else 0,
        "hora": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


@router.post("/api/admin/reiniciar")
def reiniciar(request: Request):
    """El servidor sale con el codigo 75; el lanzador lo ve y lo arranca de nuevo."""
    exigir(request)
    g("log")("Reinicio pedido desde el panel")
    rec = g("RECOLECTOR")
    if rec.en_marcha:
        rec.parar()
    threading.Timer(0.8, lambda: os._exit(g("CODIGO_REINICIO"))).start()
    return {"ok": True}
