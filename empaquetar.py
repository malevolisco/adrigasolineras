# -*- coding: utf-8 -*-
"""
empaquetar.py - Hace el zip de la app (surtidor-app.zip) que Surtidor.exe descarga de GitHub.

Lleva el codigo, el mapa y el panel, nunca lo del usuario (config.json, precios recogidos, historial).
Si hay un precios.json en el repositorio, viaja como precios.semilla.json: es lo que ve quien abre el
programa por primera vez sin conexion, hasta que haga su primera recogida.

Lo usa GitHub Actions al publicar una version; tambien vale a mano:

    python empaquetar.py v2026.10.10          -> dist/surtidor-app.zip con VERSION = v2026.10.10
"""
import ast
import importlib.util
import sys
import zipfile
from pathlib import Path

AQUI = Path(__file__).resolve().parent

# lo que es de la herramienta
INCLUIR = [
    "servidor.py", "admin.py", "index.html", "panel/admin.html", "collector/collect.py",
    "config.example.json", "EXE_MIN", "surtidor.ico", "README.md",
]
# imprescindible: sin esto la app no arranca y no se publica
OBLIGATORIOS = {"servidor.py", "admin.py", "index.html", "panel/admin.html", "collector/collect.py"}
# lo del usuario no viaja nunca, aunque alguien lo anada a INCLUIR por error
EXCLUIR = {"config.json", "precios.json", "precios.anterior.json", "precios.antes.json", "historial.jsonl",
           "surtidor.log"}
SEMILLA = ("precios.json", "precios.semilla.json")


def ficheros():
    for rel in INCLUIR:
        p = AQUI / rel
        if p.is_file() and rel not in EXCLUIR:
            yield rel, p


def modulos_que_faltan(incluidos):
    """Modulos que importa el codigo del zip y que no estan ni en el zip ni instalados."""
    en_zip = {Path(rel).stem for rel in incluidos if rel.endswith(".py")}
    ruta_sin_repo = [d for d in sys.path if Path(d or ".").resolve() != AQUI]
    faltan = {}
    for rel in incluidos:
        if not rel.endswith(".py"):
            continue
        arbol = ast.parse((AQUI / rel).read_text(encoding="utf-8"), rel)
        for nodo in ast.walk(arbol):
            if isinstance(nodo, ast.Import):
                nombres = [a.name.split(".")[0] for a in nodo.names]
            elif isinstance(nodo, ast.ImportFrom) and nodo.module and not nodo.level:
                nombres = [nodo.module.split(".")[0]]
            else:
                continue
            for m in nombres:
                if m in en_zip or m in sys.stdlib_module_names:
                    continue
                if (AQUI / f"{m}.py").exists() or not _instalado(m, ruta_sin_repo):
                    faltan.setdefault(m, set()).add(rel)
    return faltan


def _instalado(modulo, ruta):
    guardada = sys.path[:]
    try:
        sys.path[:] = ruta
        return importlib.util.find_spec(modulo) is not None
    except (ImportError, ValueError):
        return False
    finally:
        sys.path[:] = guardada


def empaquetar(version, destino=None):
    incluidos = [rel for rel, _ in ficheros()]
    sin = OBLIGATORIOS - set(incluidos)
    if sin:
        sys.exit("No se publica: faltan " + ", ".join(sorted(sin)))
    faltan = modulos_que_faltan(incluidos)
    if faltan:
        for m, quien in sorted(faltan.items()):
            print(f"ERROR: falta {m} (lo usan: {', '.join(sorted(quien))})")
        sys.exit("No se publica: sube los ficheros que faltan o instala las librerias y vuelve a compilar.")
    destino = Path(destino) if destino else AQUI / "dist" / "surtidor-app.zip"
    destino.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destino, "w", zipfile.ZIP_DEFLATED) as z:
        n = 0
        for rel, p in ficheros():
            z.write(p, rel)
            n += 1
        origen, nombre = SEMILLA
        if (AQUI / origen).is_file():
            z.write(AQUI / origen, nombre)
            n += 1
        z.writestr("VERSION", version.strip() + "\n")
    print(f"{destino}: {n} ficheros, version {version}")
    return destino


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    empaquetar(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
