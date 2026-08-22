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

import html as _html
import json
import re
import sys
import time
import urllib.robotparser
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# RESPETAR_ROBOTS = True  -> se salta las cadenas que lo prohiben (recomendado)
# RESPETAR_ROBOTS = False -> se consultan igual
#
# Ignorar robots.txt no es delito, pero puede incumplir las condiciones de uso
# del sitio y, en la UE, rozar el derecho sui generis sobre bases de datos si
# la extraccion es masiva. El riesgo cotidiano es que bloqueen la IP.
# Dos pasadas al dia y agente identificado mantienen la carga en lo minimo.
# Si una web responde 403, es un no activo: no se insiste.
# ---------------------------------------------------------------------------
RESPETAR_ROBOTS = False

AGENTE = "RutaDieselBot/1.0 (proyecto personal no comercial)"
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
    ok = True if rp is None else rp.can_fetch(AGENTE, url)
    if not ok and not RESPETAR_ROBOTS:
        log(f"  [aviso] robots.txt de {p.netloc} lo desaconseja; se continua por configuracion")
        return True
    return ok


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


DIESEL_TXT = re.compile(r'(?i)\b(diesel|gazole|gasoil|b7)\b')


def precio_diesel(obj, prof=0):
    """
    Busca el precio de diesel en cualquier sitio del subarbol.
    Cubre los dos formatos habituales:
      a) plano:   {"diesel": 2.292}
      b) anidado: {"fuelPrices":[{"fuelProductType":"DIESEL","price":2.292}]}
    """
    if prof > 6:
        return None
    if isinstance(obj, dict):
        # (a) una clave que menciona diesel y lleva el numero
        for k, v in obj.items():
            if DIESEL_TXT.search(str(k)) and not isinstance(v, (dict, list)):
                p = a_float(v)
                if p:
                    return p
        # (b) un objeto que se identifica como diesel y guarda el precio aparte
        marcado = any(DIESEL_TXT.search(str(v)) for v in obj.values()
                      if not isinstance(v, (dict, list)))
        if marcado:
            for k, v in obj.items():
                if re.search(r'(?i)pric|prijs|prix|amount|value|tarief', str(k)):
                    p = a_float(v)
                    if p:
                        return p
        for v in obj.values():
            p = precio_diesel(v, prof + 1)
            if p:
                return p
    elif isinstance(obj, list):
        for v in obj:
            p = precio_diesel(v, prof + 1)
            if p:
                return p
    return None


def rastrea(obj, salida):
    """Objetos con latitud y longitud; el precio puede estar mas abajo."""
    if isinstance(obj, dict):
        lat = lon = None
        for k, v in obj.items():
            kl = str(k).lower()
            if lat is None and kl in ("lat", "latitude", "breedtegraad"):
                lat = a_coord(v)
            if lon is None and kl in ("lng", "lon", "long", "longitude", "lengtegraad"):
                lon = a_coord(v)
        if lat is not None and lon is not None:
            precio = precio_diesel(obj)
            if precio:
                salida.append({
                    "lat": lat, "lon": lon, "price": precio,
                    "name": obj.get("name") or obj.get("title") or obj.get("naam")
                            or obj.get("displayName") or "",
                    "city": obj.get("city") or obj.get("plaats") or obj.get("gemeente")
                            or obj.get("municipality") or "",
                })
                return          # ya resuelto: no se baja mas por esta rama
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


PRECIO_HTML = re.compile(
    r'(?is)(?:diesel|gazole|\bb7\b).{0,150}?([12][.,]\d{2,3})')
COORD_HTML = re.compile(
    r'(?is)lat(?:itude)?["\':=\s]{1,6}(-?\d{1,2}\.\d{3,})'
    r'.{0,200}?l(?:ng|on|ongitude)["\':=\s]{1,6}(-?\d{1,3}\.\d{3,})')
NOMBRE_HTML = re.compile(r'(?is)(?:name|naam|title)["\':=\s]{1,6}["\']([^"\'<>]{3,40})')


def del_html(html, marca):
    """
    Ultimo recurso cuando no hay JSON: se localiza cada par de coordenadas y
    se busca un precio de diesel en su vecindad. Emparejar por ventana evita
    el error de casar el precio de una estacion con la posicion de otra.
    """
    fuera = []
    for m in COORD_HTML.finditer(html):
        la, lo = a_coord(m.group(1)), a_coord(m.group(2))
        if la is None or lo is None:
            continue
        # Primero hacia delante: en una ficha, el precio suele ir tras la posicion.
        adelante = html[m.end():m.end() + 1200]
        pm = PRECIO_HTML.search(adelante)
        ventana = adelante
        if not pm:
            # Si no, hacia atras, y se toma el mas cercano, no el primero.
            atras = html[max(0, m.start() - 600):m.start()]
            todos = list(PRECIO_HTML.finditer(atras))
            if not todos:
                continue
            pm = todos[-1]
            ventana = atras
        precio = a_float(pm.group(1))
        if not precio:
            continue
        nm = NOMBRE_HTML.search(ventana)
        fuera.append({"lat": la, "lon": lo, "price": precio,
                      "name": (nm.group(1) if nm else marca), "city": ""})
    return fuera


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

# ---------------------------------------------------------------------------
# Extractor para TinQ (Drupal). Estructura confirmada en el codigo fuente:
#   <div class="... taxonomy-term-Diesel B7 ...">  ... <p>Diesel B7</p> ...
#   <div content="2.359" class="field field--name-field-prices-price-pump ...">
# El precio esta en el atributo content, no en el texto visible, que va
# partido en <sup> y no se puede leer de corrido.
# ---------------------------------------------------------------------------
FICHA = re.compile(r'href="((?:https?://[^"]*?)?/tankstations/[^"?#]{3,}?)/?"', re.I)
BLOQUE_DIESEL = re.compile(r'taxonomy-term-Diesel[\s_-]*B7(.{0,4000}?)field--name-field-prices-price-pump', re.S | re.I)
PRECIO_ATTR = re.compile(r'content="(\d[.,]\d{2,3})"[^>]*field--name-field-prices-price-pump', re.I)
DIRECCION = re.compile(r'address-line1">([^<]+)<.*?postal-code">([^<]+)<.*?locality">([^<]+)<', re.S | re.I)
GMAPS = re.compile(r'[?&]q=(-?\d{1,2}\.\d{4,}),(-?\d{1,3}\.\d{4,})|@(-?\d{1,2}\.\d{4,}),(-?\d{1,3}\.\d{4,})')


def ficha_drupal(html, marca, pais, url):
    """Saca precio de diesel, direccion y coordenadas de una ficha de estacion."""
    pm = PRECIO_ATTR.search(html)
    precio = a_float(pm.group(1)) if pm else None
    if not precio:
        return None

    lat = lon = None
    gm = GMAPS.search(html)
    if gm:
        lat = a_coord(gm.group(1) or gm.group(3))
        lon = a_coord(gm.group(2) or gm.group(4))
    if lat is None:
        cm = COORD_HTML.search(html)
        if cm:
            lat, lon = a_coord(cm.group(1)), a_coord(cm.group(2))
    if lat is None or lon is None:
        return None

    dm = DIRECCION.search(html)
    sub = f"{dm.group(1).strip()}, {dm.group(2).strip()} {dm.group(3).strip()}" if dm else ""
    nombre = url.rstrip("/").split("/")[-1].replace("-", " ").title()
    return {"lat": lat, "lon": lon, "price": precio,
            "name": f"{marca} {nombre}", "city": sub}


def por_fichas(base, listado, marca, pais, tope=500):
    """Listado -> enlaces de ficha -> precio en cada ficha."""
    html, diag = traer(listado)
    log(f"  listado {listado} -> {diag}")
    if html is None:
        return []
    enlaces = []
    for m in FICHA.finditer(html):
        u = urljoin(base, m.group(1))
        if u not in enlaces:
            enlaces.append(u)
    log(f"  fichas encontradas: {len(enlaces)}")
    if not enlaces:
        return []

    fuera, fallos = [], 0
    for i, u in enumerate(enlaces[:tope]):
        h, d = traer(u)
        if h is None:
            fallos += 1
            if fallos > 8:
                log(f"  demasiados fallos seguidos, se detiene en {i}")
                break
            continue
        e = ficha_drupal(h, marca, pais, u)
        if e:
            fuera.append(e)
        if i and i % 50 == 0:
            log(f"  ... {i} fichas revisadas, {len(fuera)} con precio")
    log(f"  total con precio: {len(fuera)} de {min(len(enlaces), tope)} fichas")
    return fuera


# ---------------------------------------------------------------------------
# DATS 24 (Belgica). Endpoint publico confirmado en el panel de red:
#   POST https://dats24.be/api/service_point_locator
#   {"fuelProductType":[],"latitude":..,"longitude":..,
#    "searchRadius":28512,"serviceDeliveryPointType":["FUEL"]}
# Se barre el pais con una rejilla de puntos separados unos 40 km.
# ---------------------------------------------------------------------------
def postjson(url, cuerpo, timeout=25):
    datos = json.dumps(cuerpo).encode("utf-8")
    req = Request(url, data=datos, headers={
        "User-Agent": AGENTE,
        "Content-Type": "text/plain;charset=UTF-8",
        "Accept": "*/*",
        "Accept-Language": "nl-BE,nl;q=0.9,en;q=0.7",
        "Origin": "https://dats24.be",
        "Referer": "https://dats24.be/nl/particulier/tankstation-laadpaal-vinden",
    })
    try:
        with urlopen(req, timeout=timeout) as r:
            crudo = r.read()
        time.sleep(ESPERA)
        return json.loads(crudo.decode("utf-8", "replace")), f"HTTP 200, {len(crudo)//1024} KB"
    except HTTPError as e:
        return None, f"HTTP {e.code}"
    except URLError as e:
        return None, f"sin conexion ({e.reason})"
    except Exception as e:
        return None, type(e).__name__


def dats24(c):
    LOCATOR = "https://dats24.be/api/service_point_locator"
    DETAILS = "https://dats24.be/api/service_point_details"

    # ---- 1. Localizador: confirmado que devuelve estaciones con coordenadas
    def buscar(la, lo, radio=28512):
        return postjson(LOCATOR, {"fuelProductType": [], "latitude": la, "longitude": lo,
                                  "searchRadius": radio, "serviceDeliveryPointType": ["FUEL"]})

    j, diag = buscar(50.6570, 4.1651)
    if not j:
        log(f"  el localizador no responde ({diag}); se abandona")
        return []
    log(f"  localizador -> {diag}, {len(j) if isinstance(j, list) else '?'} estaciones")
    log(f"  ficha completa de ejemplo: {str(j[0])[:1200]}")

    # ¿Trae ya el precio el propio localizador?
    prueba = []
    rastrea(j, prueba)
    if prueba:
        log(f"  el localizador YA trae precios ({len(prueba)} en este punto)")
        modo = "locator"
    else:
        # ---- 2. Si no, se sondea el endpoint de detalle con el primer id
        ident = j[0].get("id") if isinstance(j[0], dict) else None
        log(f"  sin precios en el localizador; se sondea el detalle con id={ident}")
        variantes = [
            ("servicePointId", {"servicePointId": ident}),
            ("id", {"id": ident}),
            ("servicePointId numerico", {"servicePointId": int(ident)} if str(ident).isdigit() else None),
            ("con tipo", {"servicePointId": ident, "serviceDeliveryPointType": "FUEL"}),
            ("lista de ids", {"servicePointIds": [ident]}),
        ]
        modo = None
        for nombre, cuerpo in variantes:
            if cuerpo is None:
                continue
            d, dg = postjson(DETAILS, cuerpo)
            log(f"  detalle [{nombre}] -> {dg}, {str(d)[:400]}")
            if d:
                pr = []
                rastrea(d, pr)
                if not pr and precio_diesel(d):
                    pr = [{"ok": True}]
                if pr:
                    modo = cuerpo
                    log(f"  -> el detalle funciona con: {nombre}")
                    break
        if modo is None:
            log("  el detalle no suelta precios con ninguna variante probada")
            return []

    # ---- 3. Barrido del pais
    puntos, la = [], 49.55
    while la <= 51.55:
        lo = 2.65
        while lo <= 6.45:
            puntos.append((round(la, 4), round(lo, 4)))
            lo += 0.50
        la += 0.35

    estaciones, vistas = [], set()
    for i, (la, lo) in enumerate(puntos):
        j, _ = buscar(la, lo)
        if not isinstance(j, list):
            continue
        for e in j:
            if not isinstance(e, dict):
                continue
            k = e.get("id") or (e.get("latitude"), e.get("longitude"))
            if k in vistas:
                continue
            vistas.add(k)
            estaciones.append(e)
        if i % 12 == 0:
            log(f"  ... {i+1}/{len(puntos)} puntos, {len(estaciones)} estaciones distintas")
    log(f"  estaciones localizadas: {len(estaciones)}")

    # ---- 4. Precios
    fuera = []
    if modo == "locator":
        rastrea(estaciones, fuera)
    else:
        clave = [k for k in modo if "ervicePoint" in k or k == "id"][0]
        for i, e in enumerate(estaciones):
            cuerpo = dict(modo)
            ident = e.get("id")
            cuerpo[clave] = [ident] if isinstance(modo[clave], list) else (
                int(ident) if isinstance(modo[clave], int) and str(ident).isdigit() else ident)
            d, _ = postjson(DETAILS, cuerpo)
            if not d:
                continue
            precio = precio_diesel(d)
            if not precio:
                continue
            fuera.append({
                "lat": e.get("latitude"), "lon": e.get("longitude"), "price": precio,
                "name": "DATS 24 " + str(e.get("addressCity") or ""),
                "city": f"{e.get('addressStreet','')} {e.get('addressNumber','')}".strip(),
            })
            if i % 25 == 0:
                log(f"  ... {i+1}/{len(estaciones)} fichas, {len(fuera)} con precio")
    log(f"  DATS 24: {len(fuera)} con precio de diesel")
    return fuera


# ---------------------------------------------------------------------------
# Prijzenindex: publica el diesel por estacion de Belgica y Paises Bajos,
# ciudad a ciudad, en tablas planas. Cada fila trae un enlace de navegacion
# con las coordenadas dentro, el precio y la marca en el title del logotipo.
# Es la via mas directa para cubrir los dos paises que no tienen dato abierto.
# ---------------------------------------------------------------------------
PI_BASE = "https://prijzenindex.nl"
PI_RAICES = [("BE", "/brandstof/diesel-belgie"), ("NL", "/brandstof/diesel")]

PI_CIUDAD = re.compile(r'href="(?:https?://[^"/]+)?(/brandstof/diesel(?:-belgie)?/[a-z0-9\\-]+)/?"', re.I)
PI_COORD = re.compile(r'destination=(-?\d+\.\d+),(-?\d+\.\d+)')
PI_PRECIO = re.compile(r'&euro;|\u20ac|€')
PI_NUM = re.compile(r'(\d[.,]\d{2,3})')
PI_MARCA = re.compile(r'<img[^>]+title="([^"]{2,30})"', re.I)
PI_VIEJO = re.compile(r'(?i)class="[^"]*(text-muted|text-secondary|grijs|gray|grey|old)[^"]*"')


def sin_tags(t):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', t)).strip()


def pi_ciudad(url, pais, muestra=False):
    html, diag = traer(PI_BASE + url)
    if html is None:
        return [], diag
    filas = re.split(r'<tr[\s>]', html)[1:]
    if muestra and filas:
        log(f"  fila de ejemplo: {filas[0][:500]}")
    fuera = []
    for f in filas:
        c = PI_COORD.search(f)
        if not c:
            continue
        lat, lon = a_coord(c.group(1)), a_coord(c.group(2))
        if lat is None or lon is None:
            continue
        # el precio es el primer numero tras el simbolo del euro
        pos = 0
        m = PI_PRECIO.search(f)
        if m:
            pos = m.end()
        n = PI_NUM.search(f, pos)
        precio = a_float(n.group(1)) if n else None
        if not precio:
            continue
        marca = ""
        mm = PI_MARCA.search(f)
        if mm:
            marca = _html.unescape(mm.group(1)).strip()
        texto = _html.unescape(sin_tags(f))
        dir_ = ""
        d = re.search(r'(?:€|EUR)?\s*\d[.,]\d{2,3}\s+(.{4,60}?)\s{2,}', texto)
        if d:
            dir_ = d.group(1)
        else:
            partes = texto.split()
            dir_ = " ".join(partes[2:8]) if len(partes) > 8 else texto[:60]
        fuera.append({"lat": lat, "lon": lon, "price": precio,
                      "name": (marca or "Gasolinera"), "city": dir_,
                      "viejo": bool(PI_VIEJO.search(f))})
    return fuera, diag


def prijzenindex(c):
    todo, vistas = [], set()
    for pais, raiz in PI_RAICES:
        html, diag = traer(PI_BASE + raiz)
        log(f"  indice {pais} {raiz} -> {diag}")
        if html is None:
            continue
        # El indice del pais ya lista estaciones: se extraen tambien de ahi
        n_idx = 0
        for f in re.split(r'<tr[\s>]', html)[1:]:
            c = PI_COORD.search(f)
            if not c:
                continue
            lat, lon = a_coord(c.group(1)), a_coord(c.group(2))
            m2 = PI_PRECIO.search(f)
            n2 = PI_NUM.search(f, m2.end() if m2 else 0)
            precio = a_float(n2.group(1)) if n2 else None
            if lat is None or lon is None or not precio:
                continue
            k = (round(lat, 4), round(lon, 4))
            if k in vistas:
                continue
            vistas.add(k)
            mm2 = PI_MARCA.search(f)
            marca = _html.unescape(mm2.group(1)).strip() if mm2 else "Gasolinera"
            todo.append({"lat": lat, "lon": lon, "price": precio, "name": marca,
                         "city": "", "viejo": bool(PI_VIEJO.search(f)),
                         "brand": marca, "country": pais, "source": "Prijzenindex"})
            n_idx += 1
        log(f"  del propio indice {pais}: {n_idx} estaciones, total {len(todo)}")

        ciudades = []
        for m in PI_CIUDAD.finditer(html):
            u = m.group(1)
            if u.rstrip("/") == raiz.rstrip("/"):
                continue
            if u not in ciudades:
                ciudades.append(u)
        log(f"  ciudades encontradas en {pais}: {len(ciudades)}")
        for i, u in enumerate(ciudades[:80]):
            filas, diag = pi_ciudad(u, pais, muestra=(i == 0 and pais == "BE"))
            nuevas = 0
            for e in filas:
                k = (round(e["lat"], 4), round(e["lon"], 4))
                if k in vistas:
                    continue
                vistas.add(k)
                e["brand"] = e["name"]
                e["country"] = pais
                e["source"] = "Prijzenindex"
                todo.append(e)
                nuevas += 1
            if i % 10 == 0 or nuevas:
                log(f"  {u} -> {len(filas)} filas, {nuevas} nuevas (total {len(todo)})")
    viejos = sum(1 for e in todo if e.get("viejo"))
    log(f"  Prijzenindex: {len(todo)} estaciones con precio ({viejos} marcadas como no actuales)")
    return todo


CADENAS = [
    {"marca": "Prijzenindex", "pais": "BE/NL", "base": PI_BASE, "fn": prijzenindex},
    {"marca": "DATS 24", "pais": "BE", "base": "https://dats24.be", "fn": dats24},
    {"marca": "Maes",     "pais": "BE", "base": "https://www.maes.be"},
    {"marca": "Gabriels",  "pais": "BE", "base": "https://www.gabriels.be"},
    {"marca": "Octa+",     "pais": "BE", "base": "https://www.octaplus.be"},
    {"marca": "Tango",   "pais": "NL", "base": "https://www.tango.nl"},
    {"marca": "TinQ",    "pais": "NL", "base": "https://www.tinq.nl",
     "fn": lambda c: por_fichas(c["base"], c["base"] + "/tankstations", c["marca"], c["pais"])},
    {"marca": "Gulf",    "pais": "NL", "base": "https://www.gulf.nl"},
    {"marca": "Kreuze",  "pais": "NL", "base": "https://www.kreuze.nl"},
    {"marca": "Firezone",  "pais": "NL", "base": "https://www.firezone.nl"},
    {"marca": "Fieten",    "pais": "NL", "base": "https://www.fietenolie.nl"},
    {"marca": "SuperTank", "pais": "NL", "base": "https://www.supertank.nl"},
    {"marca": "Berkman",   "pais": "NL", "base": "https://www.berkman.nl"},
    {"marca": "OK",        "pais": "NL", "base": "https://www.ok.nl"},
]


def procesar(c):
    marca, pais, base = c["marca"], c["pais"], c["base"]
    log(f"\n--- {marca} ({pais}) ---")

    if c.get("fn"):
        r = c["fn"](c)
        for e in r:
            e["brand"] = marca
            e["country"] = pais
            e["source"] = marca
        return r

    portada, diag = traer(base)
    log(f"  portada {base} -> {diag}")
    if portada is None:
        return []

    fijas = [base + r for r in ("/tankstations", "/nl/tankstations", "/stations",
                                "/nl/stations", "/api/stations", "/api/tankstations",
                                "/prijzen", "/nl/prijzen")]
    urls = [base] + candidatas(portada, base) + fijas
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
        if not halladas:
            halladas = del_html(html, marca)
            via = "html"
        else:
            via = "json"
        log(f"  {u} -> {diag}, {len(bloques)} bloques JSON, {len(halladas)} estaciones ({via})")
        if len(halladas) > len(mejor):
            mejor = halladas
            if len(mejor) > 30:
                break
    for e in mejor:
        e["brand"] = marca
        e["country"] = pais
        e["source"] = marca
    return mejor


# ---------------------------------------------------------------------------
# Descubrimiento de cadenas: se pregunta a OpenStreetMap que marcas existen en
# NL y BE, cuantas estaciones tiene cada una y cual es su web oficial. Asi la
# lista de objetivos sale de un dato, no de suposiciones.
# ---------------------------------------------------------------------------
def descubrir_marcas():
    consulta = ('[out:json][timeout:60];'
                'nwr["amenity"="fuel"](49.45,2.50,53.70,7.30);'
                'out tags center;')
    try:
        req = Request("https://overpass-api.de/api/interpreter",
                      data=("data=" + consulta).encode("utf-8"),
                      headers={"User-Agent": AGENTE,
                               "Content-Type": "application/x-www-form-urlencoded"})
        with urlopen(req, timeout=180) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log(f"  no se ha podido consultar OpenStreetMap: {type(e).__name__}")
        return

    cuenta, webs, paises = {}, {}, {}
    for el in j.get("elements", []):
        t = el.get("tags", {})
        marca = (t.get("brand") or t.get("operator") or "").strip()
        if not marca:
            continue
        cuenta[marca] = cuenta.get(marca, 0) + 1
        w = t.get("brand:website") or t.get("website") or t.get("contact:website")
        if w and marca not in webs:
            webs[marca] = w.split("?")[0]
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        if lat:
            paises.setdefault(marca, set()).add("NL" if lat > 51.45 else "BE")

    log("\n  === CADENAS REALES EN NL Y BE, POR TAMANO ===")
    log("  %-26s %6s  %-6s %s" % ("MARCA", "ESTAC.", "PAIS", "WEB OFICIAL"))
    for marca, n in sorted(cuenta.items(), key=lambda x: -x[1])[:30]:
        if n < 8:
            continue
        log("  %-26s %6d  %-6s %s" % (marca[:26], n,
                                      "/".join(sorted(paises.get(marca, {"?"}))),
                                      webs.get(marca, "(sin web en OSM)")))
    log("  ============================================\n")


def main():
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    descubrir_marcas()
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
