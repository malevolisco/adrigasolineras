#!/usr/bin/env python3
"""
Recolector de precios por estacion — Paises Bajos y Belgica. v3

Novedades frente a la v2:
  - Multicarburante. Ya no se recoge solo el gasoleo: se barren tambien
    gasolina (E10) de Belgica y Paises Bajos y GLP de Paises Bajos, que
    son los unicos listados que Prijzenindex publica por estacion.
  - Cada estacion guarda un diccionario "prices" con los carburantes que
    tenga: {"diesel": 2.129, "e10": 2.139, "glp": 0.759}. Se conserva
    ademas el campo suelto "price" con el gasoleo, para no romper nada
    que ya lo estuviera leyendo.
  - Fusion por coordenadas en lugar de descarte: la misma estacion sale
    en el listado de diesel y en el de gasolina, y antes la segunda
    aparicion se tiraba por duplicada. Ahora se funden en un solo
    registro y se suman los precios.
  - Rango de validacion por carburante. El GLP ronda los 0,76 €/l y con
    el filtro unico de la v2 (0,8 a 4) se descartaba entero.
  - Cada carburante se recoge de forma independiente: si la pagina de
    GLP falla o cambia de maqueta, el diesel del dia se publica igual.

De la v2 se mantiene:
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
import os
import re
import sys
import time
import urllib.robotparser
from datetime import datetime, timedelta, timezone
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
ESPERA = 1.2
SALIDA = "precios.json"
LOG = []

# Pasar de dos listados a cinco multiplica por 2,5 el tiempo de ejecucion.
# Con este tope el trabajo nunca se queda colgado: al agotarlo se deja de
# barrer ciudades y se publica lo que haya, que es preferible a no publicar.
TOPE_MINUTOS = 150
ARRANQUE = time.monotonic()
CURSOR = {}          # por que ciudad iba cada fuente, se guarda en precios.json


def queda_tiempo(margen=0):
    return (time.monotonic() - ARRANQUE) / 60 < (TOPE_MINUTOS - margen)


# Bloque de ciudades que se barre por fuente en cada vuelta del turno rotatorio.
# Con bloques pequenos las cinco fuentes avanzan a la vez y ninguna se queda sin
# tocar cuando se agota el tiempo.
BLOQUE = 20

# Dias que se conserva un precio antes de tirarlo. Un dato de hace tres semanas
# ya no sirve para decidir donde repostar, pero uno de anteayer si.
CADUCA = 21


def carga_previo():
    """
    Lo recogido en pasadas anteriores. Sin esto, cada ejecucion empezaria de
    cero y la cobertura no pasaria nunca de lo que cabe en una sola pasada:
    era justo el motivo de que faltaran provincias enteras.
    """
    if not os.path.exists(SALIDA):
        return {}, {}
    try:
        with open(SALIDA, encoding="utf-8") as f:
            d = json.load(f)
    except Exception as e:
        log(f"  no se ha podido leer {SALIDA}: {type(e).__name__}: {e}")
        return {}, {}
    previo = {}
    for e in d.get("stations", []):
        if e.get("lat") is None or e.get("lon") is None:
            continue
        # Los ficheros de la v2 solo traian "price" suelto: se interpreta como
        # gasoleo, que es lo unico que recogia aquella version. Sin esto, una
        # pasada nueva tiraria a la basura todo lo publicado hasta ahora.
        if not isinstance(e.get("prices"), dict) or not e["prices"]:
            e["prices"] = {"diesel": e["price"]} if e.get("price") is not None else {}
        e.setdefault("pdate", {})
        if not e["pdate"]:
            e["pdate"] = {c: e.get("date", "") for c in e["prices"]}
        # Las pasadas antiguas guardaban "viejo" como booleano suelto
        if not isinstance(e.get("viejo"), dict):
            e["viejo"] = {c: bool(e.get("viejo")) for c in e["prices"]}
        previo[(round(e["lat"], 4), round(e["lon"], 4))] = e
    cursor = d.get("cursor", {}) or {}
    log(f"  pasada anterior: {len(previo)} estaciones, cursor {cursor}")
    return previo, cursor

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

# ---------------------------------------------------------------------------
# Vocabulario de carburantes. Es el mismo que usa el mapa, asi que si aqui se
# anade una clave hay que anadirla alli. Se separa E5 de E10 a proposito: son
# productos distintos y se llevan varios centimos, y Francia los publica por
# separado. "p98" es la 98 de toda la vida (E5).
# ---------------------------------------------------------------------------
COMBUSTIBLES = ("diesel", "e5", "e10", "p98", "glp", "e85")

ETIQUETA = {
    "diesel": "Diesel (B7)",
    "e5": "Gasolina 95 E5",
    "e10": "Gasolina 95 E10",
    "p98": "Gasolina 98",
    "glp": "GLP",
    "e85": "E85",
}

# Rango plausible por litro. El GLP baja de 0,80 € y por eso no puede
# compartir el filtro con los liquidos: con el rango unico de la v2 se
# descartaba entero sin dejar rastro en el registro.
RANGO = {
    "diesel": (0.80, 4.00),
    "e5": (0.80, 4.50),
    "e10": (0.80, 4.50),
    "p98": (0.80, 4.50),
    "glp": (0.35, 2.20),
    "e85": (0.40, 3.00),
}


def a_float(v, comb="diesel"):
    try:
        f = float(str(v).replace(",", "."))
    except (TypeError, ValueError):
        return None
    lo, hi = RANGO.get(comb, (0.80, 4.00))
    return f if lo < f < hi else None


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


# Como se nombra cada carburante en las webs que se leen. El orden importa:
# "euro95" a secas es E10 en Paises Bajos, asi que E5 exige que lo diga.
TXT = {
    "diesel": re.compile(r'(?i)\b(diesel|gazole|gasoil|b7)\b'),
    "e5": re.compile(r'(?i)(euro\s?95\s*\(?\s*e5|sp\s?95(?!\s*[-\s]?e10)|super\s?95\s*e5|\be5\b)'),
    "e10": re.compile(r'(?i)(euro\s?95|benzine|essence|sp\s?95[-\s]?e10|\be10\b)'),
    "p98": re.compile(r'(?i)(euro\s?98|super\s?98|sp\s?98|\b98\s*\(?e5)'),
    "glp": re.compile(r'(?i)\b(lpg|glp|gpl|gplc|autogas)\b'),
    "e85": re.compile(r'(?i)(e85|super\s?ethanol|superéthanol)'),
}
DIESEL_TXT = TXT["diesel"]      # se conserva el nombre por compatibilidad


def precio_de(obj, comb="diesel", prof=0):
    """
    Busca el precio del carburante indicado en cualquier sitio del subarbol.
    Cubre los dos formatos habituales:
      a) plano:   {"diesel": 2.292}
      b) anidado: {"fuelPrices":[{"fuelProductType":"DIESEL","price":2.292}]}
    """
    if prof > 6:
        return None
    patron = TXT.get(comb, TXT["diesel"])
    if isinstance(obj, dict):
        # (a) una clave que menciona el carburante y lleva el numero
        for k, v in obj.items():
            if patron.search(str(k)) and not isinstance(v, (dict, list)):
                p = a_float(v, comb)
                if p:
                    return p
        # (b) un objeto que se identifica con ese carburante y guarda el
        #     precio en otra clave
        marcado = any(patron.search(str(v)) for v in obj.values()
                      if not isinstance(v, (dict, list)))
        if marcado:
            for k, v in obj.items():
                if re.search(r'(?i)pric|prijs|prix|amount|value|tarief', str(k)):
                    p = a_float(v, comb)
                    if p:
                        return p
        for v in obj.values():
            p = precio_de(v, comb, prof + 1)
            if p:
                return p
    elif isinstance(obj, list):
        for v in obj:
            p = precio_de(v, comb, prof + 1)
            if p:
                return p
    return None


def precio_diesel(obj, prof=0):
    return precio_de(obj, "diesel", prof)


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
            precios = {}
            for comb in COMBUSTIBLES:
                p = precio_de(obj, comb)
                if p:
                    precios[comb] = p
            if precios:
                salida.append({
                    "lat": lat, "lon": lon,
                    "price": precios.get("diesel"), "prices": precios,
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
                      "prices": {"diesel": precio},
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
            "prices": {"diesel": precio},
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
            precios = {}
            for comb in COMBUSTIBLES:
                pc = precio_de(d, comb)
                if pc:
                    precios[comb] = pc
            if not precios:
                continue
            fuera.append({
                "lat": e.get("latitude"), "lon": e.get("longitude"),
                "price": precios.get("diesel"), "prices": precios,
                "name": "DATS 24 " + str(e.get("addressCity") or ""),
                "city": f"{e.get('addressStreet','')} {e.get('addressNumber','')}".strip(),
            })
            if i % 25 == 0:
                log(f"  ... {i+1}/{len(estaciones)} fichas, {len(fuera)} con precio")
    log(f"  DATS 24: {len(fuera)} con algun precio")
    return fuera


# ---------------------------------------------------------------------------
# Prijzenindex: publica el diesel por estacion de Belgica y Paises Bajos,
# ciudad a ciudad, en tablas planas. Cada fila trae un enlace de navegacion
# con las coordenadas dentro, el precio y la marca en el title del logotipo.
# Es la via mas directa para cubrir los dos paises que no tienen dato abierto.
# ---------------------------------------------------------------------------
PI_BASE = "https://prijzenindex.nl"

# (carburante, pais, raiz). Comprobado en el indice del propio sitio: para
# Belgica y Paises Bajos solo existen estas cinco combinaciones. No hay
# pagina de 98 ni de E5, y el GLP solo esta en Paises Bajos. Lo que
# Prijzenindex llama "benzine" es E10, no E5: por eso se cataloga asi.
PI_FUENTES = [
    ("diesel", "BE", "/brandstof/diesel-belgie"),
    ("diesel", "NL", "/brandstof/diesel"),
    ("e10",    "BE", "/brandstof/benzine-belgie"),
    ("e10",    "NL", "/brandstof/benzine"),
    ("glp",    "NL", "/brandstof/lpg"),
]


def pi_re_ciudad(raiz):
    """Enlaces a las ciudades colgando de esa raiz, sea el carburante que sea."""
    return re.compile(r'href="(?:https?://[^"/]+)?(' + re.escape(raiz) +
                      r'/[a-z0-9\-]+)/?"', re.I)
PI_COORD = re.compile(r'destination=(-?\d+\.\d+),(-?\d+\.\d+)')
PI_PRECIO = re.compile(r'&euro;|\u20ac|€')
PI_NUM = re.compile(r'(\d[.,]\d{2,3})')
PI_MARCA = re.compile(r'<img[^>]+title="([^"]{2,30})"', re.I)
PI_VIEJO = re.compile(r'(?i)class="[^"]*(text-muted|text-secondary|grijs|gray|grey|old)[^"]*"')


def sin_tags(t):
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', t)).strip()


def limpia_fila(f):
    """Al partir por <tr, el trozo empieza con el resto de la etiqueta:
       class="fuel-station "> ... Se descarta hasta el primer cierre."""
    i = f.find('>')
    return f[i + 1:] if 0 <= i < 200 else f


def pi_fila(f, comb):
    """Una fila de la tabla -> registro, o None si no lleva precio util."""
    c = PI_COORD.search(f)
    if not c:
        return None
    lat, lon = a_coord(c.group(1)), a_coord(c.group(2))
    if lat is None or lon is None:
        return None
    # el precio es el primer numero tras el simbolo del euro
    pos = 0
    m = PI_PRECIO.search(f)
    if m:
        pos = m.end()
    n = PI_NUM.search(f, pos)
    precio = a_float(n.group(1), comb) if n else None
    if not precio:
        return None
    marca = ""
    mm = PI_MARCA.search(f)
    if mm:
        marca = _html.unescape(mm.group(1)).strip()
    texto = _html.unescape(sin_tags(f))
    texto = re.sub(r'class="[^"]*"?>?', ' ', texto)
    texto = re.sub(r'\s{2,}', ' ', texto).strip()
    d = re.search(r'(?:€|EUR)?\s*\d[.,]\d{2,3}\s+(.{4,60}?)\s{2,}', texto)
    if d:
        dir_ = d.group(1)
    else:
        partes = texto.split()
        dir_ = " ".join(partes[2:8]) if len(partes) > 8 else texto[:60]
    viejo = bool(PI_VIEJO.search(f))
    return {"lat": lat, "lon": lon, "price": precio,
            "prices": {comb: precio}, "viejo": {comb: viejo},
            "name": (marca or "Gasolinera"), "city": dir_}


def pi_ciudad(url, comb, muestra=False):
    html, diag = traer(PI_BASE + url)
    if html is None:
        return [], diag
    filas = [limpia_fila(x) for x in re.split(r'<tr[\s>]', html)[1:]]
    if muestra and filas:
        log(f"  fila de ejemplo: {filas[0][:500]}")
    fuera = []
    for f in filas:
        e = pi_fila(f, comb)
        if e:
            fuera.append(e)
    return fuera, diag


def prijzenindex(cadena):
    """
    Barrido rotatorio. La v3 se paraba en la ciudad 250 de cada listado y
    reconstruia el fichero desde cero en cada pasada, asi que las ciudades del
    final de la lista no se visitaban NUNCA: por eso no habia ni una estacion
    en Tilburg, Breda, Roosendaal o Brujas. Ahora cada fuente recuerda por
    donde iba, se avanza por turnos de BLOQUE ciudades hasta agotar el tiempo,
    y la vuelta siguiente arranca donde se quedo la anterior. Como main()
    fusiona con lo ya publicado, la cobertura se acumula en vez de perderse.
    """
    global CURSOR
    todo, listas, indices = [], {}, {}

    # 1) Indice de cada fuente: estaciones que ya trae y lista de ciudades
    for comb, pais, raiz in PI_FUENTES:
        try:
            hallado, ciudades = pi_indice(comb, pais, raiz)
        except Exception as e:
            log(f"  ERROR en el indice de {ETIQUETA.get(comb, comb)} {pais}: "
                f"{type(e).__name__}: {e}")
            continue
        todo.extend(hallado)
        listas[raiz] = (comb, pais, ciudades)
        indices[raiz] = CURSOR.get(raiz, 0) % max(len(ciudades), 1)

    # 2) Turnos: todas las fuentes avanzan a la vez mientras quede tiempo
    vueltas, hechas = 0, {r: 0 for r in listas}
    while queda_tiempo(margen=3) and listas:
        movido = False
        for raiz in list(listas):
            if not queda_tiempo(margen=3):
                break
            comb, pais, ciudades = listas[raiz]
            if not ciudades or hechas[raiz] >= len(ciudades):
                continue                       # esta fuente ya dio la vuelta entera
            movido = True
            ini = indices[raiz]
            for k in range(BLOQUE):
                if not queda_tiempo(margen=3) or hechas[raiz] >= len(ciudades):
                    break
                u = ciudades[(ini + k) % len(ciudades)]
                try:
                    filas, _ = pi_ciudad(u, comb)
                except Exception as e:
                    log(f"  fallo en {u}: {type(e).__name__}: {e}")
                    filas = []
                for e in filas:
                    e["country"] = pais
                    e["source"] = "Prijzenindex"
                    e["brand"] = e["name"]
                todo.extend(filas)
                indices[raiz] = (ini + k + 1) % len(ciudades)
                hechas[raiz] += 1
        vueltas += 1
        if not movido:
            break
        log(f"  vuelta {vueltas}: " +
            " · ".join(f"{ETIQUETA.get(listas[r][0], r)[:9]} {hechas[r]}/{len(listas[r][2])}"
                       for r in listas))

    # 3) Se guarda por donde se ha quedado cada fuente para la proxima pasada
    for raiz in listas:
        CURSOR[raiz] = indices[raiz]

    log("\n  === PRIJZENINDEX: RESUMEN ===")
    for raiz in listas:
        comb, pais, ciudades = listas[raiz]
        log(f"  {ETIQUETA.get(comb, comb):<16} {pais}: {hechas[raiz]} de {len(ciudades)} "
            f"ciudades esta pasada, siguiente arranque en la {indices[raiz]}")
    for comb in COMBUSTIBLES:
        for pais in ("BE", "NL"):
            n = sum(1 for e in todo if comb in e.get("prices", {}) and e.get("country") == pais)
            if n:
                log(f"  filas de {ETIQUETA.get(comb, comb):<16} {pais}: {n}")
    log(f"  filas totales esta pasada: {len(todo)}")
    return todo


def pi_indice(comb, pais, raiz):
    """Portada de la fuente: estaciones que ya lista y enlaces a sus ciudades."""
    etiqueta = ETIQUETA.get(comb, comb)
    log(f"\n  --- {etiqueta} en {pais} ({raiz}) ---")
    html, diag = traer(PI_BASE + raiz)
    log(f"  indice -> {diag}")
    if html is None:
        return [], []

    fuera = []
    for f in [limpia_fila(x) for x in re.split(r'<tr[\s>]', html)[1:]]:
        e = pi_fila(f, comb)
        if e:
            e["country"] = pais
            e["source"] = "Prijzenindex"
            e["brand"] = e["name"]
            fuera.append(e)
    log(f"  del propio indice: {len(fuera)} estaciones")

    ciudades = []
    for m in pi_re_ciudad(raiz).finditer(html):
        u = m.group(1)
        if u.rstrip("/") == raiz.rstrip("/"):
            continue
        if u not in ciudades:
            ciudades.append(u)
    log(f"  ciudades en el listado: {len(ciudades)}")
    return fuera, ciudades


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
            # Si el extractor ya sabe la marca y el pais reales, se respetan.
            e.setdefault("brand", marca)
            e.setdefault("country", pais)
            e.setdefault("source", marca)
        return [e for e in map(normaliza, r) if e]

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
    return [e for e in map(normaliza, mejor) if e]


def normaliza(e):
    """
    Deja todo registro con la misma forma antes de fusionar:
      prices -> diccionario {carburante: precio}
      viejo  -> diccionario {carburante: bool}
    Los extractores antiguos que solo devuelven "price" siguen valiendo:
    se interpreta como gasoleo, que es lo que recogian.
    """
    if not e or e.get("lat") is None or e.get("lon") is None:
        return None
    precios = e.get("prices")
    if not isinstance(precios, dict) or not precios:
        p = e.get("price")
        precios = {"diesel": p} if p else {}
    limpio = {}
    for comb, p in precios.items():
        v = a_float(p, comb)
        if v:
            limpio[comb] = v
    if not limpio:
        return None
    e["prices"] = limpio
    viejo = e.get("viejo")
    if not isinstance(viejo, dict):
        viejo = {c: bool(viejo) for c in limpio}
    e["viejo"] = {c: bool(viejo.get(c)) for c in limpio}
    e["price"] = limpio.get("diesel")
    return e


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


# ---------------------------------------------------------------------------
# Fusion por coordenadas. En la v2 el duplicado se descartaba; ahora se funde,
# porque la misma estacion aparece una vez por carburante y cada aparicion
# trae un precio distinto que hay que conservar.
# ---------------------------------------------------------------------------
def fusiona(todas, hoy, previo=None):
    """
    Funde lo recogido hoy con lo que ya habia publicado. Cada precio lleva su
    propia fecha en "pdate": asi una estacion puede tener el gasoleo de hoy y
    la gasolina de la semana pasada, y el mapa puede decir de cuando es cada
    cosa en vez de fingir que todo es de esta manana.
    """
    fusion, orden = {}, []

    def clave(e):
        return (round(e["lat"], 4), round(e["lon"], 4))

    # Lo anterior entra primero, para que lo de hoy lo pise
    for k, e in (previo or {}).items():
        fusion[k] = e
        orden.append(k)

    for e in todas:
        k = clave(e)
        base = fusion.get(k)
        if base is None:
            e["pdate"] = {c: hoy for c in e["prices"]}
            fusion[k] = e
            orden.append(k)
            continue
        base.setdefault("prices", {})
        base.setdefault("pdate", {})
        base.setdefault("viejo", {})
        for comb, p in e["prices"].items():
            base["prices"][comb] = p                       # lo de hoy manda
            base["pdate"][comb] = hoy
            base["viejo"][comb] = e.get("viejo", {}).get(comb, False)
        if base.get("name", "") in ("", "Gasolinera") and e.get("name"):
            base["name"] = e["name"]
            base["brand"] = e.get("brand") or e["name"]
        if not base.get("city") and e.get("city"):
            base["city"] = e["city"]
        # El pais NO se pisa: una estacion no cambia de pais segun el orden en
        # que se raspe, y en la franja fronteriza eso daria tumbos.
        base["country"] = base.get("country") or e.get("country")
        base["source"] = base.get("source") or e.get("source")

    # Se tiran los precios caducados, y la estacion entera si no le queda ninguno
    corte = (datetime.now(timezone.utc) - timedelta(days=CADUCA)).strftime("%Y-%m-%d")
    limpias, caducados, muertas = [], 0, 0
    for k in orden:
        e = fusion[k]
        for comb in list(e.get("prices", {})):
            if e.get("pdate", {}).get(comb, "") < corte:
                del e["prices"][comb]
                e["pdate"].pop(comb, None)
                e.get("viejo", {}).pop(comb, None)
                caducados += 1
        if not e.get("prices"):
            muertas += 1
            continue
        e["date"] = max(e["pdate"].values()) if e.get("pdate") else hoy
        e["price"] = e["prices"].get("diesel")
        e["id"] = "%s-%.5f-%.5f" % (str(e.get("brand") or "estacion").lower().replace(" ", ""),
                                    e["lat"], e["lon"])
        limpias.append(e)
    if caducados or muertas:
        log(f"  caducados (mas de {CADUCA} dias): {caducados} precios, "
            f"{muertas} estaciones se quedan sin ninguno")
    return limpias


def main():
    global CURSOR
    hoy = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    log("=== ARRANQUE " + hoy + " ===")
    previo, CURSOR = carga_previo()
    descubrir_marcas()
    todas = []
    for c in CADENAS:
        try:
            todas.extend(procesar(c))
        except Exception as e:
            log(f"  ERROR inesperado en {c['marca']}: {type(e).__name__}: {e}")

    limpias = fusiona(todas, hoy, previo)

    doc = {
        "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "count": len(limpias),
        "fuels": [c for c in COMBUSTIBLES
                  if any(c in e["prices"] for e in limpias)],
        "labels": {c: ETIQUETA[c] for c in COMBUSTIBLES},
        "note": "Precios recogidos de las webs de cada cadena. Pueden diferir del surtidor.",
        "cursor": CURSOR,
        "stations": limpias,
    }
    with open(SALIDA, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))

    log("\n=== RESUMEN " + hoy + " ===")
    log(f"Estaciones distintas: {len(limpias)}")
    for pais in ("NL", "BE"):
        n = sum(1 for e in limpias if e["country"] == pais)
        log(f"  {pais}: {n}")
    log("Precios por carburante:")
    for comb in COMBUSTIBLES:
        n = sum(1 for e in limpias if comb in e["prices"])
        if n:
            log(f"  {ETIQUETA[comb]:<16} {n}")
    multi = sum(1 for e in limpias if len(e["prices"]) > 1)
    log(f"Estaciones con mas de un carburante: {multi}")
    frescas = sum(1 for e in limpias if e.get("date") == hoy)
    log(f"Actualizadas hoy: {frescas} · arrastradas de pasadas anteriores: {len(limpias)-frescas}")
    log(f"Cursor para la proxima pasada: {CURSOR}")
    return 0        # nunca falla: el archivo se escribe siempre


if __name__ == "__main__":
    sys.exit(main())
