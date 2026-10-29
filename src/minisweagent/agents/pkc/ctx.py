"""The `ctx` restore command: a POSIX sh script in the sandbox that reads items the harness materializes on demand."""

import base64
import hashlib
import re
import shlex

SCRIPT = r"""#!/bin/sh
D=@DIR@
echo "$*" >> "$D/log"
usage() { echo "usage: ctx show <id> | ctx show <path>::<symbol> | ctx show <path>:<a>-<b> | ctx list" >&2; exit 2; }
nlr() { nl -ba "$1" | sed -n "$2,$3p"; }
case "$1" in
  list) cat "$D/list" 2>/dev/null; exit 0 ;;
  show) [ -n "$2" ] || usage ;;
  *) usage ;;
esac
a="$2"
case "$a" in
  *::*)
    f="$D/sym/$(printf %s "$a" | sha1sum | cut -c1-16)"
    [ -f "$f" ] || { echo "ctx: unknown symbol $a" >&2; exit 1; }
    { read -r p; read -r s; read -r e; read -r h; } < "$f"
    [ -f "$p" ] || { echo "ctx: no such file $p" >&2; exit 1; }
    if [ "$(sha1sum < "$p" | cut -c1-40)" = "$h" ]; then nlr "$p" "$s" "$e"; exit 0; fi
    n="${a##*::}"; n="${n##*.}"
    s=$(grep -nE "^[[:space:]]*((export|pub|async|static|public|private|protected)[[:space:]]+)*(def|class|func|function|fn|type|struct|interface|enum|impl|trait)[[:space:]]+(\([^)]*\)[[:space:]]*)?$n([^A-Za-z0-9_]|\$)" "$p" | head -n 1 | cut -d: -f1)
    [ -n "$s" ] || { echo "ctx: symbol $a not found in current $p" >&2; exit 1; }
    echo "[ranges recomputed after file change]"
    nlr "$p" "$s" "$((s + 149))" ;;
  *:*-*)
    p="${a%:*}"; r="${a##*:}"
    [ -f "$p" ] || { echo "ctx: no such file $p" >&2; exit 1; }
    nlr "$p" "${r%-*}" "${r#*-}" ;;
  *)
    [ -f "$D/store/$a" ] || { echo "ctx: unknown id $a" >&2; exit 1; }
    cat "$D/store/$a.h" 2>/dev/null; cat "$D/store/$a" ;;
esac
"""
LITERAL = re.compile(r"#?[oe]\d+(?:\.\d+)?|[\w./@+-]+::[\w.]+|[\w./@+-]+:\d+-\d+")
_CHUNK = 60_000


def refs(cmd: str) -> tuple[list[str], bool]:
    """Arguments of `ctx show` in a command, and whether any of them is not a literal."""
    found = [r.rstrip(";&|)").strip("'\"") for r in re.findall(r"\bctx\s+show\s+(\S+)", cmd)]
    return [r.lstrip("#") if re.fullmatch(r"#[oe][\d.]+", r) else r for r in found], any(
        not LITERAL.fullmatch(r) for r in found
    )


def sym_key(arg: str) -> str:
    return hashlib.sha1(arg.encode()).hexdigest()[:16]


def write_commands(d: str, files: dict[str, str], symbols: list[tuple[str, str, int, int]]) -> list[str]:
    """Shell commands writing `files` (relative to `d`) and symbol entries (arg, abs path, start, end)."""
    q = shlex.quote
    cmds = [f"mkdir -p {q(d)}/bin {q(d)}/store {q(d)}/sym"]
    for rel, text in files.items():
        data = base64.b64encode(text.encode()).decode()
        target = q(f"{d}/{rel}")
        chunks = [data[i : i + _CHUNK] for i in range(0, len(data), _CHUNK)] or [""]
        cmds += [f"chmod u+w {target} 2>/dev/null; : > {target}"]
        cmds += [f"printf %s {q(c)} | base64 -d >> {target}" for c in chunks]
        cmds += [f"chmod {'755' if rel.startswith('bin/') else '444'} {target}"]
    for arg, path, start, end in symbols:
        f = q(f"{d}/sym/{sym_key(arg)}")
        cmds += [f"printf '%s\\n%s\\n%s\\n' {q(path)} {start} {end} > {f} && sha1sum < {q(path)} | cut -c1-40 >> {f}"]
    out, cur = [], ""
    for c in cmds:
        if cur and len(cur) + len(c) > 2 * _CHUNK:
            out.append(cur)
            cur = ""
        cur = f"{cur}\n{c}" if cur else c
    return out + ([cur] if cur else [])


def wrap(cmd: str, d: str) -> str:
    return f"export PATH={shlex.quote(d + '/bin')}:$PATH\n{cmd}"


def read_log(d: str) -> str:
    return f"cat {shlex.quote(d)}/log 2>/dev/null; : > {shlex.quote(d)}/log"
