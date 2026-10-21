# La fábrica: cómo entra un cambio en `main`

Adrián lo pidió el 20/10/2026 («every repo», «without invoking any skills»): en todos sus repositorios, cada cambio
pasa por especificación, construcción, revisores independientes y aprobador antes de entrar en `main`. No hay que
invocar nada: es la forma normal de trabajar en este repositorio. Lo que es propio de cada proyecto (qué revisor
entra, qué se escala, cómo se prueba) está en su CLAUDE.md, sección «La fábrica en este proyecto».

Este fichero es igual en todos los repositorios (`.claude/fabrica.md`). Si se cambia, se cambia en todos.

## Cuándo

- Sí: todo encargo que cambia ficheros del repositorio y acaba en `main` (código, datos versionados, documentación).
- No: preguntas, explicaciones, leer informes o registros sin cambiar nada. Tampoco lo que suben solos los
  programas o las Actions (precios, «Cambios desde la app»...): no pasa por Claude.
- Si Adrián dice expresamente que un cambio concreto va sin fábrica («sin fábrica», «urgente, súbelo ya»), se
  escribe el veredicto con `"decision": "APROBADO_POR_ADRIAN"` y en `motivo` sus palabras literales. Nunca por
  iniciativa propia.

## Pasos

1. **Especificación** (la sesión principal, que es la que habla con Adrián): `.fabrica/espec.md` con
   - la petición de Adrián, con sus palabras;
   - qué cambia y qué no debe romperse;
   - cómo se va a probar (los comandos);
   - qué revisores entran (según la sección del proyecto) y si algo apunta a escalada.
2. **Construcción** (la sesión principal): el cambio en la rama de trabajo, las pruebas de verdad con su salida
   guardada en `.fabrica/pruebas.txt`, commit y push de la rama. Nunca a `main`.
3. **Revisores**, en paralelo y en un solo mensaje (herramienta Agent, `subagent_type` = el nombre del revisor):
   - `revisor-codigo`: siempre;
   - `revisor-seguridad`, `revisor-ux`, `revisor-diseno`: cuando lo diga la sección del proyecto.
   A cada uno se le da solo: el repositorio (ruta), `.fabrica/espec.md`, el commit base (`git merge-base origin/main
   HEAD`) y el commit final (`HEAD`). No ven la conversación: así juzgan solo lo que se pidió y lo que se hizo.
   Su respuesta se guarda tal cual en `.fabrica/revision-<revisor>.md`.
4. **Rondas**: si alguno dice `VEREDICTO: CAMBIOS`, se arregla, se prueba, push y vuelven a revisar solo los que
   pidieron cambios, con el diff nuevo. Dos rondas como mucho; si siguen pidiendo cambios, va al aprobador igual
   (y él escalará).
5. **Aprobador** (`aprobador`), siempre: lee la especificación, las revisiones, las pruebas y el diff, y escribe
   `.fabrica/veredicto.json` con el commit exacto.
6. **APROBAR**: pull request y unión a `main` diciendo el commit aprobado: `expectedHeadSha` en la herramienta de
   GitHub o `--match-head-commit` con `gh pr merge` (así GitHub no une otro commit). Nada de unión automática. La
   puerta (`.claude/hooks/puerta_fabrica.py`) comprueba el veredicto y el commit; si no cuadran, bloquea la unión.
7. **ESCALAR**: el pull request se queda abierto y se le dice a Adrián, en dos o tres líneas que se entiendan sin
   saber programar, qué cambia y por qué necesita su visto bueno. Solo si responde que sí («aprobado», «adelante»...),
   la sesión escribe el veredicto con `"decision": "APROBADO_POR_ADRIAN"`, el commit y sus palabras en `motivo`, y
   une. La sesión nunca escribe `"APROBAR"`: eso solo lo escribe el aprobador.

Si después de aprobar cambia algo (otro commit), el veredicto ya no vale: vuelta al paso 3 con lo nuevo.

Sesión abierta fuera de la carpeta del repositorio (varios repositorios a la vez): Claude Code no carga su
`.claude/` y no existen ni los revisores ni la puerta. Se hace igual: los revisores y el aprobador como agente
general con el texto de su fichero de `.claude/agents/` (y su modelo), `.fabrica/` fuera del repositorio, y nunca se
une sin veredicto.

## Ficheros

`.fabrica/` está fuera de git (`.gitignore`): es el trabajo de una sesión. Formato del veredicto, una línea:
`{"sha": "<commit>", "decision": "APROBAR" | "ESCALAR" | "APROBADO_POR_ADRIAN", "motivo": "...", "fecha": "AAAA-MM-DDTHH:MM"}`

## Al terminar

En el informe a Adrián, una línea por revisor (aprobado o qué pidió y cómo se arregló) y la decisión del aprobador.
Lo demás, como pida el CLAUDE.md del proyecto.

## Modelos

Revisores de código, uso y diseño: Sonnet. Seguridad y aprobador: Opus. Los fija cada fichero de `.claude/agents/`.
