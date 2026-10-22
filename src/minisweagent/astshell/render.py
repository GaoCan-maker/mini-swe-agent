"""Rendering of read and search blocks. Output items are file line numbers (int) or placeholder messages (str)."""

import hashlib
import posixpath
import re
from collections.abc import Callable
from dataclasses import dataclass, field, replace

from minisweagent.astshell.command import SearchSpec, head_lines
from minisweagent.astshell.config import ASTShellConfig
from minisweagent.astshell.symbols import FUNCTION_KINDS, ParsedFile, Symbol, calls_of

PLACEHOLDER = " " * 6 + "\t[ast-shell] "
SKEL_HEAD = "outline: bodies folded"
_NUMBERED = re.compile(r"^ *(\d+)\t(.*)$", re.S)
_ANCHOR = re.compile(r"^L(\d+)-(\d+)(:| prepended:)")
_PREPENDED = re.compile(r"^L\d+-\d+ prepended:")
Item = int | str


@dataclass
class Rendered:
    text: str
    ops: list[str] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    alts: list["Rendered"] = field(default_factory=list)
    start: int | None = None
    """First line of an anchored (unnumbered) rendering; None for numbered output."""


@dataclass
class ReadCtx:
    pf: ParsedFile
    path: str
    cfg: ASTShellConfig
    hits: set[int] = field(default_factory=set)
    task_names: set[str] = field(default_factory=set)
    seen_turns_ago: Callable[[Symbol], int | None] = lambda sym: None
    elsewhere: dict[int, str] = field(default_factory=dict)
    """Lines that other reads of the same file in the same command show -> "shown above" or "shown below"."""


def symbol_sha(pf: ParsedFile, sym: Symbol) -> str:
    return hashlib.sha1(pf.text(sym.start, sym.end).encode()).hexdigest()


def to_text(items: list[Item], lines: list[str], numbered: bool = True) -> str:
    fmt = "{:>6}\t{}\n" if numbered else "{1}\n"
    return "".join(fmt.format(i, lines[i - 1]) if isinstance(i, int) else f"{PLACEHOLDER}{i}\n" for i in items)


def _next_line(message: str, cur: int) -> int:
    """Line number of the code line following a placeholder in anchored output."""
    m = _ANCHOR.match(message)
    return cur if m is None else int(m[1]) if m[3] == " prepended:" else int(m[2]) + 1


def anchor(items: list[Item], start: int) -> list[Item] | None:
    """Make every line number derivable from `start` and placeholders by filling silent gaps."""
    out, cur = [], start
    for i in items:
        if isinstance(i, str):
            cur = _next_line(i, cur)
        elif i < cur:
            return None
        else:
            if i > cur:
                out.append(f"L{cur}-{i - 1}: omitted")
            cur = i + 1
        out.append(i)
    return out


def numbered_items(items: list[Item], lines: list[str], skeleton: bool) -> list[Item]:
    """Numbered variant: line ranges in M-SNAP-UP are redundant and skeletons drop blank lines."""
    out: list[Item] = []
    for i in items:
        if isinstance(i, str):
            out.append(f"{SKEL_HEAD}, line numbers real" if i == SKEL_HEAD else _PREPENDED.sub("prepended:", i))
        elif not (skeleton and not lines[i - 1].strip()):
            out.append(i)
    return out


def verify(text: str, lines: list[str], start: int | None = None) -> bool:
    cur = start
    for line in text.split("\n")[:-1]:
        if line.startswith(PLACEHOLDER):
            if cur is not None:
                cur = _next_line(line[len(PLACEHOLDER) :], cur)
            continue
        if cur is not None:
            if not 0 < cur <= len(lines) or lines[cur - 1] != line:
                return False
            cur += 1
            continue
        m = _NUMBERED.match(line)
        if not m or not 0 < int(m[1]) <= len(lines) or lines[int(m[1]) - 1] != m[2]:
            return False
    return True


def runs_of(seq: list[int]) -> list[tuple[int, int]]:
    runs: list[list[int]] = []
    for ln in seq:
        if runs and ln == runs[-1][1] + 1:
            runs[-1][1] = ln
        else:
            runs.append([ln, ln])
    return [(a, b) for a, b in runs]


def _closes(pf: ParsedFile, sym: Symbol) -> bool:
    return pf.lang != "python" and sym.end > sym.sig_end and pf.lines[sym.end - 1].strip().startswith("}")


def _body_lines(pf: ParsedFile, sym: Symbol) -> int:
    return sym.end - sym.sig_end - _closes(pf, sym)


# ---------------------------------------------------------------- SnapRead


@dataclass
class Window:
    lo: int
    hi: int
    pre: list[str] = field(default_factory=list)
    post: list[str] = field(default_factory=list)
    folds: dict[int, str] = field(default_factory=dict)
    """Folded line -> name of the symbol whose tail it belongs to."""
    ops: list[str] = field(default_factory=list)
    focus: set[Symbol] = field(default_factory=set)


def snap(ctx: ReadCtx, x: int, y: int) -> Window:
    """Align the window [x, y] to symbol boundaries."""
    pf, cfg = ctx.pf, ctx.cfg
    top, bot = pf.leaf_at(x), pf.leaf_at(y)
    top = top if top is not None and top.start < x else None
    bot = bot if bot is not None and bot.end > y else None
    starts_inside = any(x < s.start <= y for s in pf.symbols)
    lo, hi, pre, post, ops, focus = x, y, [], [], [], set()
    folds: dict[int, str] = {}
    if top is not None:
        if any(top.start <= h <= top.end for h in ctx.hits) or not starts_inside:
            focus.add(top)
            if x - top.doc_start <= cfg.snap_up_max:
                lo = top.doc_start
            elif x - top.start <= cfg.snap_up_max:
                lo = top.start
            if lo < x:
                pre.append(f"L{lo}-{x - 1} prepended: header of {top.name}")
                ops.append("snap_up")
            else:
                pre.append(f"inside {top.name} L{top.start}-{top.end}")
                ops.append("inside")
        elif min(top.end, y) - x + 1 >= cfg.tail_fold_min:
            folds = dict.fromkeys(range(x, min(top.end, y) + 1), top.name)
            ops.append("fold_tail")
    if bot is not None:
        bot_hit = any(bot.start <= h <= bot.end for h in ctx.hits)
        if y - bot.start + 1 <= 3 and not bot_hit and bot is not top:
            post.append(f"{bot.name} continues to L{bot.end}")
            ops.append("cont")
        elif bot.end - y <= cfg.snap_down_max:
            hi = bot.end
            focus.add(bot)
            post.append(f"extended to end of {bot.name}")
            ops.append("snap_down")
        else:
            focus.add(bot)
            post.append(f"{bot.name} continues to L{bot.end}")
            ops.append("cont")
    return Window(lo, hi, pre, post, folds, ops, focus)


def merge_windows(windows: list[Window]) -> list[Item]:
    """Merge overlapping or adjacent windows so that no line and no boundary message is emitted twice."""
    groups: list[list[Window]] = []
    for w in sorted(windows, key=lambda w: w.lo):
        if groups and w.lo <= max(g.hi for g in groups[-1]) + 1:
            groups[-1].append(w)
        else:
            groups.append([w])
    items: list[Item] = []
    for group in groups:
        shown = {i for w in group for i in range(w.lo, w.hi + 1) if i not in w.folds}
        folds = {i: name for w in group for i, name in w.folds.items() if i not in shown}
        lo, hi = group[0].lo, max(w.hi for w in group)
        items += group[0].pre
        i = lo
        while i <= hi:
            if i in folds:
                j = i
                while j + 1 in folds and folds[j + 1] == folds[i]:
                    j += 1
                items.append(f"L{i}-{j}: end of {folds[i]} omitted")
                i = j + 1
                continue
            items.append(i)
            i += 1
        items += max(reversed(group), key=lambda w: w.hi).post
    return items


def collapse_elsewhere(items: list[Item], requested: set[int], elsewhere: dict[int, str]) -> list[Item]:
    """Replace added (not requested) lines that another read of the same command shows with one placeholder."""
    out: list[Item] = []
    run: list[int] = []

    def flush():
        if run:
            out.append(f"L{run[0]}-{run[-1]}: {elsewhere[run[0]]}")
            run.clear()

    for i in items:
        if isinstance(i, int) and i not in requested and i in elsewhere:
            if run and (i != run[-1] + 1 or elsewhere[i] != elsewhere[run[0]]):
                flush()
            run.append(i)
            continue
        flush()
        out.append(i)
    flush()
    return out


# ---------------------------------------------------------------- skeleton


class _Skeleton:
    def __init__(self, ctx: ReadCtx, level: int):
        self.ctx, self.pf, self.cfg, self.level, self.out = ctx, ctx.pf, ctx.cfg, level, []

    def fold(self, a: int, b: int, keep: set[int], desc: str):
        i = a
        while i <= b:
            if i in keep:
                self.out.append(i)
                i += 1
                continue
            j = i
            while j + 1 <= b and j + 1 not in keep:
                j += 1
            if j - i + 1 < 3:
                self.out.extend(range(i, j + 1))
            else:
                self.out.append(f"L{i}-{j}: {desc} folded")
            i = j + 1

    def gap(self, a: int, b: int, container: Symbol | None, desc: str = ""):
        if a > b:
            return
        if container is not None:
            desc, limit, head, tail = desc or "fields", self.cfg.gap_max_lines, 15, 10
            if self.level >= 2:
                limit, head, tail = 2, 0, 0
        elif a == 1:
            desc, limit, head, tail = "imports", self.cfg.header_max_lines, 40, 10
            if self.level >= 2:
                limit, head, tail = 10, 5, 2
        else:
            desc, limit, head, tail = desc or "top-level statements", self.cfg.gap_max_lines, 8, 3
            if self.level >= 2:
                limit, head, tail = 4, 1, 0
        if b - a + 1 <= limit:
            self.out.extend(range(a, b + 1))
            return
        keep = set(range(a, a + head)) | set(range(b - tail + 1, b + 1))
        if self.level == 1:
            keep |= {i for h in self.ctx.hits for i in range(h - 2, h + 3)}
        self.fold(a, b, keep, desc)

    def region(self, lo: int, hi: int, syms: list[Symbol], container: Symbol | None):
        pos = lo
        for s in syms:
            a, b = max(s.doc_start, lo), min(s.end, hi)
            if a > b or b < pos:
                continue
            a = max(a, pos)
            self.gap(pos, a - 1, container)
            self.symbol(s, a, b)
            pos = b + 1
        self.gap(pos, hi, container)

    def symbol(self, s: Symbol, a: int, b: int):
        pf, closes = self.pf, _closes(self.pf, s)
        if not s.is_leaf:
            head_end = min(s.sig_end, b)
            if self.level >= 2:
                self.out.extend(i for i in range(max(a, s.start), min(s.start, head_end) + 1))
            else:
                self.out.extend(range(a, head_end + 1))
            inner_hi = b - 1 if closes and b == s.end else b
            self.region(max(head_end + 1, a), inner_hi, s.children, s)
            if closes and b == s.end and inner_hi >= head_end:
                self.out.append(s.end)
            return
        if s.kind == "block":
            self.gap(a, b, None, f"{s.name} block")
            return
        if self.level == 1 and (
            any(s.start <= h <= s.end for h in self.ctx.hits)
            or _body_lines(pf, s) <= self.cfg.body_keep_max
            or (len(s.short_name) >= 4 and s.short_name in self.ctx.task_names)
            or s.has_error
        ):
            self.out.extend(range(a, b + 1))
            return
        if self.level >= 2:
            head = [s.start] if a <= s.start <= b else []
            body_lo = max(s.start + 1, a)
        else:
            head = list(range(a, min(s.sig_end, b) + 1))
            body_lo = max(s.sig_end + 1, a)
            if s.doc_line == body_lo and body_lo <= b:
                head.append(body_lo)
                body_lo += 1
        body_hi = min(s.end - closes, b)
        if body_hi - body_lo + 1 < 2:
            self.out.extend(range(a, b + 1))
            return
        calls = ""
        if self.level == 1 and self.cfg.skeleton_calls and s.kind in FUNCTION_KINDS:
            names = calls_of(s, pf, self.cfg.calls_max)
            calls = f"; calls: {', '.join(names)}" if names else ""
        self.out.extend(head)
        self.out.append(f"L{body_lo}-{body_hi}: body folded{calls}")
        if closes and b == s.end:
            self.out.append(s.end)


def skeleton(ctx: ReadCtx, lo: int, hi: int, level: int) -> list[Item]:
    sk = _Skeleton(ctx, min(level, 2))
    sk.region(lo, hi, ctx.pf.top, None)
    items: list[Item] = [SKEL_HEAD, *sk.out]
    if level < 3:
        return items
    out, size, limit = [], 0, ctx.cfg.block_max_chars
    starts = {s.start for s in ctx.pf.symbols}
    for k, item in enumerate(items):
        size += len(item) + 13 if isinstance(item, str) else len(ctx.pf.lines[item - 1]) + 8
        if size > limit:
            rest = sum(1 for i in items[k:] if isinstance(i, int) and i in starts)
            return out + [f"{rest} more symbols omitted"]
        out.append(item)
    return out


# ---------------------------------------------------------------- SeenFold


def seen_fold(ctx: ReadCtx, items: list[Item], protect: set[Symbol]) -> tuple[list[Item], bool]:
    if not ctx.cfg.seen_fold:
        return items, False
    shown = {i for i in items if isinstance(i, int)}
    candidates = [
        s
        for s in ctx.pf.symbols
        if s.is_leaf
        and s.kind in FUNCTION_KINDS
        and s not in protect
        and _body_lines(ctx.pf, s) > ctx.cfg.body_keep_max
        and all(i in shown for i in range(s.start, s.end + 1))
        and not any(s.start <= h <= s.end for h in ctx.hits)
    ]
    if len(candidates) == len([s for s in ctx.pf.symbols if s.is_leaf and s.start in shown]) == 1:
        return items, False
    folds = {}
    for s in candidates:
        if (d := ctx.seen_turns_ago(s)) is not None:
            folds[s.start] = (s, "shown above" if d == 0 else f"shown {d} turns ago")
    if not folds:
        return items, False
    out, skip_to = [], 0
    for item in items:
        if isinstance(item, int):
            if item <= skip_to:
                continue
            if item in folds:
                s, ago = folds[item]
                out.append(f"L{s.start}-{s.end}: {s.name} unchanged, {ago}")
                skip_to = s.end
                continue
        out.append(item)
    return out, True


# ---------------------------------------------------------------- reads


def render_read(ctx: ReadCtx, seq: list[int], raw: str, *, start: int | None, repeat: bool) -> Rendered:
    """Primary rendering of a read block plus shorter alternatives for the budget guard.

    `start` is the first line the agent knows its unnumbered command prints; None forces numbered output.
    """
    pf, cfg = ctx.pf, ctx.cfg
    unchanged = Rendered(raw, [], list(seq))
    runs = runs_of(seq)
    if not runs:
        return unchanged

    def make(items: list[Item], ops: list[str], protect: set[Symbol] = frozenset()) -> Rendered:
        if not repeat:
            items, folded = seen_fold(ctx, items, protect)
            ops = [*ops, "seen_fold"] if folded else ops
        skel = any(op.startswith("skeleton") for op in ops)
        numbered = Rendered(to_text(numbered_items(items, pf.lines, skel), pf.lines), ops, items)
        anchored = anchor(items, start) if start is not None else None
        if anchored is None:
            return numbered
        plain = Rendered(to_text(anchored, pf.lines, False), ops, items, start=start)
        return plain if len(plain.text) <= len(numbered.text) and verify(plain.text, pf.lines, start) else numbered

    first_top = pf.top[0].doc_start if pf.top else 1
    lo, hi = runs[0][0], runs[-1][1]
    whole = len(runs) == 1 and lo <= max(1, first_top) and hi >= 0.8 * len(pf.lines)
    levels = [make(skeleton(ctx, lo, hi, lvl), [f"skeleton_l{lvl}"]) for lvl in (1, 2, 3)] if cfg.skeleton else []
    if whole:
        if levels and len(raw) > cfg.skeleton_min_chars:
            k = next((i for i, r in enumerate(levels) if len(r.text) <= cfg.block_max_chars), len(levels) - 1)
            return replace(levels[k], alts=levels[k + 1 :])
        unchanged.alts = levels
        return unchanged
    primary = unchanged
    if cfg.snap_read:
        windows = [snap(ctx, x, min(y, len(pf.lines))) for x, y in runs]
        ops = [op for w in windows for op in w.ops]
        focus = {s for w in windows for s in w.focus}
        merged = merge_windows(windows)
        items = collapse_elsewhere(merged, set(seq), ctx.elsewhere)
        if items != merged:
            ops.append("dedup")
        if ops:
            primary = make(items, ops, focus)
        elif cfg.seen_fold:
            folded = make(list(seq), [])
            primary = folded if folded.ops else unchanged
    primary.alts = levels
    return primary


# ---------------------------------------------------------------- search


@dataclass
class Match:
    kind: str
    raw: str
    path: str = ""
    abspath: str = ""
    line: int = 0
    text: str = ""
    valid: bool = False
    tier: int = 1
    ann: str = ""


_PREFIXED = re.compile(r"^(.+?):(\d+):(.*)$", re.S)
_SINGLE = re.compile(r"^(\d+)([:-])(.*)$", re.S)


def parse_search(raw: str, spec: SearchSpec, pwd: str) -> list[Match]:
    lines = raw.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    out: list[Match] = []
    single = spec.single_file is not None and all(l == "--" or _SINGLE.match(l) for l in lines)
    known: list[str] = []
    for line in lines:
        if line == "--":
            out.append(Match("sep", line))
        elif single and (m := _SINGLE.match(line)):
            out.append(Match("match" if m[2] == ":" else "ctx", line, spec.single_file, "", int(m[1]), m[3]))
        elif m := _PREFIXED.match(line):
            out.append(Match("match", line, m[1], "", int(m[2]), m[3]))
            if m[1] not in known:
                known.append(m[1])
        else:
            out.append(Match("other", line))
    for m in out:
        if m.kind == "other":
            for path in known:
                if (c := re.match(rf"^{re.escape(path)}-(\d+)-(.*)$", m.raw, re.S)) is not None:
                    m.kind, m.path, m.line, m.text = "ctx", path, int(c[1]), c[2]
                    break
        if m.path:
            m.abspath = posixpath.normpath(posixpath.join(pwd, m.path))
    return out


def _tier(m: Match, pf: ParsedFile | None, test_re: re.Pattern) -> tuple[int, str]:
    """0 definition, 1 source, 2 comment, 3 test; with the annotation text."""
    if pf is None:
        return 1, ""
    sym = pf.innermost[m.line] if m.line < len(pf.innermost) else None
    is_def = sym is not None and sym.start <= m.line <= sym.sig_end
    test = bool(test_re.search(m.path))
    tag = "test " if test else ("comment " if m.line in pf.comment_only else "")
    ann = (
        ""
        if sym is None
        else (f"[{tag}def L{sym.start}-{sym.end}]" if is_def else f"[{tag}in {sym.name} L{sym.start}-{sym.end}]")
    )
    return (3 if test else 0 if is_def else 2 if m.line in pf.comment_only else 1), ann


def _annotated(m: Match, compact: bool) -> str:
    if not m.ann:
        return f"  {m.line}:{m.text}" if compact else m.raw
    if compact:
        return f"  {m.line}: {m.ann} {m.text}"
    prefix = m.raw[: len(m.raw) - len(m.text)]
    return f"{prefix} {m.ann} {m.text}"


def render_search(
    matches: list[Match], spec: SearchSpec, files: dict[str, ParsedFile | None], cfg: ASTShellConfig, truncated: bool
) -> Rendered | None:
    test_re = re.compile("|".join(cfg.test_path_patterns))
    by_file: dict[str, list[Match]] = {}
    for m in matches:
        if m.kind in ("match", "ctx"):
            pf = files.get(m.abspath)
            m.valid = pf is not None and 0 < m.line <= len(pf.lines) and pf.lines[m.line - 1] == m.text
            by_file.setdefault(m.abspath, []).append(m)
    for group in by_file.values():
        if sum(not m.valid for m in group) * 2 > len(group):
            for m in group:
                m.valid = False
    hits = [m for m in matches if m.kind == "match"]
    if not hits:
        return None
    for m in hits:
        m.tier, m.ann = _tier(m, files.get(m.abspath) if m.valid else None, test_re)
    k = spec.head_k
    rerank = (
        cfg.grep_rerank
        and not spec.has_context
        and all(m.kind == "match" for m in matches)
        and (len(hits) > k if k is not None else len(hits) > cfg.grep_rerank_min)
    )
    if not rerank:
        shown = matches[:k] if k is not None else matches
        if not any(m.ann for m in shown if m.kind == "match"):
            return None
        text = "".join(_annotated(m, False) + "\n" if m.kind == "match" else m.raw + "\n" for m in shown)
        return Rendered(text, ["annotate"])
    ordered = sorted(hits, key=lambda m: m.tier)
    entries: list[Match | str] = []
    per_symbol: dict[str, list[Match]] = {}
    for m in ordered:
        key = m.ann.rsplit(" L", 1)[0] if m.ann and m.tier != 0 else ""
        if key:
            per_symbol.setdefault(f"{m.abspath}|{key}", []).append(m)
    collapsed: set[int] = set()
    extra: dict[int, str] = {}
    for group in per_symbol.values():
        if len(group) > 3:
            for m in group[2:]:
                collapsed.add(id(m))
            name = re.search(r"in (.+) L\d+-\d+\]$", group[0].ann)[1]
            lines = ",".join(f"L{m.line}" for m in group[2:10]) + (",..." if len(group) > 10 else "")
            extra[id(group[1])] = f"+{len(group) - 2} more in {name}: {lines}"
    for m in ordered:
        if id(m) in collapsed:
            continue
        entries.append(m)
        if id(m) in extra:
            entries.append(extra[id(m)])

    def build(limit: int | None) -> Rendered:
        shown, n_matches = [], 0
        for e in entries:
            if isinstance(e, Match):
                if limit is not None and n_matches >= limit:
                    break
                n_matches += 1
            shown.append(e)
        shown_ids = {id(e) for e in shown if isinstance(e, Match)}
        omitted = [m for m in ordered if id(m) not in shown_ids and id(m) not in collapsed]
        out, current = [], None
        for e in shown:
            if isinstance(e, str):
                out.append(f"{PLACEHOLDER}{e}")
                continue
            if e.path != current:
                out.append(e.path)
                current = e.path
            out.append(_annotated(e, True))
        if omitted:
            counts = {
                name: sum(m.tier == t for m in omitted) for t, name in enumerate(("def", "src", "comment", "test"))
            }
            desc = ", ".join(f"{v} {name}" for name, v in counts.items() if v)
            total = f"{len(hits)}+" if truncated else str(len(hits))
            out.append(f"{PLACEHOLDER}{n_matches}/{total} matches shown, defs first, tests last; omitted: {desc}")
        return Rendered("\n".join(out) + "\n", ["annotate", "rerank"] + (["omit"] if omitted else []))

    primary = build(k)
    n = k if k is not None else len(entries)
    while n > 5:
        n //= 2
        primary.alts.append(build(n))
    return primary


def search_fallback(raw: str, spec: SearchSpec, enlarged: bool) -> str:
    return head_lines(raw, spec.head_k) if enlarged and spec.head_k is not None else raw
