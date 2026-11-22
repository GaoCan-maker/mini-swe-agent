import pytest

from minisweagent.req.bash import analyze
from minisweagent.req.verdict import is_check, passed


@pytest.mark.parametrize(
    ("output", "code", "expected"),
    [
        ("5 passed in 0.1s", 0, True),
        ("ok  \tgithub.com/x/y\t0.01s", 0, True),
        ("0 errors, 3 warnings", 0, True),
        ("1 failed, 4 passed", 1, False),
        ("1 failed, 4 passed", 0, False),
        ("Traceback (most recent call last):\n  ...\nValueError: x", 0, False),
        ("--- FAIL: TestSlice (0.00s)", 0, False),
        ("src/a.ts(3,1): error TS2304: Cannot find name 'x'.", 0, False),
        ("thread 'main' panicked at src/lib.rs:3:5", 0, False),
        ("all good", 2, False),
    ],
)
def test_passed(output, code, expected):
    assert passed(output, code) is expected


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("pytest tests/test_x.py -q", True),
        ("cd /repo && go test ./...", True),
        ("python3 check.py", True),
        ("node -e 'console.log(1)'", True),
        ("cat pkg/core.py", False),
        ("pip install -e .", False),
        ("sed -i 's/a/b/' x.py", False),
        ("python3 - <<'EOF'\nfrom pathlib import Path\nPath('x.py').write_text('y')\nEOF", False),
        ("python3 - <<'EOF'\nimport x\nassert x.f() == 1\nEOF", True),
        ("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", False),
    ],
)
def test_is_check(command, expected):
    assert is_check(analyze(command)) is expected
