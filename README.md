# Surtidor

Mapa de precios de carburante entre Madrid y Ámsterdam, con planificador de paradas. Se usa de dos maneras:

- **En la web**, publicada en Netlify desde este repositorio. Los precios de Países Bajos y Bélgica los
  recoge la Action `precios.yml` dos veces al día.
- **En tu PC, con `Surtidor.exe`**, igual que el Catalogator: el programa se actualiza solo desde GitHub,
  recoge precios por su cuenta cada las horas que digas y tiene su panel de administración.

## Usar Surtidor.exe

1. Descarga `Surtidor.exe` de la última versión en
   [Releases](https://github.com/malevolisco/adrigasolineras/releases/latest). Solo hace falta una vez.
2. Ábrelo. La primera vez Windows puede avisar con «Windows protegió su PC», porque el exe no está firmado:
   **Más información → Ejecutar de todas formas**.
3. Se abre una ventana que comprueba actualizaciones, arranca el servidor y enseña el mapa.
4. El engranaje de arriba a la derecha del panel lleva a **Administración**.

Cerrar la ventana para el servidor. Cada vez que lo abras comprueba si hay versión nueva y la instala sola.

### Qué hay en el panel

| Pestaña | Para qué |
|---|---|
| Estado | Si está recogiendo, cuándo fue la última pasada y cuándo toca la siguiente. Recoger ahora, parar, traer lo recogido por GitHub, deshacer la última. Estaciones por país y carburante, e historial de pasadas. |
| Registro | Todo lo que escriben el servidor y el recolector, en vivo. Se puede filtrar a solo avisos y errores. |
| Ajustes | Cada cuántas horas recoger, tiempo máximo por pasada, claves de Tankerkönig y Open Charge Map, puerto. |
| Sistema | Versiones, buscar actualizaciones, abrir la carpeta, reiniciar el servidor, descargar `precios.json` y comprobar si cada fuente responde desde tu PC. |

### Dónde guarda las cosas

Todo en `%LOCALAPPDATA%\Surtidor\app`. Lo tuyo no se toca nunca al actualizar:

| Fichero | Qué es |
|---|---|
| `config.json` | Ajustes y claves. Nunca va al repositorio. |
| `precios.json` | Lo recogido. Cada pasada lo amplía; los precios de más de 21 días se descartan. |
| `precios.anterior.json` | Lo de antes del último cambio bueno: lo que recupera «Deshacer la última». |
| `historial.jsonl` | Una línea por pasada. |
| `surtidor.log` | El registro del lanzador. |

Si una pasada falla, se para a mano o el PC se apaga a mitad, se vuelve solo a lo que había antes de empezarla.

### Claves

Las de **Tankerkönig** (precios de Alemania) y **Open Charge Map** (puntos de carga) se ponen en
Administración → Ajustes y se quedan en tu PC. **No las subas nunca al repositorio**: es público, hay bots
que rastrean GitHub buscando claves, y Git conserva en el historial lo que borres después.

En la web pública siguen funcionando por la dirección: `?tk=TU_CLAVE` y `?ocm=TU_CLAVE`.

## Publicar una versión nueva

Se publica con una etiqueta. GitHub compila el exe en Windows, pasa las pruebas, comprueba que el exe
arranca y sirve el mapa, y crea la release. Si una prueba falla, no se publica nada.

```
git tag v2026.10.11
git push origin v2026.10.11
```

Los Surtidor.exe ya instalados se actualizan solos al abrirse. Si un cambio necesita un lanzador nuevo
(algo de `surtidor.py`), sube la versión de `EXE_MIN` a la de la etiqueta: los exe antiguos se cambian solos.

## Desarrollo

```
pip install -r requirements.txt pytest httpx
python -m pytest tests -q          # las pruebas
python servidor.py                 # el servidor, en http://127.0.0.1:8766
python surtidor.py --consola       # el lanzador entero, sin ventana
```

| Fichero | Qué hace |
|---|---|
| `index.html` | El mapa. Un solo fichero; sirve igual para la web y para el exe. |
| `collector/collect.py` | El recolector de Países Bajos y Bélgica. Solo librería estándar. |
| `servidor.py` | El servidor local: mapa, precios y recogida programada. |
| `admin.py`, `panel/admin.html` | El panel de administración. |
| `surtidor.py` | El lanzador: actualizaciones, ventana y arranque. Es lo que se compila a `Surtidor.exe`. |
| `empaquetar.py` | Hace `surtidor-app.zip`, lo que el exe descarga. |
| `surtidor.spec` | La receta de PyInstaller. |
| `.github/workflows/` | `precios.yml` recoge para la web, `release.yml` publica el exe, `pruebas.yml` prueba cada cambio. |
