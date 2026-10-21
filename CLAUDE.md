# CLAUDE.md · Surtidor (adrigasolineras)

Mapa de precios de carburante entre Madrid y Ámsterdam, de Adrián (no programa). Antes de nada, lee `README.md`.

Cómo trabajar con Adrián: en español (si escribe en inglés, en inglés, corrigiendo con suavidad gramática y
vocabulario), práctico y directo, datos comprobados, código siempre completo, comandos exactos con una frase de qué
hacen. GitHub lo lleva Claude de principio a fin.

## La fábrica en este proyecto

Toda entrega pasa por la fábrica (Adrián, 20/10/2026): especificación, construcción, revisores independientes y
aprobador antes de `main`. El protocolo, común a todos sus repositorios, está en `.claude/fabrica.md`; aquí, lo propio
de este. `main` se publica en Netlify; las versiones del .exe salen de una etiqueta (`v...`), que solo se pone sobre un
commit de `main` ya aprobado. La Action `precios.yml` sube precios a `main` dos veces al día: no pasa por la fábrica.

@.claude/fabrica.md

- **Pruebas** (en `.fabrica/pruebas.txt`): `python -m pytest tests -q` en verde.
- **Revisores**: `revisor-codigo` siempre. `revisor-seguridad` si se toca `servidor.py`, `admin.py`, `surtidor.py`
  (actualizaciones), `collector/`, `.github/workflows/`, el manejo de claves (Tankerkönig, Open Charge Map) o
  `.claude/`. `revisor-ux` y `revisor-diseno` si se toca `index.html` o `panel/`.
- **Se escala siempre**: `.github/workflows/`; `surtidor.py` y `EXE_MIN` (lanzador de los exe instalados); claves.
