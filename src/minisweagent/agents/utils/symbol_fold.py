"""Fold code regions of overflowing command outputs down to symbol signatures.

Symbols (functions, classes, ...) whose code mentions identifiers from the agent's current intent are kept in full,
everything else is reduced to its signature followed by a `... [A-B]` marker with the hidden file line range.
Output that does not come from a supported code-file read (grep, git diff, test logs, ...) is kept verbatim.
"""

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from pydantic import BaseModel


class FoldConfig(BaseModel):
    fold_budget: int = 9800
    """Folded output must stay below the observation template's 10000-char truncation threshold."""
    keep_small_chars: int = 1500
    """Code regions up to this size are shown verbatim."""
    prelude_chars: int = 1500
    """Max chars of module-level code shown before the first symbol of a region."""
    gap_lines: int = 3
    """Code between symbols that is longer than this is folded."""
    max_open_share: float = 0.4
    """A matching leaf symbol larger than this share of the budget is only shown around the matching lines."""
    hit_window: int = 10
    """Context lines around matching lines of a partially shown symbol."""
    intent_history: int = 6
    """Number of previous commands whose identifiers count as intent."""
    max_term_df: float = 0.2
    """Ignore intent terms that occur in more than this share of code lines."""
    locate_timeout: int = 10
    """Timeout for re-running an unnumbered read command to locate its output."""


HEADER = '[folded code; "... [A-B]" = hidden lines A-B]\n'
LANGS = {".py": "python", ".ts": "typescript", ".tsx": "tsx", ".js": "javascript", ".jsx": "javascript",
         ".mjs": "javascript", ".cjs": "javascript", ".rs": "rust", ".go": "go"}  # fmt: skip
BRACE_WRAPPER = {"typescript": "class __Fold__ {", "tsx": "class __Fold__ {", "javascript": "class __Fold__ {",
                 "rust": "impl __Fold__ {"}  # fmt: skip
SYMBOL_TYPES = {
    "function_definition", "class_definition", "function_declaration", "generator_function_declaration",
    "class_declaration", "abstract_class_declaration", "method_definition", "interface_declaration",
    "enum_declaration", "type_alias_declaration", "function_item", "impl_item", "trait_item", "struct_item",
    "enum_item", "mod_item", "method_declaration", "type_declaration",
}  # fmt: skip
FUNCTION_VALUES = {"arrow_function", "function_expression", "function"}
CONTAINER_TYPES = {"class_definition", "class_declaration", "abstract_class_declaration", "impl_item", "trait_item",
                   "mod_item"}  # fmt: skip
START_WRAPPERS = {"decorated_definition", "export_statement"}
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")
NUM_LINE = re.compile(r"^ *(\d+)\t")
STOP = set("""this that with from have will what where which when then them they their there these those into onto
about after before should would could need needs file files line lines code function method class test tests output
look looking check checking read make sure find found first next also just only still more some here return true false
none null self import export const string number value values type types error errors main print using used like want
know understand implementation implement current existing async await static public private protected default
def func function struct impl trait interface enum module package json yaml toml html text""".split())  # fmt: skip
_ID = r"[A-Za-z_][A-Za-z0-9_]{3,}"
CODE_CONTEXT = re.compile(rf"`({_ID})`|['\"]({_ID})['\"]|({_ID})\(|\.({_ID})\b")
KEYWORDS = {"def", "class", "function", "async", "export", "default", "pub", "fn", "impl", "type", "func", "struct",
            "enum", "trait", "interface", "const", "let", "var", "abstract", "static", "get", "set", "mod", "for"}  # fmt: skip

_Q = r"""(?:'[^']*'|"[^"]*"|[^\s'"|;&<>]+)"""
_RANGE = r"""['"]?(\d+),(\d+)p['"]?"""
_N = r"-(?:n\s*)?(\d+)"
_NUM = r"(?:cat\s+-n|nl\s+-ba)"
READ_FORMS = [  # (regex, numbered, kind)
    (re.compile(rf"{_NUM}\s+({_Q})"), True, "all"),
    (re.compile(rf"{_NUM}\s+({_Q})\s*\|\s*sed\s+-n\s+{_RANGE}"), True, "range"),
    (re.compile(rf"{_NUM}\s+({_Q})\s*\|\s*head\s+{_N}"), True, "head"),
    (re.compile(rf"sed\s+-n\s+{_RANGE}\s+({_Q})"), False, "range_first"),
    (re.compile(rf"head\s+{_N}\s+({_Q})"), False, "head_first"),
    (re.compile(rf"cat\s+({_Q})\s*\|\s*head\s+{_N}"), False, "head"),
    (re.compile(rf"cat((?:\s+{_Q})+)"), False, "multi"),
]


@dataclass
class ReadSegment:
    command: str
    path: str
    first: int
    last: int | None
    numbered: bool


@dataclass(eq=False)
class Symbol:
    start: int
    end: int
    header_end: int
    name: str
    parent: "Symbol | None" = None
    children: list["Symbol"] = field(default_factory=list)


@dataclass(eq=False)
class Region:
    seg: ReadSegment
    lang: str
    start: int
    end: int
    text: list[str]
    src: list[str]
    line_nos: list[int]
    symbols: list[Symbol] = field(default_factory=list)
    skip_rows: int = 0
    expanded: set = field(default_factory=set)
    opened: dict = field(default_factory=dict)
    merged: set = field(default_factory=set)


# ----------------------------------------------------------------------------- commands


def split_command(command: str) -> list[str]:
    """Split on top-level `;`, `&&`, `||` and newlines, respecting quotes."""
    parts, buf, quote, i = [], [], None, 0
    while i < len(command):
        ch = command[i]
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif command[i : i + 2] in ("&&", "||") or ch in ";\n":
            parts.append("".join(buf))
            buf = []
            i += 1 if ch in ";\n" else 2
            continue
        buf.append(ch)
        i += 1
    parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def _unquote(token: str) -> str:
    return token[1:-1] if len(token) > 1 and token[0] == token[-1] and token[0] in "'\"" else token


def read_segments(command: str) -> list[ReadSegment]:
    segments, prefix = [], ""
    for part in split_command(command):
        if m := re.fullmatch(r"cd\s+(" + _Q + ")", part):
            prefix += f"cd {m.group(1)} && "
            continue
        for rx, numbered, kind in READ_FORMS:
            if not (m := rx.fullmatch(part)):
                continue
            g = m.groups()
            if kind == "multi":
                paths = [_unquote(t) for t in re.findall(_Q, g[0])]
                if any(p.startswith("-") for p in paths):
                    break
                segments += [ReadSegment(f"{prefix}cat {t}", _unquote(t), 1, None, False) for t in re.findall(_Q, g[0])]
            else:
                path, first, last = {
                    "all": lambda: (g[0], 1, None),
                    "range": lambda: (g[0], int(g[1]), int(g[2])),
                    "head": lambda: (g[0], 1, int(g[1])),
                    "range_first": lambda: (g[2], int(g[0]), int(g[1])),
                    "head_first": lambda: (g[1], 1, int(g[0])),
                }[kind]()
                segments.append(ReadSegment(prefix + part, _unquote(path), first, last, numbered))
            break
    return [s for s in segments if PurePosixPath(s.path).suffix in LANGS]


# ----------------------------------------------------------------------------- locating code in the output


def _find_numbered(seg: ReadSegment, lines: list[str], pos: int) -> tuple[int, int] | None:
    for i in range(pos, len(lines)):
        if (m := NUM_LINE.match(lines[i])) and int(m.group(1)) == seg.first:
            j = i + 1
            while j < len(lines) and (m2 := NUM_LINE.match(lines[j])) and int(m2.group(1)) == seg.first + j - i:
                if seg.last is not None and seg.first + j - i > seg.last:
                    break
                j += 1
            return i, j
    return None


def _find_raw(seg: ReadSegment, lines: list[str], pos: int, run: Callable[[str], str]) -> tuple[int, int] | None:
    expected = run(seg.command).splitlines(keepends=True)
    if not expected:
        return None
    for i in range(pos, len(lines) - len(expected) + 1):
        if lines[i] == expected[0] and lines[i : i + len(expected)] == expected:
            return i, i + len(expected)
    return None


def locate_regions(segments: list[ReadSegment], lines: list[str], run: Callable[[str], str]) -> list[Region]:
    regions, pos = [], 0
    for seg in segments:
        span = _find_numbered(seg, lines, pos) if seg.numbered else _find_raw(seg, lines, pos, run)
        if span is None:
            continue
        a, b = span
        text = [line if line.endswith("\n") else line + "\n" for line in lines[a:b]]
        if seg.numbered:
            src = [NUM_LINE.sub("", line, count=1).rstrip("\n") for line in lines[a:b]]
            line_nos = [int(NUM_LINE.match(line).group(1)) for line in lines[a:b]]
        else:
            src = [line.rstrip("\n") for line in lines[a:b]]
            line_nos = list(range(seg.first, seg.first + b - a))
        regions.append(Region(seg, LANGS[PurePosixPath(seg.path).suffix], a, b, text, src, line_nos))
        pos = b
    return regions


# ----------------------------------------------------------------------------- symbol trees


def _walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _symbol_info(node):
    if node.type in SYMBOL_TYPES:
        return node.child_by_field_name("name") or node.child_by_field_name("type"), node.child_by_field_name("body")
    if node.type in ("lexical_declaration", "variable_declaration"):
        for d in node.children:
            v = d.child_by_field_name("value") if d.type == "variable_declarator" else None
            if v is not None and v.type in FUNCTION_VALUES:
                return d.child_by_field_name("name"), v.child_by_field_name("body")
    return None


def _collect(node, src: list[str], lang: str, offset: int) -> list[Symbol]:
    out = []
    for c in node.children:
        if (info := _symbol_info(c)) is None:
            out += _collect(c, src, lang, offset)
            continue
        name_node, body = info
        start = (
            c.parent.start_point[0] if c.parent is not None and c.parent.type in START_WRAPPERS else c.start_point[0]
        )
        start, end = start + offset, c.end_point[0] + offset
        header_end = start
        if body is not None:
            body_row = body.start_point[0] + offset
            header_end = body_row - 1 if lang == "python" and body_row > c.start_point[0] + offset else body_row
        header_end = max(start, min(header_end, start + 5, end))
        name = name_node.text.decode(errors="replace") if name_node is not None else ""
        if not name:
            name = next((w for w in IDENT.findall(src[min(start, len(src) - 1)]) if w not in KEYWORDS), "")
        sym = Symbol(start, end, header_end, name)
        sym.children = _collect(c, src, lang, offset) if c.type in CONTAINER_TYPES else []
        for ch in sym.children:
            ch.parent = sym
        out.append(sym)
    return out


def _error_rows(root) -> set[int]:
    rows = set()
    for n in _walk(root):
        if n.type == "ERROR" or n.is_missing:
            rows.update(range(n.start_point[0], n.end_point[0] + 1))
    return rows


def _leading_fragment(src: list[str]) -> int:
    """Rows at the start that close a block opened before the shown range."""
    depth = 0
    for i, line in enumerate(src):
        depth += line.count("{") - line.count("}")
        if depth < 0:
            return i + 1
    return 0


def parse_symbols(region: Region) -> bool:
    """Fill `region.symbols`; return False if the region cannot be folded."""
    from tree_sitter_language_pack import get_parser

    src, lang = region.src, region.lang
    if not src or max(len(line) for line in src) > 2000:
        return False
    parser = get_parser(lang)
    candidates = [(0, "")]
    if lang in BRACE_WRAPPER or lang == "go":
        if k := _leading_fragment(src):
            candidates += [(k, "")] + ([(k, BRACE_WRAPPER[lang])] if lang in BRACE_WRAPPER else [])
    best = None
    for k, wrapper in candidates:
        lines = ([wrapper] if wrapper else []) + src[k:]
        root = parser.parse("\n".join(lines).encode()).root_node
        err = len(_error_rows(root)) / max(len(lines), 1)
        if best is None or err < best[0]:
            best = (err, k, wrapper, root)
    err, k, wrapper, root = best
    if err > 0.3:
        return False
    offset = k - (1 if wrapper else 0)
    symbols = _collect(root, src, lang, offset)
    if wrapper:
        symbols = [c for s in symbols for c in (s.children if s.start < k else [s])]
        for s in symbols:
            s.parent = None
    for s in _iter_symbols(symbols):
        s.end = min(s.end, len(src) - 1)
        s.header_end = min(s.header_end, s.end)
    region.symbols, region.skip_rows = [s for s in symbols if s.start >= k], k
    return bool(region.symbols)


def _iter_symbols(symbols: list[Symbol]):
    for s in symbols:
        yield s
        yield from _iter_symbols(s.children)


# ----------------------------------------------------------------------------- intent


def extract_terms(intent: str) -> set[str]:
    """Identifier-shaped words (snake_case, camelCase, digits), plus plain words used in code context."""
    shaped = {w for w in IDENT.findall(intent) if "_" in w or re.search(r"[a-z][A-Z]|\d", w)}
    in_code = {w for m in CODE_CONTEXT.finditer(intent) for w in m.groups() if w}
    return {w for w in shaped | in_code if len(w) >= 4 and w.lower() not in STOP}


def term_weights(rows: list[str], terms: set[str], max_df: float) -> dict[str, float]:
    df: dict[str, int] = {}
    for row in rows:
        for w in set(IDENT.findall(row)) & terms:
            df[w] = df.get(w, 0) + 1
    n = len(rows)
    return {w: math.log(1 + n / c) for w, c in df.items() if c <= max(3, max_df * n)}


# ----------------------------------------------------------------------------- folding


def _gap_rows(lo: int, hi: int, members: list[Symbol], max_len: int, merged: set) -> set[int]:
    """Rows in [lo, hi] outside members, keeping gap runs of at most max_len rows unless all neighbors are merged."""
    owner = {r: m for m in members for r in range(m.start, m.end + 1)}
    rows, run, prev = set(), [], None
    for r in range(lo, hi + 2):
        if r <= hi and r not in owner:
            run.append(r)
            continue
        nxt = owner.get(r)
        if run and len(run) <= max_len and not ({prev, nxt} - {None} <= merged and (prev or nxt)):
            rows.update(run)
        run, prev = [], nxt if nxt is not None else prev
    return rows


def shown_rows(region: Region, cfg: FoldConfig) -> set[int]:
    rows: set[int] = set()
    top = region.symbols
    chars = 0
    for r in range(region.skip_rows, top[0].start):
        chars += len(region.text[r])
        if chars > cfg.prelude_chars:
            break
        rows.add(r)
    rows |= _gap_rows(top[0].start, len(region.src) - 1, top, cfg.gap_lines, region.merged)

    def visit(s: Symbol):
        if s in region.merged:
            return
        rows.update(range(s.start, s.header_end + 1))
        if s in region.expanded:
            rows.update(_gap_rows(s.header_end + 1, s.end, s.children, cfg.gap_lines, region.merged))
            for c in s.children:
                visit(c)
        if s in region.opened:
            rows.update(region.opened[s])

    for s in top:
        visit(s)
    return rows


def render_region(region: Region, rows: set[int]) -> str:
    out, i, n = [], 0, len(region.text)
    while i < n:
        if i in rows:
            out.append(region.text[i])
            i += 1
            continue
        j = i
        while j < n and j not in rows:
            j += 1
        if j - i == 1:
            out.append(region.text[i])
        else:
            first = next((region.src[r] for r in range(i, j) if region.src[r].strip()), "")
            indent = first[: len(first) - len(first.lstrip())]
            out.append(f"{indent}... [{region.line_nos[i]}-{region.line_nos[j - 1]}]\n")
        i = j
    return "".join(out)


def _has_opened(region: Region, s: Symbol) -> bool:
    return any(x in region.opened for x in _iter_symbols([s]))


def fold_output(
    command: str, output: str, intent: str, cfg: FoldConfig, run: Callable[[str], str]
) -> tuple[str, dict] | None:
    """Return (folded_text, stats), or None to fall back to the default truncation.
    `run` executes a read-only command and returns its output; it is only used to locate unnumbered code."""
    if len(output) < cfg.fold_budget or not (segments := read_segments(command)):
        return None
    lines = output.splitlines(keepends=True)
    regions = locate_regions(segments, lines, run)
    folded = [r for r in regions if sum(map(len, r.text)) > cfg.keep_small_chars and parse_symbols(r)]
    if not folded:
        return None

    titles = {}
    for prev, reg in zip(regions, regions[1:]):
        if (
            not prev.seg.numbered
            and not reg.seg.numbered
            and prev.end == reg.start
            and (prev in folded or reg in folded)
        ):
            titles[reg.start] = f"== {reg.seg.path} ==\n"
    fixed = len(HEADER) + sum(map(len, titles.values()))
    in_folded = {i for r in folded for i in range(r.start, r.end)}
    fixed += sum(len(line) for i, line in enumerate(lines) if i not in in_folded)
    cost = {r: len(render_region(r, shown_rows(r, cfg))) for r in folded}
    if fixed >= cfg.fold_budget:
        return None

    while fixed + sum(cost.values()) >= cfg.fold_budget:
        cands = [r for r in folded if any(s not in r.merged for s in r.symbols)]
        if not cands:
            return None
        reg = max(cands, key=lambda r: cost[r])
        reg.merged.add(next(s for s in reversed(reg.symbols) if s not in reg.merged))
        cost[reg] = len(render_region(reg, shown_rows(reg, cfg)))

    terms = extract_terms(intent)
    weights = term_weights([row for r in folded for row in r.src], terms, cfg.max_term_df)
    leaves = []
    for ri, reg in enumerate(folded):
        row_terms = [set(IDENT.findall(row)) & weights.keys() for row in reg.src]
        for top in reg.symbols:
            if top in reg.merged:
                continue
            for s in _iter_symbols([top]):
                if s.children:
                    continue
                score = sum(weights[w] for w in set().union(*row_terms[s.start : s.end + 1]))
                if s.name in terms:
                    score = 3 * (score or 1)
                if score > 0:
                    hits = [r for r in range(s.start, s.end + 1) if row_terms[r]]
                    leaves.append((-score, ri, s.start, reg, s, hits))
    leaves.sort(key=lambda x: x[:3])

    for _, _, _, reg, s, hits in leaves:
        state = (set(reg.expanded), dict(reg.opened), set(reg.merged))
        path, p = [], s.parent
        while p is not None:
            path.append(p)
            p = p.parent
        new = [p for p in path if p not in reg.expanded]
        reg.expanded.update(path)
        rows = set(range(s.start, s.end + 1))
        if sum(len(reg.text[r]) for r in rows) > cfg.max_open_share * cfg.fold_budget:
            rows = set(range(s.start, s.header_end + 1))
            rows |= {
                j for h in hits for j in range(h - cfg.hit_window, h + cfg.hit_window + 1) if s.start <= j <= s.end
            }
        reg.opened[s] = rows
        others = sum(c for r, c in cost.items() if r is not reg)
        new_cost = len(render_region(reg, shown_rows(reg, cfg)))
        if fixed + others + new_cost >= cfg.fold_budget and new:
            keep = set(path) | {s}
            reg.merged |= {c for p in new for c in p.children if c not in keep and not _has_opened(reg, c)}
            new_cost = len(render_region(reg, shown_rows(reg, cfg)))
        if fixed + others + new_cost >= cfg.fold_budget:
            reg.expanded, reg.opened, reg.merged = state
            continue
        cost[reg] = new_cost

    rendered = {r.start: r for r in folded}
    out, i = [HEADER], 0
    while i < len(lines):
        out.append(titles.get(i, ""))
        if (reg := rendered.get(i)) is not None:
            out.append(render_region(reg, shown_rows(reg, cfg)))
            i = reg.end
        else:
            out.append(lines[i])
            i += 1
    text = "".join(out)
    if len(text) >= cfg.fold_budget:
        return None
    stats = {
        "regions": [
            {
                "path": r.seg.path,
                "lang": r.lang,
                "numbered": r.seg.numbered,
                "n_lines": len(r.src),
                "folded": r in folded,
                "opened": [s.name for s in r.opened],
                "merged": len(r.merged),
            }
            for r in regions
        ],
        "terms": sorted(terms),
        "shown_chars": len(text),
        "orig_chars": len(output),
    }
    return text, stats
