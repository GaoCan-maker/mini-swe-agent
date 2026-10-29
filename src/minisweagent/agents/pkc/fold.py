"""Fold planning (which records become stubs) and deterministic stub text."""

import re
from dataclasses import dataclass, field

from minisweagent.agents.pkc.commands import TEST_PATH, Segment, record_category, test_key, test_summary
from minisweagent.agents.pkc.ledger import Part, Record
from minisweagent.agents.pkc.symbols import NUMBERED, outline

GREP_LINE = re.compile(r"^([\w./@+-]+\.\w+):(\d+)[:-]")
ECHO = re.compile(r"^(?:echo|printf)\s+(?:-\w+\s+)*(['\"]?)(.+?)\1\s*$")


@dataclass
class Labels:
    """Hints from the checkpoint and deltas plus the hot set, as used by the planner."""

    targets: set[str] = field(default_factory=set)
    exemplars: set[str] = field(default_factory=set)
    has_exemplars: bool = False
    ruled_out: dict[str, str] = field(default_factory=dict)
    keep: set[str] = field(default_factory=set)
    hot: set[str] = field(default_factory=set)
    first_keyframe: bool = True
    exemplar_mode: str = "working_set"


@dataclass
class FoldItem:
    rec: Record
    kind: str
    text: str
    saved: int


@dataclass
class FoldPlan:
    items: list[FoldItem]
    lo: int
    hi: int

    @property
    def saved_chars(self) -> int:
        return sum(i.saved for i in self.items)


def short(cmd: str, n: int = 100) -> str:
    line = re.sub(r"\s+", " ", cmd.strip().split("\n")[0])
    return line if len(line) <= n else line[: n - 3] + "..."


def search_summary(text: str, limit: int) -> tuple[int, str]:
    hits: dict[str, list[str]] = {}
    total = 0
    for line in text.splitlines():
        if m := GREP_LINE.match(line):
            hits.setdefault(m.group(1), []).append(m.group(2))
            total += 1
    shown, parts = 0, []
    for path, lines in hits.items():
        if shown >= limit:
            break
        take = lines[: limit - shown]
        parts.append(f"{path}:{','.join(take)}")
        shown += len(take)
    return total, "; ".join(parts) + (f" (+{total - shown})" if total > shown else "")


def _separator(seg: Segment) -> str | None:
    """Text printed by an `echo`/`printf` separator segment, as it appears on its output line."""
    if not (m := ECHO.match(seg.text)) or "%" in m.group(2):
        return None
    return re.sub(r"\\[nt]", " ", m.group(2)).strip() or None


def split_parts(rec: Record) -> list[Part] | None:
    cats = {s.category for s in rec.segments if s.category not in ("other", "git", "edit")}
    if len(cats) < 2 and sum(1 for s in rec.segments if s.read) < 2:
        return None
    lines = rec.raw.splitlines(keepends=True)
    seps = {sep for s in rec.segments if (sep := _separator(s))}
    groups: list[list[str]] = []
    segs: list[list[Segment]] = [[]]
    if seps and any(line.strip() in seps for line in lines):
        for line in lines:
            if line.strip() in seps or not groups:
                groups.append([])
            groups[-1].append(line)
        for s in rec.segments:
            if _separator(s) in seps:
                segs.append([])
            else:
                segs[-1].append(s)
    else:
        kind = None
        for line in lines:
            k = "search" if GREP_LINE.match(line) else "read" if NUMBERED.match(line) else "other"
            if k != kind and (not groups or len(groups[-1]) >= 3):
                groups.append([])
                kind = k
            groups[-1].append(line)
    if len(groups) < 2:
        return None
    segs = segs[1:] if not segs[0] else segs
    aligned = len(segs) == len(groups)
    read_paths = iter(rec.read_paths)
    parts = []
    for i, g in enumerate(groups):
        if aligned:
            cat = record_category(segs[i])
            path = next((s.read[0] for s in segs[i] if s.read), None)
        else:
            kinds = ["search" if GREP_LINE.match(x) else "read" if NUMBERED.match(x) else "other" for x in g]
            cat = max(set(kinds), key=kinds.count)
            path = next(read_paths, None) if cat == "read" else None
        parts.append(Part(f"{rec.id}.{i + 1}", "".join(g), cat, path))
    return parts


def _covered(a: tuple[str, int, int | None], b: tuple[str, int, int | None]) -> bool:
    return a[0] == b[0] and b[1] <= a[1] and (b[2] is None or (a[2] is not None and a[2] <= b[2]))


class Planner:
    def __init__(
        self, records: list[Record], labels: Labels, search_hits: int, outline_defs: int, edit_stats: dict[int, str]
    ):
        self.records, self.labels, self.edit_stats = records, labels, edit_stats
        self.search_hits, self.outline_defs = search_hits, outline_defs

    def plan(self, lo: int, hi: int, fold_edits: bool, edit_min_chars: int) -> FoldPlan:
        items = []
        for rec in self.records:
            if not (lo < rec.step <= hi) or rec.obs_msg is None:
                continue
            shown = len(rec.base_content)
            if rec.stub is None and rec.id not in self.labels.keep and (text := self._fold_output(rec)) is not None:
                if (saved := min(shown, len(rec.raw) + 100) - len(text) - 100) > 80 and len(text) < 0.7 * shown:
                    items.append(FoldItem(rec, "obs", text, saved))
            if fold_edits and rec.eid and rec.rc == 0 and rec.cmd_stub is None and len(rec.cmd) >= edit_min_chars:
                stub = self.edit_stub(rec, self.edit_stats.get(rec.eid, ", ".join(rec.edit_files[:3])))
                items.append(FoldItem(rec, "edit", stub, len(rec.cmd) - len(stub)))
        return FoldPlan(items, lo, hi)

    def _fold_output(self, rec: Record) -> str | None:
        if rec.id in self.labels.ruled_out:
            return f"[{rec.id} ruled out: {self.labels.ruled_out[rec.id]}] {short(rec.cmd)} | ctx show {rec.id}"
        rec.parts = rec.parts or split_parts(rec)
        if not rec.parts:
            return self._fold_unit(rec, rec.id, rec.raw, rec.category, rec.read_paths[0] if rec.read_paths else None)
        texts = [self._fold_unit(rec, p.pid, p.text, p.category, p.path) for p in rec.parts]
        if all(t is None for t in texts):
            return None
        return "".join(p.text if t is None else t + "\n" for p, t in zip(rec.parts, texts))

    def _fold_unit(self, rec: Record, pid: str, text: str, category: str, path: str | None) -> str | None:
        cmd = short(rec.cmd)
        if re.fullmatch(r"ctx\s+show\s+\S+.*", rec.cmd.strip()) and (
            m := re.search(r"ctx\s+show\s+([^\s;&|)]+)", rec.cmd)
        ):
            return f"[{pid} folded] restored copy of {m.group(1)} | ctx show {m.group(1)}"
        if category in ("test", "run", "build"):
            return self._fold_test(rec, pid, text)
        if category == "read" and path is not None:
            return self._fold_read(rec, pid, text, path)
        n_lines = text.count("\n") + 1
        total, hits = search_summary(text, self.search_hits)
        if total:
            return f"[{pid} folded] {cmd} -> {total} hits: {hits} | ctx show {pid}"
        return f"[{pid} folded] {cmd} -> {n_lines} lines | ctx show {pid}"

    def _fold_test(self, rec: Record, pid: str, text: str) -> str | None:
        key = test_key(rec.cmd)
        later = next(
            (r for r in self.records if r.oid > rec.oid and r.category == rec.category and test_key(r.cmd) == key), None
        )
        if later is None:
            return None
        return f"[{pid} superseded by {later.id}] {short(rec.cmd)} -> {test_summary(text, rec.rc)} | ctx show {pid}"

    def _fold_read(self, rec: Record, pid: str, text: str, path: str) -> str | None:
        labels = self.labels
        whole = pid == rec.id
        paths = rec.read_paths if whole else [path]
        if any(p in labels.hot for p in paths):
            return None
        mine = next((s.read for s in rec.segments if s.read and s.read[0] == path), (path, 1, None))
        reads = [s.read for s in rec.segments if s.read] if whole else [mine]
        later = [
            next(
                (
                    r
                    for r in self.records
                    if r.oid > rec.oid and any(s.read and _covered(m, s.read) for s in r.segments)
                ),
                None,
            )
            for m in reads
        ]
        if reads and all(later):
            return f"[{pid} superseded by {max(later, key=lambda r: r.oid).id}] {short(rec.cmd)} | ctx show {pid}"
        if labels.exemplar_mode == "keep" and any(p in labels.exemplars for p in paths):
            return None
        is_target = path in labels.targets
        same_dir = any(t.rpartition("/")[0] == path.rpartition("/")[0] for t in labels.targets)
        if (
            not is_target
            and not labels.has_exemplars
            and labels.first_keyframe
            and (TEST_PATH.search(path) or same_dir)
        ):
            return None
        rec.outline = outline(text, mine[1], self.outline_defs)
        shown = ", ".join(f"{n} L{a}-{b}" for n, a, b in rec.outline)
        n_lines = text.count("\n") + 1
        return (
            f"[{pid} folded] {short(rec.cmd)} -> {n_lines} lines"
            + (f" | outline: {shown}" if shown else "")
            + f" | ctx show {pid}"
        )

    @staticmethod
    def edit_stub(rec: Record, stats: str) -> str:
        tool = short(rec.cmd, 40).split(" ")[0]
        return (
            f"# [e{rec.eid} folded] {tool} edit -> {stats}, rc=0. "
            f"Current code: keyframe working set or ctx show <path>::<symbol>. Original: ctx show e{rec.eid}"
        )
