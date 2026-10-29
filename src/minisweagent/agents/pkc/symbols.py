"""Harness-side symbol index: Python `ast` for Python files, a definition-line regex for everything else."""

import ast
import re
from dataclasses import dataclass

DEF_RE = re.compile(
    r"^\s*(?:(?:export|pub(?:\([\w ]+\))?|async|static|public|private|protected|abstract|final|override)\s+)*"
    r"(?:def|class|func|function|fn|type|struct|interface|enum|impl|trait|module|object)\s+"
    r"(?:\([^)]*\)\s*)?([A-Za-z_]\w*)"
)
NUMBERED = re.compile(r"^\s*(\d+)\t(.*)$")


@dataclass(frozen=True)
class Symbol:
    name: str
    start: int
    end: int

    def contains(self, other: "Symbol") -> bool:
        return self.start <= other.start and other.end <= self.end and self != other


def index(path: str, text: str) -> list[Symbol]:
    if path.endswith((".py", ".pyi")):
        try:
            return _python(ast.parse(text))
        except (SyntaxError, ValueError):
            pass
    return _regex(text.splitlines())


def _python(tree: ast.AST, prefix: str = "") -> list[Symbol]:
    out = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            name = prefix + node.name
            start = min([node.lineno, *(d.lineno for d in node.decorator_list)])
            out += [Symbol(name, start, node.end_lineno or node.lineno), *_python(node, name + ".")]
    return out


def _regex(lines: list[str]) -> list[Symbol]:
    return [Symbol(m.group(1), i + 1, _end(lines, i)) for i, line in enumerate(lines) if (m := DEF_RE.match(line))]


def _end(lines: list[str], i: int) -> int:
    depth, opened = 0, False
    for j in range(i, min(len(lines), i + 3000)):
        depth += lines[j].count("{") - lines[j].count("}")
        opened = opened or "{" in lines[j]
        if opened and depth <= 0:
            return j + 1
        if not opened and j > i + 2:
            break
    if opened:
        return min(len(lines), i + 41)
    indent = len(lines[i]) - len(lines[i].lstrip())
    for j in range(i + 1, len(lines)):
        if lines[j].strip() and len(lines[j]) - len(lines[j].lstrip()) <= indent:
            return j
    return len(lines)


def find(symbols: list[Symbol], name: str) -> Symbol | None:
    exact = [s for s in symbols if s.name == name]
    if exact:
        return exact[0]
    last = name.split(".")[-1].lower()
    return next((s for s in symbols if s.name.split(".")[-1].lower() == last), None)


def syntax_ok(path: str, text: str) -> bool:
    if not path.endswith(".py"):
        return True
    try:
        ast.parse(text)
    except (SyntaxError, ValueError):
        return False
    return True


def outline(output: str, start: int = 1, limit: int = 15) -> list[tuple[str, int, int]]:
    """Definitions visible in a read observation, as (name, first line, last line)."""
    rows = []
    for k, line in enumerate(output.splitlines()):
        if m := NUMBERED.match(line):
            rows.append((int(m.group(1)), m.group(2)))
        else:
            rows.append((start + k, line))
    defs = [(n, m.group(1)) for n, line in rows if (m := DEF_RE.match(line))]
    last = rows[-1][0] if rows else start
    return [(name, n, (defs[i + 1][0] - 1) if i + 1 < len(defs) else last) for i, (n, name) in enumerate(defs)][:limit]


def strip_numbers(text: str) -> str:
    return "\n".join(m.group(2) if (m := NUMBERED.match(line)) else line for line in text.splitlines())
