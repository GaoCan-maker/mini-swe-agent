"""Recognize file reads and searches in bash commands, wrap them with markers, and split the output again."""

import base64
import binascii
import re
import shlex
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from minisweagent.astshell.config import ASTShellConfig

LANG_BY_EXT = {
    ".py": "python",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".rs": "rust",
}
KEYWORDS = ("sed", "nl", "cat", "head", "tail", "awk", "grep", "rg")
SUBMIT = "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
SAFE_PATH = re.compile(r"^[A-Za-z0-9_./@+][A-Za-z0-9_./@+-]*$")
_OPEN = {"if", "case", "for", "while", "until", "select"}
_CLOSE = {"fi", "esac", "done"}
_SKIP_FIRST = _OPEN | _CLOSE | {"then", "do", "else", "elif", "function", "{", "}", "(", ")", "!"}
_ALLOWED_REDIRECT = re.compile(r"(?<!\S)2>\s*(?:/dev/null|&1)(?!\S)")
_SED_PART = re.compile(r"^\s*(\d+)\s*(?:,\s*(\d+)\s*)?p\s*$")
_AWK = re.compile(r"^\s*NR\s*>=\s*(\d+)\s*&&\s*NR\s*<=\s*(\d+)\s*(?:\{\s*print\s*(?:\$0)?\s*;?\s*\})?\s*$")
_NUM = re.compile(r"^\d+$")


def lang_of(path: str) -> str | None:
    return LANG_BY_EXT.get(PurePosixPath(path).suffix)


@dataclass
class Segment:
    start: int
    end: int
    text: str
    stages: list[str]
    background: bool = False
    pipe_amp: bool = False


@dataclass
class ReadSpec:
    path: str
    lang: str
    numbered: bool
    ops: list[tuple[str, object]]


@dataclass
class SearchSpec:
    tool: str
    head_k: int | None
    single_file: str | None
    has_context: bool
    command: str


@dataclass
class Plan:
    command: str
    specs: dict[int, ReadSpec | SearchSpec] = field(default_factory=dict)
    enlarged: set[int] = field(default_factory=set)


@dataclass
class Block:
    id: int
    kind: str
    pwd: str
    content: bytes | None
    raw: str
    rc: int


# ---------------------------------------------------------------- splitting


def _heredoc_delim(cmd: str, i: int) -> tuple[str, bool, int] | None:
    """Parse the delimiter of a here-doc whose `<<` ends right before position i."""
    strip_tabs = i < len(cmd) and cmd[i] == "-"
    i += strip_tabs
    while i < len(cmd) and cmd[i] in " \t":
        i += 1
    m = re.match(r"""'([^']*)'|"([^"]*)"|\\?([^\s;&|<>()]+)""", cmd[i:])
    if not m:
        return None
    return next(g for g in m.groups() if g is not None), strip_tabs, i + m.end()


def _skip_heredocs(cmd: str, i: int, pending: list[tuple[str, bool]]) -> int | None:
    """i points right after a newline; skip here-doc bodies. Returns the position after the last terminator."""
    for delim, strip_tabs in pending:
        while True:
            if i >= len(cmd):
                return None
            j = cmd.find("\n", i)
            line = cmd[i : j if j >= 0 else len(cmd)]
            i = j + 1 if j >= 0 else len(cmd)
            if (line.lstrip("\t") if strip_tabs else line) == delim:
                break
    return i


def _scan(cmd: str) -> list[tuple[int, int, str]] | None:
    """Return top-level operators as (start, end, op), or None if the command cannot be parsed safely."""
    ops: list[tuple[int, int, str]] = []
    pending: list[tuple[str, bool]] = []
    depth, i, n = 0, 0, len(cmd)
    while i < n:
        c = cmd[i]
        if c == "\\":
            i += 2
        elif c == "'":
            j = cmd.find("'", i + 1)
            if j < 0:
                return None
            i = j + 1
        elif c == '"' or c == "`":
            j = i + 1
            while j < n and cmd[j] != c:
                j += 2 if cmd[j] == "\\" else 1
            if j >= n:
                return None
            i = j + 1
        elif c == "#" and (i == 0 or cmd[i - 1] in " \t\n;&|()"):
            j = cmd.find("\n", i)
            i = n if j < 0 else j
        elif c == "(":
            depth += 1
            i += 1
        elif c == ")":
            depth = max(0, depth - 1)
            i += 1
        elif cmd.startswith("<<", i) and not cmd.startswith("<<<", i):
            parsed = _heredoc_delim(cmd, i + 2)
            if parsed is None:
                return None
            pending.append(parsed[:2])
            i = parsed[2]
        elif c == "\n":
            end = i + 1
            if pending:
                end = _skip_heredocs(cmd, i + 1, pending)
                if end is None:
                    return None
                pending = []
            if depth == 0:
                ops.append((i, end, "\n"))
            i = end
        elif depth == 0 and cmd.startswith(("&&", "||", ";;", "|&"), i):
            ops.append((i, i + 2, {"&&": "&&", "||": "||", ";;": ";", "|&": "|&"}[cmd[i : i + 2]]))
            i += 2
        elif depth == 0 and c in ";|":
            ops.append((i, i + 1, c))
            i += 1
        elif depth == 0 and c == "&" and cmd[i - 1 : i] != ">" and cmd[i + 1 : i + 2] != ">":
            ops.append((i, i + 1, "&"))
            i += 1
        else:
            i += 1
    return None if pending else ops


def split_segments(cmd: str) -> list[Segment] | None:
    ops = _scan(cmd)
    if ops is None:
        return None
    segments, pos, pipes, pipe_amp = [], 0, [], False
    for start, end, op in [*ops, (len(cmd), len(cmd), "")]:
        if op in ("|", "|&"):
            pipes.append((start, end))
            pipe_amp |= op == "|&"
            continue
        bounds = [pos, *[x for p in pipes for x in p], start]
        stages = [cmd[bounds[k] : bounds[k + 1]].strip() for k in range(0, len(bounds), 2)]
        raw = cmd[pos:start]
        lead, trail = len(raw) - len(raw.lstrip()), len(raw) - len(raw.rstrip())
        if raw.strip():
            segments.append(
                Segment(pos + lead, start - trail, raw.strip(), stages, background=op == "&", pipe_amp=pipe_amp)
            )
        pos, pipes, pipe_amp = end, [], False
    return segments


# ---------------------------------------------------------------- recognition


def _unquoted(text: str) -> str:
    """Replace quoted content by spaces so that operators can be searched for."""
    out, i, quote = [], 0, ""
    while i < len(text):
        c = text[i]
        if quote:
            if c == "\\" and quote == '"':
                out.append("  ")
                i += 2
                continue
            if c == quote:
                quote = ""
            out.append(" ")
        elif c in "'\"":
            quote = c
            out.append(" ")
        elif c == "\\":
            out.append("  ")
            i += 2
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _tokens(stage: str, *, allow_glob: bool) -> list[str] | None:
    stage = _ALLOWED_REDIRECT.sub(" ", stage)
    bare = _unquoted(stage)
    if any(c in bare for c in "<>$`") or "$(" in stage or "`" in stage:
        return None
    if not allow_glob and any(c in bare for c in "*?[{~"):
        return None
    try:
        return shlex.split(stage)
    except ValueError:
        return None


def _count(value: str, *, signed: bool = False) -> int | None:
    if signed and value.startswith("+") and _NUM.match(value[1:]):
        return -int(value[1:])
    return int(value) if _NUM.match(value) else None


def _sed_ranges(script: str) -> list[tuple[int, int | None]] | None:
    parts = [p for p in script.split(";") if p.strip()]
    ranges = []
    for part in parts:
        m = _SED_PART.match(part)
        if not m or int(m[1]) == 0:
            return None
        ranges.append((int(m[1]), int(m[2]) if m[2] else None))
    return ranges or None


def _head_tail(tokens: list[str]) -> tuple[tuple[str, int] | None, list[str]]:
    """Parse `head -n K`/`head -K`/`tail -n K`/`tail -n +X`; returns the op and the remaining operands."""
    cmd, args = tokens[0], tokens[1:]
    value = None
    if args and args[0] == "-n" and len(args) >= 2:
        value, args = args[1], args[2:]
    elif args and args[0].startswith("--lines="):
        value, args = args[0][8:], args[1:]
    elif args and args[0].startswith("-n") and len(args[0]) > 2:
        value, args = args[0][2:], args[1:]
    elif args and args[0].startswith("-") and _NUM.match(args[0][1:]):
        value, args = args[0][1:], args[1:]
    if value is None:
        return None, args
    k = _count(value, signed=cmd == "tail")
    if k is None:
        return None, args
    if k < 0:
        return ("tail+", -k), args
    return (cmd, k), args


def _read_source(tokens: list[str]) -> tuple[str, bool, list] | None:
    """A pipeline stage that reads one file: returns (path, numbered, ops)."""
    cmd = tokens[0]
    if cmd == "cat" and len(tokens) == 2:
        return tokens[1], False, []
    if cmd == "cat" and len(tokens) == 3 and tokens[1] == "-n":
        return tokens[2], True, []
    if cmd == "nl" and tokens[1:-1] in (["-ba"], ["-b", "a"]):
        return tokens[-1], True, []
    if cmd == "sed" and len(tokens) == 4 and tokens[1] == "-n" and (ranges := _sed_ranges(tokens[2])):
        return tokens[3], False, [("sed", ranges)]
    if cmd == "awk" and len(tokens) == 3 and (m := _AWK.match(tokens[1])):
        return tokens[2], False, [("awk", (int(m[1]), int(m[2])))]
    if cmd in ("head", "tail"):
        op, rest = _head_tail(tokens)
        if op and len(rest) == 1:
            return rest[0], False, [op]
    return None


def _read_filter(tokens: list[str]) -> tuple | None:
    if tokens[0] == "sed" and len(tokens) == 3 and tokens[1] == "-n" and (ranges := _sed_ranges(tokens[2])):
        return ("sed", ranges)
    if tokens[0] == "awk" and len(tokens) == 2 and (m := _AWK.match(tokens[1])):
        return ("awk", (int(m[1]), int(m[2])))
    if tokens[0] in ("head", "tail"):
        op, rest = _head_tail(tokens)
        if op and not rest:
            return op
    return None


def recognize_read(stages: list[str], cfg: ASTShellConfig) -> ReadSpec | None:
    tokens = [_tokens(s, allow_glob=False) for s in stages]
    if any(not t for t in tokens) or not (source := _read_source(tokens[0])):
        return None
    path, numbered, ops = source
    for t in tokens[1:]:
        if not (op := _read_filter(t)):
            return None
        ops.append(op)
    lang = lang_of(path)
    if not SAFE_PATH.match(path) or lang not in cfg.languages:
        return None
    return ReadSpec(path, lang, numbered, ops)


_SEARCH_ARG_SHORT = {"grep": "ABCefmdD", "rg": "ABCefmgtTjMdE", "git": "ABCefm"}
_SEARCH_BAD_SHORT = {"grep": "clLqZovhzbTu", "rg": "clqovINbr0p", "git": "clLqovhzOpW"}
_SEARCH_ARG_LONG = {
    "include", "exclude", "exclude-dir", "context", "after-context", "before-context", "max-count", "regexp",
    "file", "glob", "iglob", "type", "type-not", "max-depth", "threads", "binary-files", "label", "devices",
    "directories", "encoding", "max-columns", "max-filesize", "type-add",
}  # fmt: skip
_SEARCH_BAD_LONG = {
    "count", "files-with-matches", "files-without-match", "null", "json", "only-matching", "invert-match",
    "no-filename", "byte-offset", "initial-tab", "vimgrep", "heading", "replace", "files", "show-function",
    "function-context", "name-only", "null-data", "pretty", "count-matches", "no-line-number", "column",
    "quiet", "silent", "passthru", "passthrough", "files-without-match",
}  # fmt: skip


def recognize_search(stages: list[str], cfg: ASTShellConfig) -> SearchSpec | None:
    if len(stages) > 2:
        return None
    tokens = _tokens(stages[0], allow_glob=True)
    if not tokens:
        return None
    tool = tokens[0]
    if tool == "git":
        if tokens[1:2] != ["grep"]:
            return None
        tokens = tokens[1:]
    elif tool not in ("grep", "rg"):
        return None
    head_k = None
    if len(stages) == 2:
        head = _tokens(stages[1], allow_glob=False)
        op, rest = _head_tail(head) if head and head[0] == "head" else (None, [])
        if not op or rest:
            return None
        head_k = op[1]
    line_numbers = context = recursive = explicit_pattern = False
    positionals: list[str] = []
    args = iter(tokens[1:])
    for tok in args:
        if tok == "--":
            positionals.extend(args)
            break
        if tok.startswith("--"):
            name, _, value = tok[2:].partition("=")
            if name in _SEARCH_BAD_LONG:
                return None
            line_numbers |= name == "line-number"
            context |= name in ("context", "after-context", "before-context")
            recursive |= name in ("recursive", "dereference-recursive")
            explicit_pattern |= name in ("regexp", "file")
            if name in _SEARCH_ARG_LONG and not value:
                next(args, None)
            continue
        if not tok.startswith("-") or tok == "-":
            positionals.append(tok)
            continue
        for k, ch in enumerate(tok[1:], 1):
            if ch.isdigit():
                context = True
                break
            if ch in _SEARCH_BAD_SHORT[tool]:
                return None
            line_numbers |= ch == "n"
            recursive |= ch in "rR"
            if ch in _SEARCH_ARG_SHORT[tool]:
                context |= ch in "ABC"
                explicit_pattern |= ch in "ef"
                if k == len(tok) - 1:
                    next(args, None)
                break
    if not line_numbers:
        return None
    paths = positionals if explicit_pattern else positionals[1:]
    single = paths[0] if tool != "git" and len(paths) == 1 and SAFE_PATH.match(paths[0]) else None
    if tool == "grep" and recursive:
        single = single if single and lang_of(single) else None
    return SearchSpec(tool, head_k, single, context, stages[0])


# ---------------------------------------------------------------- rewriting

_READ_WRAP = (
    "{{ printf '\\036AST:B {id} R\\n'; printf '\\036AST:P %s\\n' \"$PWD\"; printf '\\036AST:F '; "
    "if [ -f {q} ] && [ \"$(wc -c < {q} | tr -d ' ')\" -le {max} ]; then "
    "base64 -w0 {q} 2>/dev/null || base64 {q} | tr -d '\\n'; fi; printf '\\n\\036AST:O\\n'; "
    "{cmd}; __ast_rc=$?; printf '\\036AST:E {id} %d\\n' \"$__ast_rc\"; (exit $__ast_rc); }}"
)
_SEARCH_WRAP = (
    "{{ printf '\\036AST:B {id} S\\n'; printf '\\036AST:P %s\\n' \"$PWD\"; printf '\\036AST:O\\n'; "
    "{cmd}; __ast_rc=$?; printf '\\036AST:E {id} %d\\n' \"$__ast_rc\"; (exit $__ast_rc); }}"
)


def plan_command(cmd: str, cfg: ASTShellConfig) -> Plan | None:
    """Return the rewritten command with all recognized subcommands, or None if nothing is recognized."""
    if SUBMIT in cmd or len(cmd) > cfg.max_command_chars or not any(k in cmd for k in KEYWORDS):
        return None
    segments = split_segments(cmd)
    if segments is None:
        return None
    plan, level, replacements = Plan(cmd), 0, []
    enlarge = "pipefail" not in cmd
    for seg in segments:
        first = seg.text.split(maxsplit=1)[0]
        level += (first in _OPEN) - (first in _CLOSE and level > 0)
        if level or first in _SKIP_FIRST or seg.background or seg.pipe_amp:
            continue
        idx = len(plan.specs)
        if cfg.smart_grep and (spec := recognize_search(seg.stages, cfg)):
            inner = seg.text
            if spec.head_k is not None and enlarge and spec.head_k < cfg.grep_head_max:
                inner = f"{spec.command} | head -n {cfg.grep_head_max}"
                plan.enlarged.add(idx)
            replacements.append((seg, _SEARCH_WRAP.format(id=idx, cmd=inner)))
        elif (cfg.snap_read or cfg.skeleton) and (spec := recognize_read(seg.stages, cfg)):
            replacements.append(
                (seg, _READ_WRAP.format(id=idx, q=shlex.quote(spec.path), max=cfg.max_file_bytes, cmd=seg.text))
            )
        else:
            continue
        plan.specs[idx] = spec
    if not replacements:
        return None
    for seg, new in reversed(replacements):
        cmd = cmd[: seg.start] + new + cmd[seg.end :]
    plan.command = cmd
    return plan


# ---------------------------------------------------------------- output parsing

_BLOCK = re.compile(
    r"\x1eAST:B (\d+) ([RS])\n\x1eAST:P ([^\n]*)\n(?:\x1eAST:F ([A-Za-z0-9+/=]*)\n)?\x1eAST:O\n(.*?)\x1eAST:E \1 (-?\d+)\n",
    re.S,
)
_STRAY = re.compile(r"\x1eAST:(?:[BPFO][^\n]*\n?|E -?\d+ -?\d+\n?)")


def strip_markers(text: str) -> str:
    return _STRAY.sub("", text)


def decode_b64(b64: str | None) -> bytes | None:
    if not b64:
        return None
    try:
        return base64.b64decode(b64, validate=True)
    except binascii.Error:
        return None


def parse_markers(output: str) -> list[str | Block]:
    pieces: list[str | Block] = []
    pos, seen = 0, set()
    for m in _BLOCK.finditer(output):
        pieces.append(strip_markers(output[pos : m.start()]))
        bid = int(m[1])
        if bid in seen:
            pieces.append(m[5])
        else:
            seen.add(bid)
            pieces.append(Block(bid, m[2], m[3], decode_b64(m[4]), m[5], int(m[6])))
        pos = m.end()
    pieces.append(strip_markers(output[pos:]))
    return pieces


# ---------------------------------------------------------------- line selection


def select_lines(n: int, ops: list[tuple[str, object]]) -> list[int]:
    """Simulate the read pipeline on a file with n lines; returns the printed file line numbers."""
    seq = list(range(1, n + 1))
    for op, arg in ops:
        if op == "sed":
            seq = [
                ln
                for idx, ln in enumerate(seq, 1)
                for x, y in arg
                if (idx == x if y is None or y < x else x <= idx <= y)
            ]
        elif op == "awk":
            seq = seq[arg[0] - 1 : arg[1]]
        elif op == "head":
            seq = seq[:arg]
        elif op == "tail":
            seq = seq[-arg:] if arg else []
        elif op == "tail+":
            seq = seq[max(arg, 1) - 1 :]
    return seq


def head_lines(text: str, k: int) -> str:
    pos = 0
    for _ in range(k):
        j = text.find("\n", pos)
        if j < 0:
            return text
        pos = j + 1
    return text[:pos]
