# -*- coding: utf-8 -*-
"""
Surtidor - el lanzador del mapa de gasolineras para quien no quiere saber nada de Python.

Un solo fichero, Surtidor.exe, que lleva dentro Python y todas las librerias. Al abrirlo:
  1. Mira en GitHub si hay una version nueva (el mapa, el servidor, el recolector, el panel) y, si la
     hay, la baja y la instala. Lo tuyo (config.json con tus claves, los precios recogidos, el
     historial) no se toca nunca.
  2. Si es la primera vez, prepara la carpeta de trabajo y crea la clave del panel.
  3. Arranca el servidor y abre el mapa en su propia ventana de escritorio (pywebview, con el motor Edge
     WebView2 de Windows), ya dentro del panel de administracion, sin teclear ninguna clave.
  4. El servidor recoge precios solo, cada las horas que digas en el panel.
Si la ventana de escritorio no puede abrirse (falta WebView2), queda una ventana pequeña que abre el
mapa en el navegador. Cerrar la ventana para el servidor.

Es el mismo mecanismo que Catalogator.exe: version en GitHub Releases, zip de la app que se descarga y
se instala, vuelta a la version anterior si la nueva no arranca, y cambio del propio exe cuando hace falta.

Donde vive todo:  %LOCALAPPDATA%\\Surtidor\\app   (Windows)   ~/.local/share/surtidor/app (otros)
Se puede cambiar con un surtidor.json junto al exe: {"carpeta": "D:\\\\surtidor", "repo": "usuario/repo"}

Modos (para el propio lanzador; el usuario no los necesita):
  Surtidor.exe                 ventana normal
  Surtidor.exe --servidor      (interno) arranca servidor.py desde la carpeta de la app
  Surtidor.exe --recolector    (interno) una pasada de collector/collect.py, la lanza el servidor
  Surtidor.exe --actualizar    solo comprueba e instala actualizaciones, en consola, y sale
  Surtidor.exe --consola       todo en consola, sin ventana (para ver errores de arranque)
  Surtidor.exe --clasica       la ventana pequeña, con el mapa en el navegador
Desarrollo: python surtidor.py funciona igual, con el Python instalado.
"""
import io
import json
import os
import re
import runpy
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
import zipfile
from datetime import datetime
from pathlib import Path

NOMBRE = "Surtidor"
REPO_DEFECTO = "malevolisco/adrigasolineras"
ASSET_APP = "surtidor-app.zip"           # el codigo de la herramienta, tal como lo publica GitHub Actions
ASSET_EXE = "Surtidor.exe"               # el lanzador, por si una version nueva lo necesita
PUERTO_DEFECTO = 8766                    # 8765 es el del Catalogator: los dos pueden convivir
try:
    from surtidor_version import EXE_VERSION   # lo escribe GitHub Actions al compilar
except ImportError:
    EXE_VERSION = "dev"

CONGELADO = getattr(sys, "frozen", False)
EXE = Path(sys.executable if CONGELADO else __file__).resolve()
AQUI = EXE.parent

# lo que es del usuario y nunca se sustituye al actualizar (el zip de la app tampoco lo trae)
DATOS_USUARIO = ("config.json", "precios.json", "precios.anterior.json", "precios.antes.json",
                 "precios.json.tmp", "historial.jsonl", "surtidor.log", "_ventana")


# ====================================================================== carpetas y ajustes del lanzador
def ajustes_lanzador():
    """surtidor.json junto al exe (opcional): carpeta de trabajo y repositorio."""
    ruta = AQUI / "surtidor.json"
    if ruta.exists():
        try:
            return json.loads(ruta.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {}


AJUSTES = ajustes_lanzador()


def carpeta_app():
    if os.environ.get("SURTIDOR_DIR"):
        return Path(os.environ["SURTIDOR_DIR"])
    if AJUSTES.get("carpeta"):
        return Path(AJUSTES["carpeta"])
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
        return base / NOMBRE / "app"
    return Path.home() / ".local" / "share" / NOMBRE.lower() / "app"


APP = carpeta_app()
REPO = AJUSTES.get("repo") or REPO_DEFECTO
LOG = APP / "surtidor.log"


def registrar(texto, ventana=None):
    linea = f"[{datetime.now():%d/%m %H:%M:%S}] {texto}"
    try:
        APP.mkdir(parents=True, exist_ok=True)
        with LOG.open("a", encoding="utf-8") as f:
            f.write(linea + "\n")
    except OSError:
        pass
    if ventana is not None:
        ventana.escribir(linea)
    else:
        try:
            print(linea, flush=True)
        except Exception:
            pass


# ====================================================================== versiones y GitHub
def version_tupla(v):
    """'v2026.10.10' -> (2026, 10, 10); 'v2026.10.10.2' -> (2026, 10, 10, 2); 'dev' -> ()"""
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def version_instalada():
    try:
        return (APP / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _pedir(url, binario=False, timeout=30):
    cab = {"User-Agent": NOMBRE, "Accept": "application/octet-stream" if binario else "application/vnd.github+json"}
    req = urllib.request.Request(url, headers=cab)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


ULTIMO_MOTIVO = [""]


def ultima_version():
    """(tag, {nombre_asset: url_descarga}) de la ultima release, o (None, {}) si no se puede saber; el
    porque queda en ULTIMO_MOTIVO. El repositorio es publico: no hace falta token."""
    try:
        datos = json.loads(_pedir(f"https://api.github.com/repos/{REPO}/releases/latest"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return "", {}                    # el repositorio existe pero aun no tiene versiones
        ULTIMO_MOTIVO[0] = ("GitHub limita las consultas desde esta conexion; se reintenta al abrir otra vez"
                            if e.code in (403, 429) else f"GitHub responde HTTP {e.code}")
        return None, {}
    except (urllib.error.URLError, ValueError, OSError) as e:
        ULTIMO_MOTIVO[0] = f"Sin conexion con GitHub ({type(e).__name__})"
        return None, {}
    assets = {a["name"]: a["url"] for a in datos.get("assets", [])}
    return datos.get("tag_name") or "", assets


def descargar(url, destino):
    datos = _pedir(url, binario=True, timeout=300)
    destino.write_bytes(datos)
    return destino


# ====================================================================== instalar y actualizar la app
def instalar_zip(ruta_zip, tag, ventana=None):
    """Sustituye el codigo de la app por el del zip. Antes guarda el actual en _anterior/ por si hay que
    volver. Lo del usuario (DATOS_USUARIO) no se toca aunque viniera en el zip, que no viene."""
    APP.mkdir(parents=True, exist_ok=True)
    anterior = APP / "_anterior"
    with zipfile.ZipFile(ruta_zip) as z:
        nombres = [n for n in z.namelist() if not n.endswith("/")]
        # que un zip raro no pueda escribir fuera de la carpeta de la app
        for n in nombres:
            if n.startswith(("/", "\\")) or ".." in Path(n).parts:
                raise ValueError(f"Ruta no valida en el zip: {n}")
        if anterior.exists():
            shutil.rmtree(anterior, ignore_errors=True)
        if (APP / "VERSION").exists():
            anterior.mkdir(parents=True, exist_ok=True)
            shutil.copy2(APP / "VERSION", anterior / "VERSION")     # para volver a ella si la nueva no arranca
        for n in nombres:
            viejo = APP / n
            if viejo.exists():
                destino = anterior / n
                destino.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(viejo, destino)
        try:
            for n in nombres:
                if n.split("/")[0] in DATOS_USUARIO:
                    continue
                destino = APP / n
                destino.parent.mkdir(parents=True, exist_ok=True)
                with z.open(n) as origen, destino.open("wb") as salida:
                    shutil.copyfileobj(origen, salida)
        except Exception:
            # a medias no se queda: se restaura lo anterior
            for n in nombres:
                copia = anterior / n
                if copia.exists():
                    shutil.copy2(copia, APP / n)
            raise
    (APP / "VERSION").write_text(tag, encoding="utf-8")


def restaurar_anterior(ventana=None):
    """La version recien instalada no arranca: vuelve a la de _anterior/ y apunta la mala en VERSION_MALA para
    no reinstalarla hasta que salga otra. Devuelve True si habia a donde volver."""
    anterior = APP / "_anterior"
    if not (anterior / "VERSION").exists():
        return False
    mala = version_instalada()
    for f in anterior.rglob("*"):
        if f.is_file():
            destino = APP / f.relative_to(anterior)
            destino.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, destino)
    (APP / "VERSION_MALA").write_text(mala, encoding="utf-8")
    registrar(f"La version {mala} no arranca: se vuelve a la {version_instalada()} y no se reinstala hasta que "
              "salga una mas nueva", ventana)
    return True


def actualizar_app(ventana=None):
    """Comprueba la ultima release e instala el zip si es mas nueva. Devuelve (tag instalado, cambio?)."""
    local = version_instalada()
    tag, assets = ultima_version()
    if tag is None:
        registrar(f"{ULTIMO_MOTIVO[0]}: se sigue con la version instalada" + (f" ({local})" if local else ""), ventana)
        return local, False
    if not tag or ASSET_APP not in assets:
        registrar(f"El repositorio {REPO} no tiene ninguna version publicada con {ASSET_APP}", ventana)
        return local, False
    if local and version_tupla(tag) <= version_tupla(local):
        registrar(f"Al dia: version {local}", ventana)
        return local, False
    try:
        mala = (APP / "VERSION_MALA").read_text(encoding="utf-8").strip()
    except OSError:
        mala = ""
    if mala and tag == mala:
        registrar(f"La version {tag} no arranco en este equipo: se sigue con la {local} hasta que salga otra", ventana)
        return local, False
    registrar(f"Version nueva {tag} (instalada: {local or 'ninguna'}). Descargando...", ventana)
    tmp = APP / f"_{ASSET_APP}"
    APP.mkdir(parents=True, exist_ok=True)
    descargar(assets[ASSET_APP], tmp)
    instalar_zip(tmp, tag, ventana)
    tmp.unlink(missing_ok=True)
    registrar(f"Instalada la version {tag}", ventana)
    # si la version nueva exige un lanzador mas nuevo, se baja y se cambia al salir
    exe_min = ""
    try:
        exe_min = (APP / "EXE_MIN").read_text(encoding="utf-8").strip()
    except OSError:
        pass
    if CONGELADO and exe_min and EXE_VERSION != "dev" and version_tupla(exe_min) > version_tupla(EXE_VERSION) \
            and ASSET_EXE in assets:
        registrar(f"Esta version necesita un {NOMBRE}.exe mas nuevo ({exe_min}): se descarga y se cambia solo", ventana)
        nuevo = EXE.with_name(EXE.stem + ".nuevo.exe")
        descargar(assets[ASSET_EXE], nuevo)
        programar_cambio_exe(nuevo)
    return tag, True


def instalar_app_embebida(ventana=None):
    """Sin red la primera vez: el exe lleva dentro una copia del zip de la app con la que se compilo."""
    base = Path(getattr(sys, "_MEIPASS", AQUI))
    zip_dentro = base / ASSET_APP
    if not zip_dentro.exists():
        zip_dentro = AQUI / "dist" / ASSET_APP          # desarrollo: el que deja empaquetar.py
    if zip_dentro.exists():
        tag = "embebida"
        try:
            with zipfile.ZipFile(zip_dentro) as z:
                if "VERSION" in z.namelist():
                    tag = z.read("VERSION").decode("utf-8").strip()
        except Exception:
            pass
        instalar_zip(zip_dentro, tag, ventana)
        registrar(f"Instalada la version que venia dentro del exe ({tag})", ventana)
        return True
    return False


def programar_cambio_exe(nuevo):
    """Windows no deja sustituir un exe en marcha: un .bat espera a que este se cierre y lo cambia."""
    bat = EXE.with_name("_cambiar_surtidor.bat")
    bat.write_text(
        "@echo off\r\n"
        ":espera\r\n"
        "timeout /t 1 /nobreak >nul\r\n"
        f"move /y \"{nuevo}\" \"{EXE}\" >nul 2>nul || goto espera\r\n"
        f"start \"\" \"{EXE}\"\r\n"
        "del \"%~f0\"\r\n", encoding="utf-8")
    globals()["CAMBIO_EXE_PENDIENTE"] = bat


CAMBIO_EXE_PENDIENTE = None


# ====================================================================== primera vez: config
def leer_config():
    try:
        return json.loads((APP / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def guardar_config(cfg):
    (APP / "config.json").write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


def config_minima():
    """Crea config.json a partir de config.example.json con una clave de panel nueva."""
    ejemplo = APP / "config.example.json"
    try:
        cfg = json.loads(ejemplo.read_text(encoding="utf-8")) if ejemplo.exists() else {}
    except ValueError:
        cfg = {}
    cfg["servidor_clave"] = secrets.token_urlsafe(12)
    cfg.setdefault("servidor_puerto", PUERTO_DEFECTO)
    guardar_config(cfg)


def puerto_config():
    try:
        return int(leer_config().get("servidor_puerto") or PUERTO_DEFECTO)
    except (TypeError, ValueError):
        return PUERTO_DEFECTO


# ====================================================================== los procesos de dentro
def _python_de_dentro(script, argv):
    """Ejecuta un .py de la carpeta de la app con el Python de dentro del exe."""
    os.chdir(APP)
    sys.path.insert(0, str(APP))
    os.environ["PYTHONUTF8"] = "1"
    for canal in ("stdout", "stderr"):
        s = getattr(sys, canal)
        if s is None:                      # exe sin consola: que los print no revienten
            setattr(sys, canal, io.StringIO())
        else:
            try:
                s.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
            except Exception:
                pass
    sys.argv = argv
    runpy.run_path(str(script), run_name="__main__")


def modo_servidor():
    """Proceso hijo: servidor.py. El exe sabe donde esta para que el servidor lance el recolector igual."""
    if CONGELADO:
        os.environ["SURTIDOR_EXE"] = str(EXE)      # el servidor lanzara "Surtidor.exe --recolector"
    else:
        os.environ.pop("SURTIDOR_EXE", None)       # desarrollo: el servidor usa el Python instalado
    os.environ["SURTIDOR_LANZADOR"] = EXE_VERSION
    _python_de_dentro(APP / "servidor.py", ["servidor.py"])


def modo_recolector():
    """Proceso nieto: una pasada de collector/collect.py. Escribe precios.json en la carpeta de la app."""
    _python_de_dentro(APP / "collector" / "collect.py", ["collect.py"])


def orden_servidor():
    if CONGELADO:
        return [str(EXE), "--servidor"]
    return [sys.executable, str(Path(__file__).resolve()), "--servidor"]


def responde(puerto):
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/salud", timeout=2) as r:
            return r.status < 500
    except Exception:
        return False


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


def abrir_carpeta(ruta):
    try:
        if sys.platform == "win32":
            os.startfile(str(ruta))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(ruta)])
        else:
            subprocess.Popen(["xdg-open", str(ruta)])
    except Exception:
        pass


# ====================================================================== la ventana pequeña (plan B)
class Ventana:
    def __init__(self):
        import tkinter as tk
        from tkinter import scrolledtext, ttk
        self.raiz = tk.Tk()
        self.raiz.title(NOMBRE + (f"  ·  lanzador {EXE_VERSION}" if EXE_VERSION != "dev" else ""))
        self.raiz.geometry("640x420")
        self.raiz.minsize(520, 320)
        self.proceso = None
        self.puerto = PUERTO_DEFECTO
        self.token = secrets.token_urlsafe(24)
        self.saliendo = False
        self.reinicio_pedido = False
        self.estado = tk.StringVar(value="Arrancando...")
        marco = ttk.Frame(self.raiz, padding=10)
        marco.pack(fill="both", expand=True)
        ttk.Label(marco, textvariable=self.estado, font=("Segoe UI", 11, "bold")).pack(anchor="w")
        self.texto = scrolledtext.ScrolledText(marco, height=14, font=("Consolas", 9), state="disabled")
        self.texto.pack(fill="both", expand=True, pady=(8, 8))
        botones = ttk.Frame(marco)
        botones.pack(fill="x")
        self.b_abrir = ttk.Button(botones, text="Abrir el mapa", command=self.abrir_pagina, state="disabled")
        self.b_abrir.pack(side="left")
        ttk.Button(botones, text="Administración", command=lambda: self.abrir_pagina("/admin")).pack(side="left", padx=6)
        ttk.Button(botones, text="Carpeta", command=lambda: abrir_carpeta(APP)).pack(side="left")
        ttk.Button(botones, text="Buscar actualizaciones", command=self.buscar_actualizaciones).pack(side="left", padx=6)
        ttk.Button(botones, text="Salir", command=self.salir).pack(side="right")
        self.raiz.protocol("WM_DELETE_WINDOW", self.salir)

    def escribir(self, linea):
        def _():
            self.texto.configure(state="normal")
            self.texto.insert("end", linea + "\n")
            self.texto.see("end")
            self.texto.configure(state="disabled")
        try:
            self.raiz.after(0, _)
        except Exception:
            pass

    def poner_estado(self, texto):
        try:
            self.raiz.after(0, lambda: self.estado.set(texto))
        except Exception:
            pass

    def listo(self):
        try:
            self.raiz.after(0, lambda: self.b_abrir.configure(state="normal"))
        except Exception:
            pass

    def abrir_pagina(self, destino="/"):
        webbrowser.open(f"http://127.0.0.1:{self.puerto}/local?t={self.token}&a={destino}")

    def buscar_actualizaciones(self):
        def _():
            try:
                tag, cambio = actualizar_app(self)
                if cambio:
                    self.reinicio_pedido = True
                    matar(self.proceso)
            except Exception as e:
                registrar(f"No se ha podido actualizar: {type(e).__name__}: {str(e)[:200]}", self)
        threading.Thread(target=_, daemon=True).start()

    def avisar(self, titulo, texto):
        from tkinter import messagebox
        messagebox.showinfo(titulo, texto, parent=self.raiz)

    def salir(self):
        self.saliendo = True
        if self.proceso is not None and self.proceso.poll() is None:
            registrar("Parando el servidor...", self)
            matar(self.proceso)
        try:
            self.raiz.destroy()
        except Exception:
            pass
        if CAMBIO_EXE_PENDIENTE:
            subprocess.Popen(["cmd", "/c", str(CAMBIO_EXE_PENDIENTE)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


# ====================================================================== la ventana de escritorio
HTML_ARRANQUE = """<!doctype html><html lang="es"><head><meta charset="utf-8"><title>Surtidor</title>
<style>
  html, body { margin:0; height:100%; }
  body { background:#0C2730; color:#85A7AF; font:14px/1.5 "Segoe UI Variable Text", "Segoe UI", system-ui, Arial, sans-serif;
         display:flex; align-items:center; justify-content:center; }
  main { width:min(640px, 90vw); }
  .marca { display:flex; align-items:center; gap:14px; margin-bottom:26px; }
  .marca div.ic { width:48px; height:48px; border-radius:12px; background:#FFC53D; display:grid; place-items:center;
                  box-shadow:0 6px 18px rgba(255,197,61,.25); }
  .marca svg { width:28px; height:28px; stroke:#0C2730; fill:none; stroke-width:1.9; stroke-linecap:round; stroke-linejoin:round; }
  h1 { color:#EDF4F2; font-size:24px; font-weight:700; margin:0; letter-spacing:.06em; text-transform:uppercase; }
  .sub { color:#85A7AF; font-size:13px; }
  #estado { color:#EDF4F2; margin:0 0 12px; font-weight:600; }
  .barra { height:3px; border-radius:3px; background:#123845; overflow:hidden; margin-bottom:16px; }
  .barra::after { content:""; display:block; height:100%; width:35%; border-radius:3px; background:#FFC53D;
                  animation:ir 1.4s ease-in-out infinite; }
  @keyframes ir { 0% { transform:translateX(-100%) } 100% { transform:translateX(290%) } }
  #lineas { background:#081c23; border:1px solid #1D5265; border-radius:10px; padding:12px 14px; height:min(40vh, 300px);
            overflow:auto; font:12px/1.65 "Cascadia Mono", Consolas, monospace; white-space:pre-wrap; color:#9fbcc2; }
</style></head><body><main>
<div class="marca"><div class="ic"><svg viewBox="0 0 24 24"><path d="M4 21V5a2 2 0 0 1 2-2h6a2 2 0 0 1 2 2v16"/><path d="M3 21h12"/><path d="M7 8h4"/><path d="M17 10v7a2 2 0 0 0 4 0V9l-3-3"/></svg></div>
<div><h1>Surtidor</h1><div class="sub">Dónde repostar entre Madrid y Ámsterdam</div></div></div>
<p id="estado">Arrancando...</p>
<div class="barra"></div>
<div id="lineas"></div>
</main><script>
  function anadir(t) { const d = document.getElementById("lineas"); d.textContent += t + "\\n"; d.scrollTop = d.scrollHeight; }
  function estado(t) { document.getElementById("estado").textContent = t; }
</script></body></html>"""


class ApiLanzador:
    """Lo que el panel puede pedirle al lanzador desde la ventana de escritorio (Administración → Sistema).
    pywebview lo publica como window.pywebview.api; lo que empieza por _ no se publica."""

    def __init__(self, ventana):
        self._v = ventana

    def info(self):
        return {"lanzador": EXE_VERSION, "app": version_instalada() or "?", "carpeta": str(APP)}

    def abrir_carpeta(self):
        abrir_carpeta(APP)
        return ""

    def buscar_actualizaciones(self):
        try:
            tag, cambio = actualizar_app(self._v)
        except Exception as e:
            return f"No se ha podido actualizar: {type(e).__name__}: {str(e)[:200]}"
        if not cambio:
            return f"Ya tienes la última versión ({tag or '?'})."
        if CAMBIO_EXE_PENDIENTE:
            return f"Versión {tag} instalada. Necesita un Surtidor.exe nuevo: cierra la ventana y se abrirá sola con el nuevo."
        # el codigo nuevo lo carga el servidor al arrancar: se reinicia solo
        self._v.reinicio_pedido = True
        threading.Timer(1.5, lambda: matar(self._v.proceso)).start()
        return f"Versión {tag} instalada. Reinicio el servidor para usarla (unos segundos)."


class VentanaWeb:
    """La ventana de escritorio: primero una pantalla de arranque con lo que va pasando y, en cuanto el
    servidor responde, el mapa (entrando con un token de un solo uso, sin clave)."""

    def __init__(self, webview):
        self.webview = webview
        self.proceso = None
        self.puerto = PUERTO_DEFECTO
        self.token = secrets.token_urlsafe(24)
        self.saliendo = False
        self.reinicio_pedido = False
        self.en_mapa = False
        self.cargada = threading.Event()
        self.win = webview.create_window(NOMBRE, html=HTML_ARRANQUE, js_api=ApiLanzador(self), width=1360, height=900,
                                         min_size=(900, 620), text_select=True, background_color="#0C2730")
        self.win.events.loaded += lambda *a: self.cargada.set()

    def _js(self, codigo):
        if self.en_mapa or self.saliendo:
            return
        if self.cargada.wait(10):
            try:
                self.win.evaluate_js(codigo)
            except Exception:
                pass

    def escribir(self, linea):
        self._js(f"anadir({json.dumps(linea)})")

    def poner_estado(self, texto):
        self._js(f"estado({json.dumps(texto)})")

    def listo(self):
        pass

    def abrir_pagina(self, destino="/"):
        self.en_mapa = True
        self.win.load_url(f"http://127.0.0.1:{self.puerto}/local?t={self.token}&a={destino}")

    def avisar(self, titulo, texto):
        registrar(texto.replace("\n\n", " ").replace("\n", " "), self)

    def salir(self):
        self.saliendo = True
        if self.proceso is not None and self.proceso.poll() is None:
            registrar("Parando el servidor...", None)
            matar(self.proceso)
        if CAMBIO_EXE_PENDIENTE:
            subprocess.Popen(["cmd", "/c", str(CAMBIO_EXE_PENDIENTE)], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def ventana_escritorio():
    """Abre la ventana de escritorio y no vuelve hasta que se cierra. False si no se ha podido abrir."""
    try:
        import webview
    except Exception as e:
        registrar(f"Sin ventana de escritorio ({type(e).__name__}): se usa el navegador")
        return False
    try:
        v = VentanaWeb(webview)
    except Exception as e:
        registrar(f"No se ha podido crear la ventana de escritorio ({type(e).__name__}: {e}): se usa el navegador")
        return False
    registrar(f"{NOMBRE} lanzador {EXE_VERSION} · carpeta {APP} · repo {REPO}", v)
    almacen = APP / "_ventana"
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True        # que "Descargar precios.json" funcione
    except Exception:
        pass
    try:
        almacen.mkdir(parents=True, exist_ok=True)
        webview.start(arrancar, (v,), private_mode=False, storage_path=str(almacen))
    except Exception as e:
        registrar(f"La ventana de escritorio ha fallado ({type(e).__name__}: {e}): se usa el navegador")
        if v.proceso is None:
            return False                         # no llego a arrancar nada: se prueba con la ventana pequeña
    v.salir()
    return True


class Consola:
    """Misma interfaz que la ventana, pero en texto (modo --consola y --actualizar)."""
    proceso = None
    puerto = PUERTO_DEFECTO
    token = ""
    saliendo = False
    reinicio_pedido = False

    def __init__(self):
        self.token = secrets.token_urlsafe(24)

    def escribir(self, linea):
        print(linea, flush=True)

    def poner_estado(self, texto):
        print("== " + texto, flush=True)

    def listo(self):
        pass

    def abrir_pagina(self, destino="/"):
        webbrowser.open(f"http://127.0.0.1:{self.puerto}/local?t={self.token}&a={destino}")

    def avisar(self, titulo, texto):
        print(texto)


# ====================================================================== la secuencia de arranque
def preparar(ventana):
    """Actualiza (o instala) y prepara la carpeta y el config.json. Devuelve True si se puede arrancar."""
    APP.mkdir(parents=True, exist_ok=True)
    ventana.poner_estado("Comprobando actualizaciones...")
    try:
        _, ventana.recien_actualizada = actualizar_app(ventana)
    except Exception as e:
        registrar(f"No se ha podido actualizar ({type(e).__name__}: {str(e)[:160]}); se sigue con lo que hay", ventana)
    if not (APP / "servidor.py").exists():
        if not instalar_app_embebida(ventana):
            ventana.poner_estado("No hay versión instalada y no se ha podido descargar")
            ventana.avisar(NOMBRE, "Hace falta conexión a internet la primera vez para descargar el programa.")
            return False
    if not (APP / "config.json").exists():
        config_minima()
        registrar("Primera vez: creado config.json con una clave de panel nueva", ventana)
    return True


CODIGO_REINICIO = 75      # el servidor sale con este codigo cuando el panel pide reiniciarlo (admin.py)


def arrancar(ventana):
    if not preparar(ventana):
        return
    ventana.puerto = puerto_config()
    if responde(ventana.puerto):
        # ya hay un Surtidor abierto (o algo en ese puerto): no se lanza otro, se abre el que hay
        registrar(f"Ya hay un servidor respondiendo en el puerto {ventana.puerto}: se abre ese", ventana)
        ventana.poner_estado(f"En marcha: http://127.0.0.1:{ventana.puerto}")
        ventana.listo()
        ventana.abrir_pagina()
        return
    ventana.poner_estado("Arrancando el servidor...")
    lanzar_servidor(ventana, primera=True)


def lanzar_servidor(ventana, primera=False):
    env = dict(os.environ, PYTHONUTF8="1", SURTIDOR_DIR=str(APP), SURTIDOR_LANZADOR=EXE_VERSION)
    if getattr(ventana, "token", ""):
        env["SURTIDOR_TOKEN_LOCAL"] = ventana.token        # la ventana entra sin clave
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    ventana.reinicio_pedido = False
    ventana.proceso = subprocess.Popen(orden_servidor(), cwd=str(APP), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                       text=True, encoding="utf-8", errors="replace", env=env, creationflags=flags)
    threading.Thread(target=_volcar_salida, args=(ventana, ventana.proceso), daemon=True).start()
    for _ in range(60):
        if responde(ventana.puerto):
            ventana.poner_estado(f"En marcha: http://127.0.0.1:{ventana.puerto}  ·  versión {version_instalada() or '?'}")
            ventana.listo()
            if primera:
                ventana.abrir_pagina()
            ventana.recien_actualizada = False
            return
        if ventana.proceso.poll() is not None:
            if _volver_atras(ventana, primera):
                return
            ventana.poner_estado("El servidor se ha parado nada más arrancar: mira el registro")
            return
        time.sleep(1)
    if _volver_atras(ventana, primera):
        return
    ventana.poner_estado("El servidor no responde: mira el registro")


def _volver_atras(ventana, primera):
    """Si la version recien instalada no arranca, vuelve a la anterior y lanza el servidor otra vez."""
    if not getattr(ventana, "recien_actualizada", False):
        return False
    ventana.recien_actualizada = False
    if ventana.proceso.poll() is None:
        matar(ventana.proceso)
    if not restaurar_anterior(ventana):
        return False
    ventana.poner_estado("La versión nueva no arranca: volviendo a la anterior...")
    lanzar_servidor(ventana, primera)
    return True


def _volcar_salida(ventana, proceso):
    try:
        for linea in proceso.stdout:
            registrar(linea.rstrip("\n"), ventana)
    except Exception:
        pass
    codigo = proceso.wait()
    if getattr(ventana, "saliendo", False) or proceso is not getattr(ventana, "proceso", proceso):
        return               # cerrando, o un proceso viejo (la vuelta a la version anterior ya lanzo otro)
    if codigo == CODIGO_REINICIO or getattr(ventana, "reinicio_pedido", False):
        registrar("Reiniciando el servidor...", ventana)
        ventana.poner_estado("Reiniciando el servidor...")
        ventana.puerto = puerto_config()        # por si el reinicio era para cambiar de puerto
        lanzar_servidor(ventana)
        return
    ventana.poner_estado("El servidor se ha parado")


def main():
    args = sys.argv[1:]
    if "--servidor" in args:
        modo_servidor()
        return 0
    if "--recolector" in args:
        modo_recolector()
        return 0
    if "--actualizar" in args:
        c = Consola()
        try:
            actualizar_app(c)
        except Exception as e:
            print(f"No se ha podido actualizar: {type(e).__name__}: {e}")
            return 1
        return 0
    if "--consola" in args:
        c = Consola()
        try:
            arrancar(c)
            if c.proceso is not None:
                while True:
                    codigo = c.proceso.wait()
                    if codigo != CODIGO_REINICIO and not c.reinicio_pedido:
                        break
                    time.sleep(2)                 # _volcar_salida ya lo ha relanzado
        except KeyboardInterrupt:
            pass
        finally:
            c.saliendo = True
            if c.proceso is not None and c.proceso.poll() is None:
                matar(c.proceso)
        return 0
    if "--clasica" not in args and ventana_escritorio():
        return 0
    try:
        v = Ventana()
    except Exception as e:                   # sin tkinter (raro): consola
        print(f"Sin ventana ({type(e).__name__}); modo consola")
        sys.argv.append("--consola")
        return main()
    registrar(f"{NOMBRE} lanzador {EXE_VERSION} · carpeta {APP} · repo {REPO}", v)
    threading.Thread(target=arrancar, args=(v,), daemon=True).start()
    v.raiz.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
