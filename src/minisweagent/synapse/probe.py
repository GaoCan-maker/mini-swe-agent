"""Side-channel commands executed through the agent's environment (never shown to the model).

All scripts are POSIX sh: the local environment runs commands with /bin/sh, which may be dash.
"""

import shlex
import time
from collections import Counter
from collections.abc import Callable

from minisweagent.synapse.symbols import LANG_BY_EXT

MARK = "__SYNAPSE_PROBE__"
_SOURCE_GLOBS = "|".join(f"*{ext}" for ext in LANG_BY_EXT)


class Probe:
    def __init__(self, execute: Callable, *, timeout: int, stats: Counter):
        self._execute, self.timeout, self.stats = execute, timeout, stats

    def run(self, command: str, *, timeout: int | None = None) -> tuple[str, int]:
        """Run a command; the output is prefixed with a marker so it can never be mistaken for a submission."""
        action = {"command": f"printf '%s\\n' {MARK}; {command}"}
        start = time.time()
        try:
            try:
                out = self._execute(action, timeout=timeout or self.timeout)
            except TypeError:
                out = self._execute(action)
        except Exception:
            self.stats["probe_errors"] += 1
            return "", -1
        finally:
            self.stats["probe_calls"] += 1
            self.stats["probe_seconds"] += time.time() - start
        text = out.get("output", "") or ""
        idx = text.find(MARK + "\n")
        if idx >= 0:
            text = text[idx + len(MARK) + 1 :]
        return text, out.get("returncode", -1)


def locate(probe: Probe) -> tuple[str, str, str] | None:
    """Returns (logical cwd, physical cwd, git root) or None if the cwd is not inside a git work tree."""
    out, _ = probe.run("pwd; pwd -P; git rev-parse --show-toplevel 2>/dev/null || echo __NOGIT__")
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if len(lines) < 3 or lines[-1] == "__NOGIT__":
        return None
    return lines[-3], lines[-2], lines[-1]


def read_status(probe: Probe, root: str, max_lines: int) -> dict[str, tuple[str, str]] | None:
    """Map path -> (porcelain status code, content hash) for files that differ from HEAD.

    Untracked files are only reported for source extensions. Hash '-' means the file does not exist.
    """
    script = f"""cd {shlex.quote(root)} 2>/dev/null || exit 0
T=$(mktemp -d 2>/dev/null || echo /tmp/.synapse_$$); mkdir -p "$T"
git -c core.quotepath=off status --porcelain=v1 -uall --no-renames 2>/dev/null | head -n {max_lines} > "$T/s"
while IFS= read -r l; do p=${{l#???}}; s=${{l%"$p"}}
  case "$s" in '?? ') case "$p" in {_SOURCE_GLOBS}) ;; *) continue;; esac;; esac
  printf '%s\\t%s\\n' "$s" "$p" >> "$T/e"
  [ -f "$p" ] && printf '%s\\n' "$p" >> "$T/h"
done < "$T/s"
echo __ENTRIES__; [ -f "$T/e" ] && cat "$T/e"
echo __HASHES__; [ -f "$T/h" ] && git hash-object --stdin-paths < "$T/h" 2>/dev/null
echo __PATHS__; [ -f "$T/h" ] && cat "$T/h"
rm -rf "$T"
"""
    out, _ = probe.run(script)
    if "__ENTRIES__" not in out or "__HASHES__" not in out or "__PATHS__" not in out:
        return None
    entries_part, rest = out.split("__ENTRIES__", 1)[1].split("__HASHES__", 1)
    hashes_part, paths_part = rest.split("__PATHS__", 1)
    hash_lines = hashes_part.strip("\n").splitlines()
    path_lines = paths_part.strip("\n").splitlines()
    hashes = dict(zip(path_lines, hash_lines)) if len(hash_lines) == len(path_lines) else {p: "?" for p in path_lines}
    status = {}
    for line in entries_part.strip("\n").splitlines():
        if "\t" not in line:
            continue
        code_, path = line.split("\t", 1)
        status[path] = (code_.strip() or "M", hashes.get(path, "-"))
    return status


def fetch_file(probe: Probe, root: str, path: str, max_bytes: int, *, head: bool = False) -> str | None:
    """Current (or HEAD) content of a repository file, None if missing, binary or too large."""
    q = shlex.quote(path)
    if head:
        cmd = f"cd {shlex.quote(root)} && git cat-file -s HEAD:{q} 2>/dev/null && git show HEAD:{q} 2>/dev/null"
    else:
        cmd = (
            f"cd {shlex.quote(root)} && [ -f {q} ] && s=$(wc -c < {q}) && echo $s && "
            f'{{ [ "$s" -gt {max_bytes} ] || cat -- {q}; }}'
        )
    out, code = probe.run(cmd)
    if code != 0 or "\n" not in out:
        return None
    size_line, content = out.split("\n", 1)
    try:
        if int(size_line.strip()) > max_bytes:
            return None
    except ValueError:
        return None
    if "\x00" in content[:8000]:
        return None
    return content


def git_grep(probe: Probe, root: str, name: str, max_hits: int) -> list[tuple[str, int, str]]:
    exts = " ".join(shlex.quote(f"*{ext}") for ext in LANG_BY_EXT)
    cmd = (
        f"cd {shlex.quote(root)} && git grep -n -I -w -F --untracked -e {shlex.quote(name)} -- {exts} "
        f"2>/dev/null | head -n {max_hits}"
    )
    out, _ = probe.run(cmd)
    hits = []
    for line in out.splitlines():
        parts = line.split(":", 2)
        if len(parts) == 3 and parts[1].isdigit():
            hits.append((parts[0], int(parts[1]), parts[2]))
    return hits


def files_mentioning(probe: Probe, root: str, needle: str, files: list[str]) -> set[str]:
    if not files or not needle:
        return set()
    cmd = f"cd {shlex.quote(root)} && grep -l -F -e {shlex.quote(needle)} -- " + " ".join(shlex.quote(f) for f in files)
    out, _ = probe.run(cmd + " 2>/dev/null")
    return {line.strip() for line in out.splitlines() if line.strip()}
