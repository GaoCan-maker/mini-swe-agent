"""Parse stack frames and test verdicts out of execution output."""

import re

_FRAME_RES = [
    re.compile(r'File "([^"]+)", line (\d+)'),
    re.compile(r"(?:^|[\s(\[])((?:/|\./|\.\./)?[\w@.+\-/]+\.(?:go|rs|[cm]?[jt]sx?|py)):(\d+)(?::\d+)?", re.M),
]
_SKIP_FRAME = re.compile(
    r"(site-packages|dist-packages|node_modules|/usr/lib/|/usr/local/go/|\.cargo/registry|<frozen)"
)
_COUNT_RE = re.compile(r"(\d+)\s+(passed|failed|errors?|skipped)\b")
_CARGO_RE = re.compile(r"test result: (ok|FAILED)\. (\d+) passed; (\d+) failed")
_JEST_RE = re.compile(r"Tests:\s+(?:(\d+) failed,\s*)?(?:\d+ skipped,\s*)?(?:(\d+) passed,\s*)?\d+ total")
_FAILED_TEST_RES = [
    re.compile(r"^FAILED\s+(\S+)", re.M),
    re.compile(r"^\s*--- FAIL: (\S+)", re.M),
    re.compile(r"^test (\S+) \.\.\. FAILED", re.M),
    re.compile(r"^\s*(?:\u2715|\u00d7|x)\s+(.+?)(?:\s+\(\d+\s*m?s\))?$", re.M),
]


def parse_frames(text: str, limit: int = 200) -> list[tuple[str, int]]:
    seen, out = set(), []
    for regex in _FRAME_RES:
        for match in regex.finditer(text):
            path, line = match.group(1), int(match.group(2))
            if _SKIP_FRAME.search(path) or (path, line) in seen:
                continue
            seen.add((path, line))
            out.append((path, line))
            if len(out) >= limit:
                return out
    return out


def parse_verdict(text: str, returncode: int | None) -> dict:
    tail = "\n".join(text.splitlines()[-60:])
    passed = failed = None
    if match := _CARGO_RE.search(tail):
        passed, failed = int(match.group(2)), int(match.group(3))
    elif match := _JEST_RE.search(tail):
        failed, passed = int(match.group(1) or 0), int(match.group(2) or 0)
    else:
        counts: dict[str, int] = {}
        for match in _COUNT_RE.finditer(tail):
            key = "failed" if match.group(2).startswith("error") else match.group(2)
            counts[key] = counts.get(key, 0) + int(match.group(1))
        if counts:
            passed, failed = counts.get("passed", 0), counts.get("failed", 0)
    failed_tests: list[str] = []
    for regex in _FAILED_TEST_RES:
        for match in regex.finditer(text):
            name = match.group(1).strip()
            if name and name not in failed_tests:
                failed_tests.append(name[:120])
            if len(failed_tests) >= 10:
                break
    ok = returncode == 0 and not failed
    return {"ok": ok, "passed": passed, "failed": failed, "failed_tests": failed_tests, "returncode": returncode}


def verdict_summary(verdict: dict | None) -> str:
    if not verdict:
        return "ran"
    parts = []
    if verdict.get("passed") is not None:
        parts.append(f"{verdict['passed']} passed")
    if verdict.get("failed"):
        parts.append(f"{verdict['failed']} failed")
    if not parts:
        parts.append("ok" if verdict.get("ok") else f"exit {verdict.get('returncode')}")
    return ", ".join(parts)
