"""Working set (current code re-read from disk) and keyframe message assembly."""

from dataclasses import dataclass, field

from minisweagent.agents.pkc.symbols import Symbol

FOOTER = (
    "\nFolded items keep a one-line stub in place; restore any of them with: "
    "ctx show <id> | ctx show <path>::<symbol> | ctx show <path>:<a>-<b>\n</keyframe>"
)


@dataclass
class Candidate:
    prio: int
    path: str
    sym: Symbol


@dataclass
class Block:
    prio: int
    path: str
    names: list[str]
    start: int
    end: int


@dataclass
class WorkingSet:
    text: str
    symbols: list[str] = field(default_factory=list)


@dataclass
class Keyframe:
    n: int
    step: int
    frozen: str
    ws: WorkingSet
    superseded: str | None = None

    def __str__(self) -> str:
        return self.frozen + (self.superseded if self.superseded is not None else self.ws.text) + FOOTER

    def supersede(self, by: int) -> int:
        """Replace the working set by a one-line stub; returns the number of characters removed."""
        names = ", ".join(self.ws.symbols[:12]) + (" ..." if len(self.ws.symbols) > 12 else "")
        self.superseded = (
            f"[K{self.n} working set superseded by K{by}] {names or '(empty)'} | "
            "current code: newer keyframe working set or ctx show <path>::<symbol>\n"
        )
        return len(self.ws.text) - len(self.superseded)


def build_working_set(
    cands: list[Candidate],
    contents: dict[str, str],
    visible: list[tuple[str, str]],
    *,
    budget_chars: int,
    max_lines: int,
    step: int,
    sha: dict[str, str],
) -> WorkingSet:
    lines_of = {p: t.splitlines() for p, t in contents.items()}
    seen, kept, see = set(), [], []
    for c in sorted(cands, key=lambda c: c.prio):
        key = (c.path, c.sym.start, c.sym.end)
        if c.path not in lines_of or key in seen:
            continue
        seen.add(key)
        body = "\n".join(lines_of[c.path][c.sym.start - 1 : c.sym.end])
        if c.sym.end - c.sym.start >= 2 and (rid := next((r for r, v in visible if body in v), None)):
            see.append(f"{c.path}::{c.sym.name}   see {rid} (current)")
            continue
        kept.append(c)
    blocks = _merge(_nest(kept, max_lines), max_lines)
    out, names, used = [], [], sum(len(s) + 1 for s in see)
    for b in sorted(blocks, key=lambda b: (b.prio, b.path, b.start)):
        text = _render(b, lines_of[b.path], max_lines, step, sha.get(b.path, "HEAD"))
        if used + len(text) > budget_chars:
            continue
        out.append(text)
        names += [f"{b.path}::{n}" for n in b.names]
        used += len(text)
    body = "".join(out) + "".join(s + "\n" for s in see)
    return WorkingSet(
        "## Working set (current code, re-read from disk)            [replaceable at next keyframe]\n" + body, names
    )


def _nest(cands: list[Candidate], max_lines: int) -> list[Block]:
    out = []
    for c in cands:
        outer = [o for o in cands if o.path == c.path and o.sym.contains(c.sym)]
        if any(o.sym.end - o.sym.start < max_lines for o in outer):
            continue
        inner = [i for i in cands if i.path == c.path and c.sym.contains(i.sym)]
        prio = min([c.prio, *(i.prio for i in inner)])
        end = c.sym.start if inner and c.sym.end - c.sym.start >= max_lines else c.sym.end
        out.append(Block(prio, c.path, [c.sym.name], c.sym.start, end))
    return out


def _merge(blocks: list[Block], max_lines: int) -> list[Block]:
    out: list[Block] = []
    for b in sorted(blocks, key=lambda b: (b.path, b.start)):
        last = out[-1] if out else None
        if last and last.path == b.path and b.start <= last.end + 4 and max(last.end, b.end) - last.start < max_lines:
            last.end, last.prio = max(last.end, b.end), min(last.prio, b.prio)
            last.names += [n for n in b.names if n not in last.names]
        else:
            out.append(Block(b.prio, b.path, list(b.names), b.start, b.end))
    return out


def _render(b: Block, lines: list[str], max_lines: int, step: int, sha: str) -> str:
    nums = list(range(b.start, min(b.end, len(lines)) + 1))
    head = f"=== {b.path} :: {', '.join(b.names)}  [L{b.start}-{b.end}, sha {sha[:7]}, as of step {step}] ===\n"
    if len(nums) > max_lines:
        omitted = f"... (lines {nums[40]}-{nums[-41]} omitted; ctx show {b.path}::{b.names[0]})\n"
        return head + _numbered(nums[:40], lines) + omitted + _numbered(nums[-40:], lines)
    return head + _numbered(nums, lines)


def _numbered(nums: list[int], lines: list[str]) -> str:
    return "".join(f"{n}\t{lines[n - 1]}\n" for n in nums)


def frozen_text(n: int, step: int, reason: str, lo: int, hi: int, sections: list[tuple[str, list[str], int]]) -> str:
    """`sections` holds (title, lines, character budget); empty sections are omitted."""
    head = (
        f'\n\n<keyframe id="K{n}" step="{step}" reason="{reason}" folded_from_step="{lo + 1}" folded_to_step="{hi}">\n'
    )
    out = []
    for title, lines, budget in sections:
        if not lines:
            continue
        text = ""
        for line in lines:
            if len(text) + len(line) + 1 > budget:
                text += "...\n"
                break
            text += line + "\n"
        out.append(f"## {title}\n{text}")
    return head + "".join(out)
