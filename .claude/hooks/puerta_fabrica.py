# -*- coding: utf-8 -*-
"""
Puerta de la fabrica (.claude/fabrica.md). Claude Code la ejecuta antes de cada orden de la terminal y de cada
herramienta de GitHub que une o escribe (hook PreToolUse de .claude/settings.json). Deja pasar todo salvo lo que mete
codigo en `main` de este repositorio:

  - unir un pull request (merge_pull_request, `gh pr merge`, `gh api .../pulls/N/merge`): solo con el commit aprobado
    dicho expresamente (`expectedHeadSha`, `--match-head-commit`, `-f sha=`), asi GitHub rechaza la union si el pull
    request tiene otro commit (uno que nadie ha revisado)
  - la union automatica (enable_pr_auto_merge) y escribir ficheros directamente en main (push_files,
    create_or_update_file, delete_file con branch main): nunca
  - `git push` a main: destino main ("origin main", "HEAD:main", "+main") o sin destino ("git push", "origin HEAD")
    estando en main; el commit que sube (HEAD) tiene que ser el aprobado
  - publicar: etiquetas que no estan en main ni son del commit aprobado (`git push origin v1`, --tags), `gh release
    create --target <rama>`, `gh workflow run --ref <rama>`

La orden se lee como la lee la terminal (palabras, comillas, `;`, `&&`, `|`, heredocs, `bash -c`, `eval`, `...`,
$(...), for/if, timeout, env...): un "push a main" dentro del mensaje de un commit o de un `echo` no es un push.

Aprobado = `.fabrica/veredicto.json` con "APROBAR" del aprobador o "APROBADO_POR_ADRIAN" cuando Adrian ha dicho que si
a una escalada. Si no, sale con 2 y Claude Code lo bloquea y le dice a Claude por que.

Es igual en todos los repositorios de Adrian. Es una red de seguridad contra el olvido, no contra la mala fe: la
propia sesion podria escribir el veredicto. Por eso .claude/fabrica.md dice quien puede escribirlo. Si algo de aqui
falla (sin git, JSON roto de Claude Code, una orden que no se entiende), deja pasar: nunca debe impedir trabajar por
un error suyo. No ve los alias de git ("git p origin main"): por eso es una red, no un candado.
"""

import json
import os
import re
import shlex
import subprocess
import sys

VALEN = ("APROBAR", "APROBADO_POR_ADRIAN")
MAIN = ("main", "refs/heads/main")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)(\w+)\1([^\n]*)\n.*?\n[ \t]*\2[ \t]*(?=\n|$)", re.S)
SEPARADORES = set(";&|()")
ESCRIBEN = ("push_files", "create_or_update_file", "delete_file")


def git(raiz, *args):
    return subprocess.run(["git", "-C", raiz] + list(args), capture_output=True, text=True,
                          timeout=10).stdout.strip()


def nombre_repo(raiz):
    """"trombinoscopio" desde la direccion del remoto (https://.../malevolisco/trombinoscopio.git o un proxy)."""
    url = git(raiz, "remote", "get-url", "origin")
    return re.sub(r"\.git$", "", url.rstrip("/")).rsplit("/", 1)[-1]


def incrustadas(texto):
    """Lo que la shell ejecuta dentro de `...` y $(...) fuera de comillas simples (tambien dentro de comillas dobles,
    como hace la shell)."""
    out, i, simple = [], 0, False
    while i < len(texto):
        c = texto[i]
        if c == "'" and not simple:
            j = texto.find("'", i + 1)
            i = len(texto) if j < 0 else j + 1
            continue
        if c == "`":
            j = texto.find("`", i + 1)
            if j < 0:
                break
            out.append(texto[i + 1:j])
            i = j + 1
            continue
        if texto.startswith("$(", i):
            nivel, j = 1, i + 2
            while j < len(texto) and nivel:
                nivel += {"(": 1, ")": -1}.get(texto[j], 0)
                j += 1
            out.append(texto[i + 2:j - 1])
            i = j
            continue
        i += 1
    return out


def ordenes(texto):
    """Las ordenes simples de una linea de terminal, cada una como lista de palabras (sin heredocs ni comillas)."""
    texto = HEREDOC.sub(lambda m: m.group(3), texto).replace("\\\n", " ")     # sin heredocs ni "\" de fin de linea
    lex = shlex.shlex(texto.replace("\n", " ; "), posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    out, actual = [], []
    for t in lex:
        if t and set(t) <= SEPARADORES:
            if actual:
                out.append(actual)
            actual = []
        else:
            actual.append(t)
    if actual:
        out.append(actual)
    return out


DELANTE = {"then", "do", "else", "elif", "!", "{", "time", "command", "nohup", "exec", "env", "sudo", "nice",
           "builtin", "timeout"}
CON_OPCIONES = {"env", "sudo", "nice", "timeout", "time", "command", "exec"}


def inicio(palabras):
    """Donde empieza el programa de una orden simple, saltando variables (X=y), palabras de la shell (then, do, !,
    {...) y envoltorios con sus opciones (env -i, nohup, timeout 60, nice -n 5, sudo -u x...)."""
    i = 0
    while i < len(palabras):
        p = palabras[i]
        if re.match(r"^\w+=", p):
            i += 1
        elif p in DELANTE:
            i += 1
            if p in CON_OPCIONES:
                while i < len(palabras) and (palabras[i].startswith("-") or
                                             re.match(r"^\d+(\.\d+)?[smhd]?$", palabras[i])):
                    i += 2 if palabras[i] in ("-u", "-g", "-n", "-s", "-k") else 1
        else:
            return i
    return i


def subcomando(palabras, programa):
    """("push", [argumentos]) de "[envoltorios] git [-C x] [-c k=v] push ...", o (None, [])."""
    i = inicio(palabras)
    if i >= len(palabras) or os.path.basename(palabras[i]) != programa:
        return None, []
    i += 1
    while i < len(palabras) and palabras[i].startswith("-"):
        i += 2 if palabras[i] in ("-C", "-c", "-R", "--repo") else 1
    return (palabras[i], palabras[i + 1:]) if i < len(palabras) else (None, [])


def texto_de_shell(palabras):
    """El texto que ejecuta "bash -c TEXTO" (o sh, zsh, dash) o "eval TEXTO"; None si no es eso."""
    i = inicio(palabras)
    if i >= len(palabras):
        return None
    programa = os.path.basename(palabras[i])
    if programa == "eval":
        return " ".join(palabras[i + 1:]) or None
    if programa in ("bash", "sh", "zsh", "dash"):
        resto = palabras[i + 1:]
        for k, a in enumerate(resto):
            if a.startswith("-") and "c" in a[1:] and not a.startswith("--") and k + 1 < len(resto):
                return resto[k + 1]
    return None


def opcion(argumentos, *nombres):
    """El valor de una opcion ("--match-head-commit abc", "--repo=x/y", "-f sha=abc" con nombre "sha")."""
    for k, a in enumerate(argumentos):
        for n in nombres:
            if a == n and k + 1 < len(argumentos):
                return argumentos[k + 1]
            if a.startswith(n + "="):
                return a[len(n) + 1:]
    return None


def push_a_main(argumentos, raiz):
    """True si `git push <argumentos>` sube a main: algun destino es main o no hay destino (o es HEAD/@) en main."""
    libres = [a for a in argumentos if not a.startswith("-") and not re.match(r"^\d*[<>&]", a)]   # ni "2>&1"
    destinos = [r.lstrip("+").split(":")[-1] for r in libres[1:]]
    if any(d in MAIN for d in destinos):
        return True
    if not [d for d in destinos if d not in ("HEAD", "@", "")]:
        return git(raiz, "branch", "--show-current") == "main"
    return False


def etiquetas_sin_aprobar(argumentos, raiz):
    """Las etiquetas publican (catalogar, adrigasolineras: release.yml con "v*"): un push que suba etiquetas solo con
    las que apuntan a un commit ya en origin/main o al aprobado. --tags/--follow-tags: todas las locales."""
    libres = [a for a in argumentos if not a.startswith("-") and not re.match(r"^\d*[<>&]", a)]
    nombres = [d[len("refs/tags/"):] for d in (r.lstrip("+").split(":")[-1] for r in libres[1:])
               if d.startswith("refs/tags/")]
    nombres += [d for d in (r.lstrip("+").split(":")[-1] for r in libres[1:])
                if d and not d.startswith("refs/") and git(raiz, "tag", "--list", d)]
    if "--tags" in argumentos or "--follow-tags" in argumentos:
        nombres += git(raiz, "tag", "--list").split()
    if not nombres:
        return
    fuera = set(git(raiz, "tag", "--no-merged", "origin/main").split())
    sueltas = [n for n in nombres if n in fuera]
    if not sueltas:
        return
    v = veredicto_de(raiz)
    for n in sueltas:
        if git(raiz, "rev-list", "-n", "1", n) != v.get("sha"):
            bloquear("la etiqueta %s publica una version y su commit no esta en main ni es el aprobado." % n)


def bloquear(motivo):
    sys.stderr.write("Fabrica: %s\nVer .claude/fabrica.md.\n" % motivo)
    sys.exit(2)


def veredicto_de(raiz):
    try:
        with open(os.path.join(raiz, ".fabrica", "veredicto.json"), encoding="utf-8") as fh:
            v = json.load(fh)
    except (OSError, ValueError):
        v = None
    if not isinstance(v, dict):
        bloquear("no hay veredicto del aprobador (.fabrica/veredicto.json). Antes de unir a main: revisores y "
                 "aprobador, o la aprobacion expresa de Adrian si se escalo.")
    if v.get("decision") not in VALEN:
        bloquear("el aprobador ha decidido %s: hay que preguntar a Adrian (%s)." % (v.get("decision"),
                                                                                 v.get("motivo", "")))
    return v


def mismo_commit(v, sha, como):
    """Que lo que se une sea el commit aprobado (vale abreviado, de 7 caracteres o mas)."""
    aprobado, sha = str(v.get("sha") or ""), str(sha or "")
    if len(sha) < 7:
        bloquear("une con el commit aprobado dicho expresamente (%s = %s): asi GitHub no une otro." % (
            como, aprobado[:12]))
    if not (aprobado.startswith(sha) or sha.startswith(aprobado or "-")):
        bloquear("el veredicto es del commit %s y se quiere unir %s: hay que volver a pasar el aprobador." % (
            aprobado[:7], sha[:7]))


def es_este(repo, raiz):
    return str(repo or "").rstrip("/").rsplit("/", 1)[-1].lower() == nombre_repo(raiz).lower()


def herramienta_github(herramienta, entrada, raiz):
    if not herramienta.endswith(ESCRIBEN + ("enable_pr_auto_merge", "merge_pull_request")):
        return              # leer, comentar, abrir pull requests...: libre
    if not es_este(entrada.get("repo"), raiz):
        return              # otro repositorio: no es cosa de esta puerta
    if herramienta.endswith(ESCRIBEN):
        if str(entrada.get("branch", "")) in MAIN:
            bloquear("nada se escribe directamente en main: rama, pull request y fabrica.")
        return
    if herramienta.endswith("enable_pr_auto_merge"):
        bloquear("la union automatica une mas tarde lo que tenga entonces el pull request, aprobado o no. Une a "
                 "mano con expectedHeadSha = el commit aprobado.")
    if herramienta.endswith("merge_pull_request"):
        mismo_commit(veredicto_de(raiz), entrada.get("expectedHeadSha"), "expectedHeadSha")


def orden_de_terminal(texto, raiz, nivel=0):
    if nivel > 3:
        return
    for dentro in incrustadas(texto):
        orden_de_terminal(dentro, raiz, nivel + 1)
    for palabras in ordenes(texto):
        dentro = texto_de_shell(palabras)
        if dentro:
            orden_de_terminal(dentro, raiz, nivel + 1)
            continue
        sub, args = subcomando(palabras, "git")
        if sub == "push":
            etiquetas_sin_aprobar(args, raiz)
        if sub == "push" and push_a_main(args, raiz):
            v = veredicto_de(raiz)
            cabeza = git(raiz, "rev-parse", "HEAD")
            if cabeza and v.get("sha") != cabeza:
                bloquear("el veredicto es del commit %s y ahora estas en %s: algo cambio despues de aprobarse. "
                         "Hay que volver a pasar el aprobador." % (str(v.get("sha"))[:7], cabeza[:7]))
            continue
        sub, args = subcomando(palabras, "gh")
        repo = opcion(palabras, "-R", "--repo")
        if sub == "pr" and args[:1] == ["merge"] and (not repo or es_este(repo, raiz)):
            if "--auto" in args:
                bloquear("la union automatica une mas tarde lo que tenga entonces el pull request, aprobado o no.")
            mismo_commit(veredicto_de(raiz), opcion(args, "--match-head-commit"), "--match-head-commit")
        elif sub == "release" and args[:1] == ["create"]:
            destino = opcion(args, "--target")
            if destino and destino not in MAIN:
                bloquear("una version solo se publica desde main (--target %s). Une antes con la fabrica." % destino)
        elif sub == "workflow" and args[:1] == ["run"]:
            rama = opcion(args, "--ref", "-r")
            if rama and rama not in MAIN:
                bloquear("una Action solo se lanza a mano sobre main (--ref %s)." % rama)
        elif sub == "api" and any(re.search(r"mergePullRequest|enablePullRequestAutoMerge", a) for a in args):
            bloquear("unir por graphql no deja decir el commit aprobado: usa la herramienta de GitHub o gh pr merge.")
        elif sub == "api":
            m = next((re.search(r"repos/([^/\s]+/[^/\s]+)/pulls/\d+/merge", a) for a in args
                      if re.search(r"/pulls/\d+/merge", a)), None)
            metodo = (opcion(args, "-X", "--method") or "GET").upper()
            plantilla = m and m.group(1) in ("{owner}/{repo}", ":owner/:repo")    # gh pone este repositorio
            if m and (plantilla or es_este(m.group(1), raiz)) and metodo == "PUT":     # GET solo pregunta
                campo = opcion(args, "-f", "-F", "--field", "--raw-field") or ""
                sha = campo[4:] if campo.startswith("sha=") else None
                for k, a in enumerate(args):        # el sha puede ir en cualquier -f
                    if a in ("-f", "-F", "--field", "--raw-field") and k + 1 < len(args) and \
                            args[k + 1].startswith("sha="):
                        sha = args[k + 1][4:]
                mismo_commit(veredicto_de(raiz), sha, "-f sha=")


def main():
    try:
        datos = json.load(sys.stdin)
    except ValueError:
        return
    if not isinstance(datos, dict):
        return
    raiz = os.environ.get("CLAUDE_PROJECT_DIR") or datos.get("cwd") or os.getcwd()
    herramienta, entrada = str(datos.get("tool_name", "")), datos.get("tool_input") or {}
    if not isinstance(entrada, dict):
        return
    try:
        if herramienta.startswith("mcp__github__"):
            herramienta_github(herramienta, entrada, raiz)
        elif herramienta == "Bash":
            orden_de_terminal(str(entrada.get("command", "")), raiz)
    except (OSError, subprocess.SubprocessError, ValueError):
        return      # sin git, o una orden que shlex no entiende (comillas sin cerrar): deja pasar


if __name__ == "__main__":
    main()
