import pytest

from minisweagent.astshell.command import (
    ReadSpec,
    SearchSpec,
    head_lines,
    parse_markers,
    plan_command,
    recognize_read,
    recognize_search,
    select_lines,
    split_segments,
)
from minisweagent.astshell.config import ASTShellConfig

CFG = ASTShellConfig(enabled=True)


@pytest.mark.parametrize(
    ("cmd", "texts"),
    [
        ("cd /app && sed -n '1,5p' a.py", ["cd /app", "sed -n '1,5p' a.py"]),
        ("echo 'a && b'; cat x.py || true", ["echo 'a && b'", "cat x.py", "true"]),
        ("echo $(cd x && ls) && nl -ba f.py | sed -n '2,3p'", ["echo $(cd x && ls)", "nl -ba f.py | sed -n '2,3p'"]),
        ("cat > f.py <<'EOF'\na && b; c\nEOF\nsed -n '1p' f.py", ["cat > f.py <<'EOF'", "sed -n '1p' f.py"]),
        ("grep -n x a.py # don't split; here\ncat b.py", ["grep -n x a.py # don't split; here", "cat b.py"]),
        ("python -c \"print('a;b')\" 2>&1 | tail -n 5", ["python -c \"print('a;b')\" 2>&1 | tail -n 5"]),
    ],
)
def test_split_segments(cmd, texts):
    segments = split_segments(cmd)
    assert [s.text for s in segments] == texts
    assert all(cmd[s.start : s.end] == s.text for s in segments)


@pytest.mark.parametrize("cmd", ["echo 'unterminated", "cat <<EOF\nno end"])
def test_split_segments_unparseable(cmd):
    assert split_segments(cmd) is None


@pytest.mark.parametrize(
    ("stages", "expected"),
    [
        (["sed -n '10,20p' a.py"], ReadSpec("a.py", "python", False, [("sed", [(10, 20)])])),
        (["sed -n '1,5p;30,40p' a.go"], ReadSpec("a.go", "go", False, [("sed", [(1, 5), (30, 40)])])),
        (["nl -ba src/x.ts", "sed -n '5,9p'"], ReadSpec("src/x.ts", "typescript", True, [("sed", [(5, 9)])])),
        (["cat -n a.rs", "head -n 30"], ReadSpec("a.rs", "rust", True, [("head", 30)])),
        (["cat a.py"], ReadSpec("a.py", "python", False, [])),
        (["head -n 200 a.py"], ReadSpec("a.py", "python", False, [("head", 200)])),
        (["tail -n +40 a.py", "head -n 20"], ReadSpec("a.py", "python", False, [("tail+", 40), ("head", 20)])),
        (["awk 'NR>=3 && NR<=9' a.py"], ReadSpec("a.py", "python", False, [("awk", (3, 9))])),
        (["sed -n '1,5p' a.py 2>/dev/null"], ReadSpec("a.py", "python", False, [("sed", [(1, 5)])])),
        (["sed -n '1,5p' README.md"], None),
        (["sed -n '1,5p' $FILE"], None),
        (["sed -n '/def/p' a.py"], None),
        (["sed -n '1,5p' a.py > out.txt"], None),
        (["cat a.py b.py"], None),
        (["nl -ba a.py", "sed -n '1,5p'", "grep x"], None),
        (["sed -n '1,5p' *.py"], None),
    ],
)
def test_recognize_read(stages, expected):
    assert recognize_read(stages, CFG) == expected


@pytest.mark.parametrize(
    ("stages", "expected"),
    [
        (["grep -rn foo src"], SearchSpec("grep", None, None, False, "grep -rn foo src")),
        (["grep -n 'def x' a.py", "head -20"], SearchSpec("grep", 20, "a.py", False, "grep -n 'def x' a.py")),
        (["rg -n foo --glob '*.py'", "head -n 50"], SearchSpec("rg", 50, None, False, "rg -n foo --glob '*.py'")),
        (["git grep -n foo"], SearchSpec("git", None, None, False, "git grep -n foo")),
        (["grep -rn -C 3 foo ."], SearchSpec("grep", None, None, True, "grep -rn -C 3 foo .")),
        (["grep -rn foo --include=*.py ."], SearchSpec("grep", None, None, False, "grep -rn foo --include=*.py .")),
        (["grep -rl foo ."], None),
        (["grep -rc foo ."], None),
        (["grep -r foo ."], None),
        (["rg -n foo", "wc -l"], None),
        (["grep -rn foo .", "sort"], None),
    ],
)
def test_recognize_search(stages, expected):
    assert recognize_search(stages, CFG) == expected


def test_plan_command_wraps_only_recognized_segments():
    cmd = "cd /app && sed -n '1,5p' a.py && echo done; grep -rn foo . | head -5"
    plan = plan_command(cmd, CFG)
    assert set(plan.specs) == {0, 1} and plan.enlarged == {1}
    assert plan.command.startswith("cd /app && { printf '\\036AST:B 0 R\\n'")
    assert "&& echo done; {" in plan.command
    assert "grep -rn foo . | head -n 500;" in plan.command


@pytest.mark.parametrize(
    "cmd",
    [
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat a.py",
        "python -m pytest -q",
        "for f in a.py b.py; do\n  cat a.py\ndone",
        "cat a.py &",
    ],
)
def test_plan_command_passthrough(cmd):
    assert plan_command(cmd, CFG) is None


def test_plan_command_keeps_head_under_pipefail():
    plan = plan_command("set -o pipefail; grep -rn foo . | head -5", CFG)
    assert plan.enlarged == set() and "| head -5;" in plan.command


@pytest.mark.parametrize(
    ("n", "ops", "expected"),
    [
        (10, [("sed", [(3, 5)])], [3, 4, 5]),
        (10, [("sed", [(8, 20), (1, 2)])], [1, 2, 8, 9, 10]),
        (10, [("sed", [(5, 2)])], [5]),
        (10, [("head", 3)], [1, 2, 3]),
        (10, [("tail", 2)], [9, 10]),
        (10, [("tail+", 9)], [9, 10]),
        (10, [("tail+", 4), ("head", 2)], [4, 5]),
        (10, [("sed", [(2, 9)]), ("tail", 3)], [7, 8, 9]),
        (10, [("awk", (4, 6))], [4, 5, 6]),
    ],
)
def test_select_lines(n, ops, expected):
    assert select_lines(n, ops) == expected


def test_parse_markers_roundtrip():
    output = (
        "before\n\x1eAST:B 0 R\n\x1eAST:P /app\n\x1eAST:F aGkK\n\x1eAST:O\nhi\n\x1eAST:E 0 0\n"
        "middle\n\x1eAST:B 1 S\n\x1eAST:P /app\n\x1eAST:O\nno-newline\x1eAST:E 1 1\nafter"
    )
    pieces = parse_markers(output)
    assert pieces[0] == "before\n" and pieces[2] == "middle\n" and pieces[4] == "after"
    assert (pieces[1].content, pieces[1].raw, pieces[1].rc, pieces[1].pwd) == (b"hi\n", "hi\n", 0, "/app")
    assert (pieces[3].kind, pieces[3].content, pieces[3].raw, pieces[3].rc) == ("S", None, "no-newline", 1)


def test_parse_markers_strips_incomplete_blocks():
    assert parse_markers("x\n\x1eAST:B 0 R\n\x1eAST:P /a\n\x1eAST:O\npartial") == ["x\npartial"]


def test_head_lines():
    assert head_lines("a\nb\nc\n", 2) == "a\nb\n"
    assert head_lines("a\nb", 5) == "a\nb"
