"""File versions in the sandbox, observed with silent git commands that never enter the model context."""

import difflib
import shlex
from dataclasses import dataclass

from minisweagent import Environment
from minisweagent.agents.pkc.commands import SCRATCH, SRC_EXT

_SEP = "\x01PKC\x01"
_SNAPSHOT = (
    "cd {root} && L=$(git -c core.quotepath=off ls-files -m -o --exclude-standard | sort -u | head -n 500"
    ' | while IFS= read -r f; do [ -f "$f" ] && printf "%s\\n" "$f"; done);'
    ' [ -n "$L" ] && printf "%s\\n" "$L" && printf "%s\\n" "$L" | git hash-object --stdin-paths;'
    " echo @@PKC@@; git -c core.quotepath=off ls-files -d"
)


@dataclass
class EditInfo:
    oid: int
    eid: int | None
    step: int
    path: str
    added: int
    removed: int
    first_line: int
    new_file: bool


class FileTracker:
    def __init__(self, env: Environment):
        self.env = env
        self.root = ""
        self.repo_files: set[str] = set()
        self.baseline: dict[str, str] = {}
        self.cur: dict[str, str] = {}
        self.content: dict[str, str] = {}
        self.base: dict[str, str] = {}
        self.edits: list[EditInfo] = []

    @property
    def enabled(self) -> bool:
        return bool(self.root)

    def _run(self, command: str) -> str:
        out = self.env.execute({"command": command})
        return out.get("output", "") if out.get("returncode") == 0 else ""

    def setup(self) -> None:
        self.root = self._run("git rev-parse --show-toplevel 2>/dev/null").strip()
        if not self.root:
            return
        listing = self._run(f"cd {shlex.quote(self.root)} && git -c core.quotepath=off ls-files")
        self.repo_files = set(listing.splitlines())
        self.baseline = self.cur = self._dirty()

    def _dirty(self) -> dict[str, str]:
        head, _, deleted = self._run(_SNAPSHOT.format(root=shlex.quote(self.root))).partition("@@PKC@@")
        rows = head.splitlines()
        return dict(zip(rows[: len(rows) // 2], rows[len(rows) // 2 :])) | {
            p: "deleted" for p in deleted.split("\n") if p.strip()
        }

    def snapshot(self, step: int, oid: int, eid: int | None) -> list[str]:
        """Record the new file versions and log per-file edits; returns the changed paths."""
        if not self.enabled:
            return []
        new = self._dirty()
        changed = sorted(p for p in new.keys() | self.cur.keys() if new.get(p) != self.cur.get(p))
        self.cur = new
        if not changed:
            return []
        sources = [p for p in changed if SRC_EXT.search(p)][:50]
        previous = {p: self.content.get(p, self.head_text(p)) for p in sources}
        for p in sources:
            self.content.pop(p, None)
        current = self.read(sources)
        for p in sources:
            before, after = previous[p].splitlines(), current.get(p, "").splitlines()
            ops = [
                op
                for op in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
                if op[0] != "equal"
            ]
            if ops:
                self.edits.append(
                    EditInfo(
                        oid,
                        eid,
                        step,
                        p,
                        sum(j2 - j1 for _, _, _, j1, j2 in ops),
                        sum(i2 - i1 for _, i1, i2, _, _ in ops),
                        ops[0][3] + 1,
                        p not in self.repo_files and not before,
                    )
                )
        return changed

    def sha(self, path: str) -> str:
        return self.cur.get(path, "HEAD")

    def targets(self) -> set[str]:
        return {
            p
            for p, sha in self.cur.items()
            if sha != self.baseline.get(p) and sha != "deleted" and SRC_EXT.search(p) and not SCRATCH.search(p)
        }

    def exists(self, path: str) -> bool:
        return (path in self.repo_files or path in self.cur) and self.cur.get(path) != "deleted"

    def head_text(self, path: str) -> str:
        if path not in self.base:
            self.base[path] = (
                self._run(f"cd {shlex.quote(self.root)} && git show HEAD:{shlex.quote(path)}")
                if path in self.repo_files
                else ""
            )
        return self.base[path]

    def read(self, paths: list[str]) -> dict[str, str]:
        """Current content of `paths` (cached while the file version does not change)."""
        missing = [p for p in dict.fromkeys(paths) if p not in self.content and self.exists(p)]
        if missing and self.enabled:
            quoted = " ".join(shlex.quote(p) for p in missing)
            out = self._run(
                f"cd {shlex.quote(self.root)} && for f in {quoted}; do printf '{_SEP}%s{_SEP}' \"$f\";"
                ' [ -f "$f" ] && head -c 1000000 -- "$f"; done'
            )
            pieces = out.split(_SEP)[1:]
            for name, body in zip(pieces[0::2], pieces[1::2]):
                self.content[name] = body
        return {p: self.content[p] for p in paths if p in self.content}

    def changed_lines(self, path: str) -> list[tuple[int, int]]:
        """New-side line ranges that differ from HEAD."""
        before, after = self.head_text(path).splitlines(), self.read([path]).get(path, "").splitlines()
        ops = difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
        return [(j1 + 1, max(j1 + 1, j2)) for tag, _, _, j1, j2 in ops if tag != "equal"]

    def shortstat(self) -> str:
        files = sorted(self.targets())
        added = removed = 0
        for p in files:
            before, after = self.head_text(p).splitlines(), self.read([p]).get(p, "").splitlines()
            for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
                if tag != "equal":
                    added, removed = added + j2 - j1, removed + i2 - i1
        return f"{len(files)} files changed, {added} insertions(+), {removed} deletions(-)"
