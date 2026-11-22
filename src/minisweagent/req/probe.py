"""Side-channel commands executed through the agent's environment (never shown to the model).

All scripts are POSIX sh: the local environment runs commands with /bin/sh, which may be dash.
Changes are measured against the commit that was checked out when the run started, so they stay
visible after the agent commits them on a new branch.
"""

import shlex
import time
from collections import Counter
from collections.abc import Callable

MARK = "__REQ_PROBE__"
FILE_MARK = "__REQ_FILE__"
_SKIP_CASE = (
    "node_modules/*|*/node_modules/*|vendor/*|dist/*|build/*|target/*|.venv/*|venv/*|"
    "__pycache__/*|*/__pycache__/*|*.min.js|*.map|*.lock|*.pyc"
)


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


def locate(probe: Probe) -> tuple[str, str] | None:
    """Returns (git root, HEAD commit) or None if the cwd is not inside a git work tree with a commit."""
    out, _ = probe.run("git rev-parse --show-toplevel 2>/dev/null && git rev-parse HEAD 2>/dev/null || echo __NOGIT__")
    lines = [line.strip() for line in out.splitlines() if line.strip()]
    if len(lines) < 2 or lines[-1] == "__NOGIT__" or len(lines[-1]) < 7:
        return None
    return lines[-2], lines[-1]


def snapshot(probe: Probe, root: str, base: str, max_paths: int) -> dict[str, str] | None:
    """Map path -> content hash for every file that differs from `base` (tracked) or is untracked.

    Hash '-' means the file was deleted.
    """
    script = f"""cd {shlex.quote(root)} 2>/dev/null || exit 0
T=$(mktemp -d 2>/dev/null || echo /tmp/.req_$$); mkdir -p "$T"; : > "$T/h"
{{ git -c core.quotepath=off diff --name-only --no-renames {shlex.quote(base)} 2>/dev/null
  git -c core.quotepath=off ls-files --others --exclude-standard 2>/dev/null; }} | head -n {max_paths} > "$T/p"
echo __DELETED__
while IFS= read -r p; do
  case "$p" in {_SKIP_CASE}) continue;; esac
  if [ -f "$p" ]; then printf '%s\\n' "$p" >> "$T/h"; else printf '%s\\n' "$p"; fi
done < "$T/p"
echo __HASHES__; [ -s "$T/h" ] && git hash-object --stdin-paths < "$T/h" 2>/dev/null
echo __PATHS__; cat "$T/h"
rm -rf "$T"
"""
    out, _ = probe.run(script)
    if "__DELETED__" not in out or "__HASHES__" not in out or "__PATHS__" not in out:
        return None
    deleted_part, rest = out.split("__DELETED__", 1)[1].split("__HASHES__", 1)
    hashes_part, paths_part = rest.split("__PATHS__", 1)
    hash_lines = hashes_part.strip("\n").splitlines()
    path_lines = paths_part.strip("\n").splitlines()
    state = dict(zip(path_lines, hash_lines)) if len(hash_lines) == len(path_lines) else {p: "?" for p in path_lines}
    for path in deleted_part.strip("\n").splitlines():
        if path:
            state[path] = "-"
    return state


def changed_code(
    probe: Probe, root: str, base: str, max_bytes: int, max_file_bytes: int
) -> tuple[dict[str, str], set[str]]:
    """Added lines (tracked files) and full content (untracked text files) relative to `base`, per path.

    Also returns the set of untracked paths.
    """
    script = f"""cd {shlex.quote(root)} 2>/dev/null || exit 0
{{ git -c core.quotepath=off diff --no-color --no-ext-diff --no-renames -U0 {shlex.quote(base)} 2>/dev/null
echo __UNTRACKED__
git -c core.quotepath=off ls-files --others --exclude-standard 2>/dev/null | head -n 300 | while IFS= read -r p; do
  case "$p" in {_SKIP_CASE}) continue;; esac
  [ -f "$p" ] || continue
  s=$(wc -c < "$p"); [ "$s" -gt {max_file_bytes} ] && continue
  grep -Iq . "$p" 2>/dev/null || continue
  printf '\\n{FILE_MARK} %s\\n' "$p"; cat -- "$p"
done; }} | head -c {max_bytes}
"""
    out, _ = probe.run(script, timeout=max(probe.timeout, 60))
    diff_part, _, untracked_part = out.partition("__UNTRACKED__")
    code: dict[str, list[str]] = {}
    current = None
    for line in diff_part.splitlines():
        if line.startswith("+++ "):
            target = line[4:].strip()
            current = target[2:] if target.startswith("b/") else None
            if current:
                code.setdefault(current, [])
        elif line.startswith("+") and current is not None:
            code[current].append(line[1:])
    untracked = split_files(untracked_part)
    for path, content in untracked.items():
        code.setdefault(path, []).append(content)
    return {path: "\n".join(lines) for path, lines in code.items() if lines}, set(untracked)


def split_files(text: str) -> dict[str, str]:
    files: dict[str, str] = {}
    for chunk in text.split(f"\n{FILE_MARK} ")[1:]:
        path, _, content = chunk.partition("\n")
        if path.strip():
            files[path.strip()] = content
    return files


def read_files(probe: Probe, paths: list[str], max_file_bytes: int, *, root: str = "") -> dict[str, str]:
    """Contents of the given text files (relative to the agent's cwd, or to `root` if given)."""
    if not paths:
        return {}
    loop = " ".join(shlex.quote(p) for p in paths)
    script = (
        (f"cd {shlex.quote(root)} 2>/dev/null; " if root else "")
        + f'for p in {loop}; do [ -f "$p" ] || continue; grep -Iq . "$p" 2>/dev/null || continue; '
        + f'printf \'\\n{FILE_MARK} %s\\n\' "$p"; head -c {max_file_bytes} -- "$p"; done'
    )
    out, _ = probe.run(script)
    return split_files("\n" + out if not out.startswith("\n") else out)
