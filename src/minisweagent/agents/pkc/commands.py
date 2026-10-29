"""Shell command classification (ported from the offline trajectory analysis)."""

import re
from dataclasses import dataclass

SRC_EXT = re.compile(
    r"\.(?:py|pyi|go|rs|js|jsx|mjs|cjs|ts|tsx|java|kt|rb|php|c|cc|cpp|h|hpp|cs|swift|scala|ex|exs|lua|zig|vue)$"
)
TEST_PATH = re.compile(r"(^|/)(tests?|spec|__tests__|testdata)(/|$)|_test\.|\.test\.|\.spec\.|(^|/)test_")
SCRATCH = re.compile(r"(^|/)(tmp|repro|reproduce|debug|scratch)|^tmp/|_tmp[_.]|tmp_")
REVERT = re.compile(r"\bgit\s+(?:checkout\s+(?:HEAD\s+)?--\s|restore\s|stash(?:\s|$)|reset\s+--hard)")
FAIL_MARK = re.compile(r"\bFAIL(?:ED|:)?\b|\bfailed\b|Traceback|panic:|\bError\b|error\[|AssertionError")
PASS_MARK = re.compile(r"passed|PASS|ok\s|\d+ tests?")
UNRELIABLE_RC = re.compile(r"\|\s*(?:tail|head|tee)\b")
CODE_EXT = "py|pyi|go|ts|tsx|js|jsx|mjs|cjs|rs|java|rb|php|c|h|cc|cpp|hpp|cs|kt|swift|scala|vue|json|ya?ml|toml|md|txt|cfg|ini|sh|sql|html|css"
PATH_RE = re.compile(rf"[\w./@+-]*[\w-]+\.(?:{CODE_EXT})\b")
CTX_REF = re.compile(r"\bctx\s+show\s+(\S+)")
CTX_USE = re.compile(r"(?:^|[\s;&|(`$])ctx\s+(?:show|list)\b")
PRIORITY = ["test", "build", "run", "read", "search", "git", "edit"]

_RE = {
    "test": re.compile(
        r"\b(pytest|py\.test|go test|cargo (?:test|nextest)|npm (?:run )?test|npx (?:jest|vitest|mocha|playwright)"
        r"|yarn (?:run )?test|pnpm (?:run )?test|bun test|jest\b|vitest|mocha\b|unittest|tox\b|deno test|make test"
        r"|phpunit|rspec|ctest|gradle test|mvn test|runtests\.py)"
    ),
    "build": re.compile(
        r"\b(pip3? install|uv (?:pip|sync|add)|npm (?:install|ci|i)\b|yarn (?:install|add)|pnpm (?:install|add|i)\b"
        r"|go (?:build|vet|mod|get|generate)|cargo (?:build|check|clippy|fmt)|tsc\b|make\b|apt(?:-get)? |poetry install"
        r"|bun install|mvn |gradle |npm run (?:build|lint|typecheck)|eslint|prettier|ruff|mypy|gofmt|black\b)"
    ),
    "edit": re.compile(
        r"\bsed\s+(?:-\w*\s+)*-i|\bperl\s+-\w*i|apply_patch|git apply|\bpatch\s+-|\btee\s+(?!/dev)"
        r"|(?<![0-9&])>>?\s*(?!/dev/null|&)[\w./\"'-]+|write_text|write_bytes|open\([^)]*['\"][wa]\+?['\"]|\.write\("
        r"|cat\s*>|cp\s|mv\s|rm\s|mkdir|touch\s"
    ),
    "run": re.compile(
        r"^(?:python3?|node|go run|ts-node|tsx\b|npx tsx|deno run|bun run|bun \S+\.ts|cargo run|ruby|php"
        r"|bash \S+\.sh|sh \S+\.sh|\./)"
    ),
    "read": re.compile(r"^(?:cat|sed\s+-n|head|tail|nl|less|more|awk|bat|wc|xxd|od|strings)\b"),
    "search": re.compile(r"^(?:grep|rg|egrep|fgrep|ag|ack|find|ls|tree|git grep|fd|locate|du|file|stat|which|type)\b"),
    "git": re.compile(r"^git\b"),
}
_READ_RANGES = [
    (re.compile(r"^nl\s+(?:-\S+\s+)*(\S+)\s*\|\s*sed\s+-n\s+['\"]?(\d+),(\d+)p"), (1, 2, 3)),
    (re.compile(r"^sed\s+-n\s+['\"]?(\d+),(\d+)p['\"]?\s+(\S+)"), (3, 1, 2)),
    (re.compile(r"^(?:cat|nl)\s+(?:-\S+\s+)*(\S+)\s*\|\s*sed\s+-n\s+['\"]?(\d+),(\d+)p"), (1, 2, 3)),
    (re.compile(r"^awk\s+['\"]NR\s*>=?\s*(\d+)\s*&&\s*NR\s*<=?\s*(\d+)['\"]\s+(\S+)"), (3, 1, 2)),
]
_HEAD = [
    re.compile(r"^(?:cat|nl)\s+(?:-\S+\s+)*(?P<f>\S+)\s*\|\s*head\s+-(?:n\s*)?(?P<n>\d+)"),
    re.compile(r"^head\s+-(?:n\s*)?(?P<n>\d+)\s+(?P<f>\S+)"),
]


@dataclass
class Segment:
    text: str
    category: str
    read: tuple[str, int, int | None] | None = None
    """(path, first line, last line); last line is None when the whole file is read."""


def strip_heredocs(cmd: str) -> tuple[str, list[tuple[str, str]]]:
    lines, out, bodies = cmd.split("\n"), [], []
    i = 0
    while i < len(lines):
        out.append(lines[i])
        if m := re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", lines[i]):
            j = i + 1
            while j < len(lines) and lines[j].strip() != m.group(1):
                j += 1
            bodies.append((lines[i], "\n".join(lines[i + 1 : j])))
            i = j
        i += 1
    return "\n".join(out), bodies


def norm_path(p: str, cwd: str | None = None, root: str = "") -> str:
    p = p.strip("'\"")
    if not p.startswith("/") and cwd:
        p = cwd.rstrip("/") + "/" + p
    p = p.replace("/./", "/").removeprefix("./")
    if root and p.startswith(root.rstrip("/") + "/"):
        return p[len(root.rstrip("/")) + 1 :]
    return re.sub(r"^/(?:app|testbed|workspace|repo)/", "", p)


def _clean(seg: str) -> str:
    return re.sub(r"^(?:(?:\w+=\S*|timeout\s+\S+|sudo|time|env|nohup)\s+)+", "", seg.lstrip("(")).strip()


def _read_target(seg: str, cwd: str | None, root: str) -> tuple[str, int, int | None] | None:
    for rx, (f, a, b) in _READ_RANGES:
        if m := rx.match(seg):
            return norm_path(m.group(f), cwd, root), int(m.group(a)), int(m.group(b))
    for rx in _HEAD:
        if m := rx.match(seg):
            return norm_path(m.group("f"), cwd, root), 1, int(m.group("n"))
    if re.match(r"^(?:cat|nl)\b", seg) and "|" not in seg:
        files = [p for p in seg.split()[1:] if not p.startswith("-") and re.search(r"\.\w+$", p)]
        if len(files) == 1:
            return norm_path(files[0], cwd, root), 1, None
    return None


def parse_command(cmd: str, root: str = "") -> list[Segment]:
    head, bodies = strip_heredocs(cmd)
    segments, cwd = [], None
    for raw in filter(None, (s.strip() for s in re.split(r"&&|\|\||;|\n", head))):
        seg = _clean(raw)
        if m := re.match(r"^cd\s+(\S+)", seg):
            cwd = m.group(1).strip("'\"")
            continue
        first = seg.split("|")[0].strip()
        is_edit = bool(_RE["edit"].search(re.sub(r"2>&1|2>\s*/dev/null|>\s*/dev/null", "", seg)))
        if re.match(r"^ctx\s+show\b", first):
            segments.append(Segment(seg, "read"))
        elif _RE["test"].search(seg):
            segments.append(Segment(seg, "test"))
        elif _RE["build"].search(seg):
            segments.append(Segment(seg, "build"))
        elif _RE["run"].match(first) and not is_edit:
            segments.append(Segment(seg, "run"))
        elif _RE["read"].match(first) and not re.search(r"\bsed\s+(?:-\w*\s+)*-i|cat\s*>", seg):
            segments.append(Segment(seg, "read", _read_target(seg, cwd, root)))
        elif _RE["search"].match(first):
            segments.append(Segment(seg, "search"))
        elif _RE["git"].match(first):
            segments.append(Segment(seg, "git"))
        elif is_edit:
            segments.append(Segment(seg, "edit"))
        else:
            segments.append(Segment(seg, "other"))
    for header, body in bodies:
        if re.search(r">\s*\S+|python3?\s+(?:-|<<)|node\s+(?:-|<<)|cat\s*>", header) and re.search(
            r"cat\s*>|>\s*[\w./]+|open\(|write|replace|\.sub\(", header + body
        ):
            segments.append(Segment(header, "edit"))
    return segments


def record_category(segments: list[Segment]) -> str:
    cats = {s.category for s in segments}
    return next((c for c in PRIORITY if c in cats), "other")


def test_key(cmd: str) -> str:
    """Normalized test command, used to decide whether a later run supersedes an earlier one."""
    parts = [s.text for s in parse_command(cmd) if s.category in ("test", "run", "build")] or [cmd]
    key = " && ".join(re.sub(r"\s*\|\s*(?:tail|head|tee)\b.*$|\s*2>&1", "", p) for p in parts)
    return re.sub(r"\s+", " ", key).strip()


def is_green(cmd: str, rc: int | None, output: str) -> bool:
    if rc != 0 and not (UNRELIABLE_RC.search(cmd) and not re.search(r"pipefail|PIPESTATUS", cmd)):
        return False
    return not FAIL_MARK.search(output[-3000:]) and bool(PASS_MARK.search(output))


def test_summary(output: str, rc: int | None) -> str:
    tail = output[-3000:]
    failed = re.findall(r"^FAILED\s+(\S+)|^--- FAIL: (\w+)|^\s*(\w+) \.\.\. (?:FAIL|ERROR)", tail, re.M)
    names = [next(x for x in f if x).split("::")[-1] for f in failed]
    if m := re.search(r"(\d+) failed", tail):
        n_failed = int(m.group(1))
    else:
        n_failed = len(names)
    if n_failed or (rc not in (0, None) and FAIL_MARK.search(tail)):
        shown = f" ({', '.join(names[:3])}{', ...' if len(names) > 3 else ''})" if names else ""
        return f"FAIL {n_failed or '?'}{shown}"
    passed = re.search(r"(\d+) passed", tail)
    return f"PASS {passed.group(1)}" if passed else ("PASS" if rc == 0 else f"rc={rc}")
