#!/usr/bin/env python3
"""
Recolector de precios de diesel por estacion — Paises Bajos y Belgica. v2

Novedades frente a la v1:
  - No hace falta acertar la URL: parte de la portada de cada cadena y
    busca sola los enlaces que huelen a listado de estaciones.
  - Prueba varias direcciones candidatas y cuenta que pasa en cada una.
  - Nunca termina con error: siempre escribe precios.json, aunque salga
    vacio, para que el resto del proceso siga.
  - Deja un diagnostico linea a linea: codigo HTTP, tamano, bloques JSON
    encontrados y estaciones extraidas. Con eso se afina sin adivinar.

Normas: se respeta robots.txt, agente identificado y dos pasadas al dia.
"""

import json
import re
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

AGENTE = "RutaDieselBot/1.0 (proyecto no comercial)"
ESPERA = 1.5
SALIDA = "precios.json"
LOG = []

_robots = {}


def log(msg):
    LOG.append(msg)
    print(msg, flush=True)


# ------------------------------------------------------------------ red

def permitido(url):
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    if base not in _robots:
        rp = urllib.robotparser.RobotFileParser()
        rp.set_url(base + "/robots.txt")
        try:
            rp.read()
        except Exception:
            rp = None
        _robots[base] = rp
    rp = _robots[base]
    return True if rp is None else rp.can_fetch(AGENTE, url)


def traer(url, timeout=25):
    """Devuelve (html, diagnostico). html es None si no se pudo."""
    if not permitido(url):
        return None, "robots.txt lo prohibe"
    req = Request(url, headers={
        "User-Agent": AGENTE,
        "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
        "Accept-Language": "nl-NL,nl;q=0.9,en;q=0.6",
        "Accept-Encoding": "identity",
    })
    try:
        with urlopen(req, timeout=timeout) as r:
            datos = r.read()
            code = r.getcode()
    except HTTPError as e:
        return None, f"HTTP {e.code}"
    except URLError as e:
        return None, f"sin conexion ({e.reason})"
    except Exception as e:
        return None, type(e).__name__
    time.sleep(ESPERA)
    return datos.decode("utf-8", "replace"), f"HTTP {code}, {len(datos)//1024} KB"


# ------------------------------------------------------------ extraccion

def a_float(v):
    try:
        f = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return f if 0.8 < f < 4 else None


def bloques_json(html):
    """JSON incrustado: Next.js, Nuxt, JSON-LD, estados iniciales."""
    out = []
    patrones = (
        r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>',
        r'window\.__NUXT__\s*=\s*(\{.*?\});?\s*</script>',
        r'window\.__INITIAL_STATE__\s*=\s*(\{.*?\});',
        r'<script[^>]+type="application/json"[^>]*>(\{.*?\})</script>',
    )
    for pat in patrones:
        for m in re.finditer(pat, html, re.S | re.I):
            txt = m.group(1).strip()
            try:
                out.append(json.loads(txt))
            except Exception:
                pass
    # La respuesta puede ser JSON puro (una API)
    t = html.strip()
    if t[:1] in "[{":
        try:
            out.append(json.loads(t))
        except Exception:
            pass
    return out


def rastrea(obj, salida):
    """Busca objetos con latitud, longitud y algun precio de diesel."""
    if isinstance(obj, dict):
        lat = lon = precio = None
        for k, v in obj.items():
            kl = str(k).lower()
            if lat is None and kl in ("lat", "latitude", "breedtegraad"):
                lat = a_coord(v)
            if lon is None and kl in ("lng", "lon", "long", "longitude", "lengtegraad"):
                lon = a_coord(v)
            if precio is None and not isinstance(v, (dict, list)):
                if ("diesel" in kl or "gazole" in kl or "b7" in kl) and "prijs" not in kl:
                    precio = a_float(v)
                elif "diesel" in kl and "prijs" in kl:
                    precio = a_float(v)
        if lat is not None and lon is not None and precio:
            salida.append({
                "lat": lat, "lon": lon, "price": precio,
                "name": obj.get("name") or obj.get("title") or obj.get("naam") or "",
                "city": obj.get("city") or obj.get("plaats") or obj.get("gemeente") or "",
            })
        for v in obj.values():
            rastrea(v, salida)
    elif isinstance(obj, list):
        for v in obj:
            rastrea(v, salida)


def a_coord(v):
    try:
        f = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return f if -180 <= f <= 180 and f != 0 else None


PALABRAS = ("tankstation", "stations", "station", "vestiging", "prijzen",
            "prijs", "locaties", "verkooppunt", "pompen")


def candidatas(portada_html, base):
    """Enlaces de la portada que parecen listados de estaciones."""
    urls = []
    for m in re.finditer(r'href="([^"#]+)"', portada_html, re.I):
        h = m.group(1)
        hl = h.lower()
        if any(p in hl for p in PALABRAS):
            u = urljoin(base, h)
            if urlparse(u).netloc == urlparse(base).netloc and u not in urls:
                urls.append(u)
    # Las mas cortas primero: suelen ser el listado, no una ficha suelta
    urls.sort(key=len)
    return urls[:4]


# ------------------------------------------------------------- cadenas

CADENAS = [
    {"marca": "Tango",   "pais": "NL", "base": "https://www.tango.nl"},
    {"marca": "TinQ",    "pais": "NL", "base": "https://www.tinq.nl"},
    {"marca": "Gulf",    "pais": "NL", "base": "https://www.gulf.nl"},
    {"marca": "Kreuze",  "pais": "NL", "base": "https://www.kreuze.nl"},
    {"marca": "DATS 24", "pais": "BE", "base": "https://www.dats24.be"},
    {"marca": "Lukoil",  "pais": "BE", "base": "https://lukoil.be"},
    {"marca": "Maes",    "pais": "BE", "base": "https://www.maesoil.be"},
]


def procesar(c):
    marca, pais, base = c["marca"], c["pais"], c["base"]
    log(f"\n--- {marca} ({pais}) ---")

    portada, diag = traer(base)
    log(f"  portada {base} -> {diag}")
    if portada is None:
        return []

    urls = [base] + candidatas(portada, base)
    log(f"  candidatas: {', '.join(u.replace(base,'') or '/' for u in urls)}")

    mejor = []
    for u in urls:
        html, diag = traer(u)
        if html is None:
            log(f"  {u} -> {diag}")
            continue
        bloques = bloques_json(html)
        halladas = []
        for b in bloques:
            rastrea(b, halladas)
        log(f"  {u} -> {diag}, {len(bloques)} bloques JSON, {len(halladas)} estaciones")
        if len(halladas) > len(mejor):
            mejor = halladas
            if len(mejor) > 30:
                break
    for e in mejor:
        e["brand"] = marca
        e["country"] = pais
        e["source"] = marca
    return mejor


def main():
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    todas = []
    for c in CADENAS:
        try:
            todas.extend(procesar(c))
        except Exception as e:
            log(f"  ERROR inesperado en {c['marca']}: {type(e).__name__}: {e}")

    vistas, limpias = set(), []
    for e in todas:
        k = (round(e["lat"], 4), round(e["lon"], 4))
        if k in vistas:
            continue
        vistas.add(k)
        e["date"] = hoy
        e["id"] = "%s-%.5f-%.5f" % (e["brand"].lower().replace(" ", ""), e["lat"], e["lon"])
        limpias.append(e)

    doc = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(limpias),
        "note": "Precios recogidos de las webs de cada cadena. Pueden diferir del surtidor.",
        "stations": limpias,
    }
    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))

    log("\n=== RESUMEN " + hoy + " ===")
    log(f"Estaciones con precio: {len(limpias)}")
    for pais in ("NL", "BE"):
        n = sum(1 for e in limpias if e["country"] == pais)
        log(f"  {pais}: {n}")
    return 0        # nunca falla: el archivo se escribe siempre


if __name__ == "__main__":
    sys.exit(main())
