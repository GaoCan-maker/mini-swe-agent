"""Deterministic summaries of removed history: the per-phase history index and search hit lists."""

import re
from collections import Counter

from minisweagent.agents.pkc.commands import test_key, test_summary
from minisweagent.agents.pkc.ledger import Record

GREP_LINE = re.compile(r"^([\w./@+-]+\.\w+):(\d+)[:-]")


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


def _segment_line(a: int, b: int, label: str | None, recs: list[Record]) -> str:
    reads: Counter[str] = Counter()
    last_read: dict[str, str] = {}
    for r in recs:
        for p in r.read_paths:
            reads[p] += 1
            last_read[p] = r.id
    parts = []
    if reads:
        top = [f"{p} {last_read[p]}" for p, _ in reads.most_common(4)]
        parts.append("read " + ", ".join(top) + (f" (+{len(reads) - 4} files)" if len(reads) > 4 else ""))
    if n := sum(r.category == "search" for r in recs):
        parts.append(f"{n} searches")
    if eids := [f"e{r.eid}" for r in recs if r.eid]:
        parts.append("edits " + " ".join(eids[:8]) + (" ..." if len(eids) > 8 else ""))
    if tests := [r for r in recs if r.category == "test"]:
        t = tests[-1]
        parts.append(f"last test {short(test_key(t.cmd), 50)} {test_summary(t.raw, t.rc)} ({t.id})")
    if n := sum(r.category in ("run", "build") for r in recs):
        parts.append(f"{n} runs/builds")
    head = f"steps {a}-{b}" + (f" (until {label})" if label else "")
    return head + ": " + ("; ".join(parts) if parts else "no commands")


def history_lines(records: list[Record], events: list[tuple[int, str]], hi: int, budget_chars: int) -> list[str]:
    """One line per phase of steps 1..hi; the oldest phases are merged until the lines fit the budget."""
    bounds = sorted({s: label for s, label in events if 0 < s < hi}.items())
    cuts = [(0, None)] + bounds + [(hi, None)]
    segments = [(cuts[i][0] + 1, cuts[i + 1][0], cuts[i + 1][1]) for i in range(len(cuts) - 1)]
    segments = [s for s in segments if s[0] <= s[1]]

    def render(segs):
        return [_segment_line(a, b, label, [r for r in records if a <= r.step <= b]) for a, b, label in segs]

    lines = render(segments)
    while len(segments) > 1 and sum(len(x) + 1 for x in lines) > budget_chars:
        (a, _, _), (_, b, label) = segments[0], segments[1]
        segments = [(a, b, label)] + segments[2:]
        lines = render(segments)
    return lines
