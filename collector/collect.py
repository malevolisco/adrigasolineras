#!/usr/bin/env python3
"""
Recolector de precios de diesel por estacion — Paises Bajos y Belgica.

Se ejecuta desde una GitHub Action dos veces al dia y escribe precios.json
en la raiz del sitio. El navegador lo lee desde el propio dominio, asi que
no hay problema de CORS ni hace falta servidor.

Normas de la casa, no negociables:
  1. Antes de pedir nada, se consulta robots.txt. Si prohibe la ruta, se salta.
  2. Agente identificado y contacto visible.
  3. Dos ejecuciones diarias. Ni una mas.
  4. Cada precio guarda su fuente y su fecha.
"""

import json
import re
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone
from urllib.parse import urlparse
from urllib.request import Request, urlopen

AGENTE = "RutaDieselBot/1.0 (+proyecto no comercial; contacto en la web)"
ESPERA = 2.0          # segundos entre peticiones al mismo dominio
SALIDA = "precios.json"

_robots = {}


def permitido(url: str) -> bool:
    """True si robots.txt del dominio permite esta ruta."""
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            # Sin robots.txt legible: se asume permitido, es el criterio estandar
            rp = None
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(AGENTE, url)


def traer(url: str, timeout: int = 25) -> str:
    if not permitido(url):
        raise PermissionError(f"robots.txt prohibe {url}")
    req = Request(url, headers={"User-Agent": AGENTE, "Accept-Language": "nl,en"})
    with urlopen(req, timeout=timeout) as r:
        html = r.read().decode("utf-8", "replace")
    time.sleep(ESPERA)
    return html


# ---------------------------------------------------------------- utilidades

PRECIO = re.compile(r"(?<![\d,.])([12][,.]\d{2,3})(?![\d])")
COORD = re.compile(r'"lat(?:itude)?"\s*:\s*"?(-?\d+\.\d+)"?.{0,80}?"l(?:ng|on|ongitude)"\s*:\s*"?(-?\d+\.\d+)"?', re.S | re.I)


def a_float(txt: str):
    try:
        v = float(str(txt).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return v if 0.5 < v < 5 else None


def json_incrustado(html: str):
    """Extrae objetos JSON incrustados (__NEXT_DATA__, JSON-LD, etc.)."""
    out = []
    for patron in (
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
        r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});',
    ):
        for m in re.finditer(patron, html, re.S | re.I):
            try:
                out.append(json.loads(m.group(1)))
            except Exception:
                pass
    return out


def busca_estaciones(obj, salida, marca, pais):
    """Recorre un JSON cualquiera buscando objetos con lat, lon y un precio."""
    if isinstance(obj, dict):
        lat = obj.get("lat") or obj.get("latitude") or obj.get("Latitude")
        lon = obj.get("lng") or obj.get("lon") or obj.get("longitude") or obj.get("Longitude")
        precio = None
        for k, v in obj.items():
            kl = str(k).lower()
            if ("diesel" in kl or "gazole" in kl or "b7" in kl) and not isinstance(v, (dict, list)):
                precio = a_float(v)
                if precio:
                    break
        if lat and lon and precio:
            salida.append({
                "brand": marca,
                "country": pais,
                "lat": float(str(lat).replace(",", ".")),
                "lon": float(str(lon).replace(",", ".")),
                "price": precio,
                "name": obj.get("name") or obj.get("title") or marca,
                "city": obj.get("city") or obj.get("plaats") or "",
            })
        for v in obj.values():
            busca_estaciones(v, salida, marca, pais)
    elif isinstance(obj, list):
        for v in obj:
            busca_estaciones(v, salida, marca, pais)


def generico(url: str, marca: str, pais: str):
    """
    Colector por defecto: descarga la pagina, busca JSON incrustado y extrae
    cualquier objeto con coordenadas y precio de diesel.

    Si una cadena no encaja aqui, se le escribe una funcion propia y se
    referencia en CADENAS. Este generico resuelve la mayoria de webs modernas,
    que llevan los datos en __NEXT_DATA__ o en JSON-LD.
    """
    html = traer(url)
    encontradas = []
    for bloque in json_incrustado(html):
        busca_estaciones(bloque, encontradas, marca, pais)
    return encontradas


# ---------------------------------------------------------------- cadenas
# Rellenar 'url' con la pagina que lista las estaciones y sus precios.
# Se comprueba una a una en la primera ejecucion real: la Action deja el
# recuento por cadena en el registro, asi se ve cual funciona y cual no.

CADENAS = [
    # Paises Bajos
    {"marca": "Tango",     "pais": "NL", "url": "https://www.tango.nl/tankstations",   "fn": generico},
    {"marca": "TinQ",      "pais": "NL", "url": "https://www.tinq.nl/tankstations",    "fn": generico},
    {"marca": "Gulf",      "pais": "NL", "url": "https://www.gulf.nl/tankstations",    "fn": generico},
    {"marca": "Kreuze",    "pais": "NL", "url": "https://www.kreuze.nl/tankstations",  "fn": generico},
    # Belgica
    {"marca": "DATS 24",   "pais": "BE", "url": "https://dats24.be/nl/tankstations",   "fn": generico},
    {"marca": "Lukoil",    "pais": "BE", "url": "https://www.lukoil.be/nl/stations",   "fn": generico},
    {"marca": "Maes",      "pais": "BE", "url": "https://www.maesoil.be/nl/stations",  "fn": generico},
]


def main():
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    estaciones, informe = [], []

    for c in CADENAS:
        try:
            r = c["fn"](c["url"], c["marca"], c["pais"])
            for e in r:
                e["source"] = c["marca"]
                e["date"] = hoy
                e["id"] = "%s-%.5f-%.5f" % (c["marca"].lower().replace(" ", ""), e["lat"], e["lon"])
            estaciones.extend(r)
            informe.append(f"{c['marca']} ({c['pais']}): {len(r)}")
        except PermissionError as e:
            informe.append(f"{c['marca']}: SALTADA por robots.txt")
        except Exception as e:
            informe.append(f"{c['marca']}: fallo -> {type(e).__name__}")

    # Deduplicado por coordenada redondeada
    vistas, limpias = set(), []
    for e in estaciones:
        k = (round(e["lat"], 4), round(e["lon"], 4))
        if k in vistas:
            continue
        vistas.add(k)
        limpias.append(e)

    doc = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(limpias),
        "note": "Precios recogidos de las webs de cada cadena. Pueden diferir del surtidor.",
        "stations": limpias,
    }

    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))

    print("=== Recolección " + hoy + " ===")
    for l in informe:
        print(" ", l)
    print("Total tras deduplicar:", len(limpias))
    return 0 if limpias else 1


if __name__ == "__main__":
    sys.exit(main())
