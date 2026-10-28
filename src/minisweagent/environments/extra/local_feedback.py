"""Local environment that reports edits to repository source files and waits for background test runs."""

import functools
import os
import re
import subprocess
import time
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from minisweagent.environments.local import LocalEnvironment, LocalEnvironmentConfig

LANGUAGES = {
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
    ".kt": "kotlin",
    ".scala": "scala",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".php": "php",
    ".swift": "swift",
}
POLL_RE = re.compile(r"^\s*((?:cd \S+\s*&&\s*)?)sleep \d+(?:\.\d+)?\s*(?:&&|;)\s*")


class LocalFeedbackEnvironmentConfig(LocalEnvironmentConfig):
    repo: str = ""
    """Git repository to watch for edits. Empty: the git root of the working directory."""
    edit_receipt: bool = True
    receipt_max_files: int = 5
    receipt_max_hunk_lines: int = 15
    wait_background: bool = True
    wait_pattern: str = (
        r"\b(pytest|unittest|tox|go (test|build|vet)|cargo (test|build|check)|vitest|jest|mocha|node --test"
        r"|(npm|pnpm|yarn)( run)? (test|build)|tsc|mvn|gradle|make)\b"
    )


class LocalFeedbackEnvironment(LocalEnvironment):
    def __init__(self, *, config_class: type = LocalFeedbackEnvironmentConfig, **kwargs):
        """Like `LocalEnvironment`, but appends a one-line receipt per edited source file and
        replaces `sleep N && <check>` polling by waiting for background test processes to exit.
        """
        super().__init__(config_class=config_class, **kwargs)
        self._root: Path | None = Path(self.config.repo) if self.config.repo else None
        self._files: dict[str, str | None] | None = None
        self._ignored_pids = set(_processes())

    def execute(self, action: dict, cwd: str = "", *, timeout: int | None = None) -> dict[str, Any]:
        command = action.get("command", "")
        note = ""
        if self.config.wait_background and (match := POLL_RE.match(command)):
            if note := self._wait_background(timeout or self.config.timeout):
                action = action | {"command": match.group(1) + command[match.end() :]}
        output = super().execute(action, cwd, timeout=timeout)
        if self.config.edit_receipt and (receipt := self._receipt(command, cwd)):
            output["output"] += ("" if output["output"].endswith("\n") or not output["output"] else "\n") + receipt
        if note:
            output["output"] = f"{note}\n{output['output']}"
        return output

    def _wait_background(self, timeout: int) -> str:
        start = time.monotonic()
        if not (running := self._running()):
            return ""
        while running and time.monotonic() - start < timeout - 10:
            time.sleep(1)
            running = self._running()
        waited = round(time.monotonic() - start)
        if running:
            self._ignored_pids |= running.keys()
            return f"[background process still running after {waited}s: {next(iter(running.values()))[:100]}]"
        return f"[waited {waited}s for background process to exit]"

    def _running(self) -> dict[int, str]:
        return {
            pid: cmd
            for pid, cmd in _processes().items()
            if pid not in self._ignored_pids and re.search(self.config.wait_pattern, cmd)
        }

    def _repo(self, cwd: str) -> Path | None:
        if self._root is None:
            out = _git(["rev-parse", "--show-toplevel"], Path(cwd or self.config.cwd or os.getcwd()))
            self._root = Path(out.stdout.strip()) if out.returncode == 0 else None
        return self._root

    def _receipt(self, command: str, cwd: str) -> str:
        if (repo := self._repo(cwd)) is None:
            return ""
        current = {p: _read(repo / p) for p in _dirty_files(repo) | set(self._files or {})}
        if self._files is None:
            self._files = current
            return ""
        changed = {}
        for path in sorted(current):
            old = self._files[path] if path in self._files else _head_content(repo, path)
            if old != current[path]:
                changed[path] = old
        self._files.update(current)
        lines = [
            line
            for path, old in list(changed.items())[: self.config.receipt_max_files]
            for line in self._describe(path, old, current[path], command)
        ]
        if len(changed) > self.config.receipt_max_files:
            lines.append(f"[edit] +{len(changed) - self.config.receipt_max_files} more files")
        return "\n".join(lines)

    def _describe(self, path: str, old: str | None, new: str | None, command: str) -> list[str]:
        if new is None:
            return [f"[edit] {path}: deleted"]
        new_lines = new.splitlines()
        n = max(len(new_lines), 1)
        if old is None:
            ranges, summary = [(1, n)], f"(created, {len(new_lines)} lines)"
        else:
            matcher = SequenceMatcher(None, old.splitlines(), new_lines, autojunk=False)
            if not (ops := [op for op in matcher.get_opcodes() if op[0] != "equal"]):
                return []
            ranges = _merge_ranges([(min(j1 + 1, n), min(max(j2, j1 + 1), n)) for *_, j1, j2 in ops])
            shown = ", ".join(f"L{s}" if s == e else f"L{s}-{e}" for s, e in ranges[:3])
            more = f" +{len(ranges) - 3} more" if len(ranges) > 3 else ""
            added, removed = sum(j2 - j1 for *_, j1, j2 in ops), sum(i2 - i1 for _, i1, i2, _, _ in ops)
            summary = f"{shown}{more} (+{added}/-{removed})"
        receipt = f"[edit] {path} {summary}{_syntax_summary(path, old, new, ranges)}"
        hunk = [(n, new_lines[n - 1]) for s, e in ranges for n in range(s, min(e, len(new_lines)) + 1)]
        if len(hunk) <= self.config.receipt_max_hunk_lines and any(
            line.strip() and line.strip() not in command for _, line in hunk
        ):
            return [receipt, *(f"{n:6}\t{line}" for n, line in hunk)]
        return [receipt]


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in ranges:
        if merged and start <= merged[-1][1] + 3:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _syntax_summary(path: str, old: str | None, new: str, ranges: list[tuple[int, int]]) -> str:
    if (parser := _parser(LANGUAGES.get(Path(path).suffix, ""))) is None:
        return ""
    tree = parser.parse(new.encode())
    scope = _scope_name(tree.root_node, ranges[0][0] - 1) if old is not None else ""
    errors = _error_rows(tree.root_node)
    old_errors = _error_rows(parser.parse(old.encode()).root_node) if old else []
    if not errors:
        status = ", syntax ok"
    elif len(errors) > len(old_errors):
        near = [r for r in errors if any(s - 3 <= r <= e + 3 for s, e in ranges)]
        status = f", syntax ERROR near L{min(near or errors)}"
    else:
        status = ""
    return (f" in {scope}" if scope else "") + status


@functools.cache
def _parser(language: str) -> Any:
    if not language:
        return None
    try:
        from tree_sitter_language_pack import get_parser

        return get_parser(language)
    except Exception:  # optional dependency; receipts fall back to line ranges only
        return None


def _scope_name(root: Any, row: int) -> str:
    node = root.descendant_for_point_range((row, 0), (row, 0))
    while node is not None:
        if any(k in node.type for k in ("function", "method", "class", "constructor")) and (
            name := node.child_by_field_name("name")
        ):
            return name.text.decode(errors="replace")
        node = node.parent
    return ""


def _error_rows(root: Any) -> list[int]:
    rows, stack = [], [root]
    while stack:
        node = stack.pop()
        if node.type == "ERROR" or node.is_missing:
            rows.append(node.start_point[0] + 1)
        elif node.has_error:
            stack.extend(node.children)
    return rows


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-c", "safe.directory=*", *args], cwd=cwd, capture_output=True, text=True, errors="replace"
    )


def _dirty_files(repo: Path) -> set[str]:
    out = _git(["status", "--porcelain", "-z", "-uall", "--", *(f"*{ext}" for ext in LANGUAGES)], repo).stdout
    entries, paths = iter(out.split("\0")), set()
    for entry in entries:
        if entry:
            paths.add(entry[3:])
            if entry[0] in "RC":
                next(entries, None)
    return paths


def _head_content(repo: Path, path: str) -> str | None:
    out = _git(["show", f"HEAD:{path}"], repo)
    return out.stdout if out.returncode == 0 else None


def _read(path: Path) -> str | None:
    return path.read_text(errors="replace") if path.is_file() else None


def _processes() -> dict[int, str]:
    processes = {}
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            processes[int(proc.name)] = (proc / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="replace")
        except OSError:  # process exited while scanning
            pass
    return processes
