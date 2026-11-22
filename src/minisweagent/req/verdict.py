"""Decide whether a command run counts as a passing check, and whether it is a check at all."""

import re

from minisweagent.req.bash import TEST_RE, CommandInfo

_FAILURE_RE = re.compile(
    r"Traceback \(most recent call last\)"
    r"|\bAssertionError\b"
    r"|^FAILED\b|^\s*--- FAIL\b|^FAIL\b"
    r"|\b[1-9]\d* (?:failed|failing|errors?)\b"
    r"|panicked at"
    r"|^error(?:\[E\d+\])?:"
    r"|\berror TS\d+"
    r"|\bSyntaxError\b"
    r"|\bUncaught\b"
    "|^\\s*[\u2715\u00d7\u2717]\\s",
    re.MULTILINE,
)
_INLINE_PY_RE = re.compile(r"\bpython[0-9.]*\s+(?:-\s*<<|-c\b|<<)")
_PY_WRITE_RE = re.compile(
    r"\.write_text\(|\.write_bytes\(|\.write\(|open\([^)]*['\"][wa]b?\+?['\"]|os\.replace\(|shutil\.(?:copy|move)"
)


def passed(output: str, returncode: int) -> bool:
    return returncode == 0 and not _FAILURE_RE.search(output or "")


def is_check(info: CommandInfo) -> bool:
    """A command that runs code (tests, scripts), excluding installs and inline python edit scripts."""
    if not info.is_exec or info.is_install or info.is_submit:
        return False
    if _INLINE_PY_RE.search(info.raw) and _PY_WRITE_RE.search(info.raw) and not TEST_RE.search(info.raw):
        return False
    return True
