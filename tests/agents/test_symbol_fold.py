import json
from pathlib import Path

import pytest
import yaml

pytest.importorskip("tree_sitter_language_pack")

from minisweagent.agents.fold import FoldAgent
from minisweagent.agents.utils.symbol_fold import (
    HEADER,
    FoldConfig,
    extract_terms,
    fold_output,
    read_segments,
    split_command,
)
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicToolcallModel, make_toolcall_output


def make_file(lang: str, n: int, target: str) -> tuple[str, dict[str, tuple[int, int]]]:
    """Source file with n members; returns (text, {name: (signature_line, last_line)}), 1-based."""
    lines, ranges = [], {}
    names = [target if i == n // 2 else f"member_{i}" for i in range(n)]
    if lang == "py":
        lines += ["import os", "import sys", ""]
        for i, name in enumerate(names):
            start = len(lines) + 1
            lines += [f"def {name}(a, b):", f'    """Compute value {i}."""', f"    x = a * {i} + b", f"    y = x - {i}"]
            lines += ["    for k in range(10):", "        y += k", "    return y", ""]
            ranges[name] = (start, start + 6)
    elif lang == "ts":
        lines += ["export class Store {", "  private items: number[] = [];", ""]
        for i, name in enumerate(names):
            start = len(lines) + 1
            lines += [f"  {name}(a: number): number {{", f"    const x = a * {i};", f"    const y = x - {i};"]
            lines += ["    this.items.push(y);", "    return y + this.items.length;", "  }", ""]
            ranges[name] = (start, start + 5)
        lines += ["}", "", "export const helper = (a: number) => {", "  return a + 1;", "};", ""]
    elif lang == "rs":
        lines += ["pub struct Store {", "    items: Vec<i32>,", "}", "", "impl Store {"]
        for i, name in enumerate(names):
            start = len(lines) + 1
            lines += [
                f"    pub fn {name}(&mut self, a: i32) -> i32 {{",
                f"        let x = a * {i};",
                f"        let y = x - {i};",
            ]
            lines += ["        self.items.push(y);", "        y + self.items.len() as i32", "    }", ""]
            ranges[name] = (start, start + 5)
        lines += ["}", "", "fn helper(a: i32) -> i32 {", "    a + 1", "}", ""]
    elif lang == "go":
        lines += ["package store", "", "type Store struct {", "\titems []int", "}", ""]
        for i, name in enumerate(names):
            start = len(lines) + 1
            lines += [f"func (s *Store) {name}(a int) int {{", f"\tx := a * {i}", f"\ty := x - {i}"]
            lines += ["\ts.items = append(s.items, y)", "\treturn y + len(s.items)", "}", ""]
            ranges[name] = (start, start + 5)
    return "\n".join(lines) + "\n", ranges


def sh(cwd: Path, command: str) -> str:
    return LocalEnvironment(cwd=str(cwd)).execute({"command": command})["output"]


def fold(cwd: Path, command: str, intent: str, **cfg):
    return fold_output(command, sh(cwd, command), intent, FoldConfig(**cfg), lambda c: sh(cwd, c))


def marker(a: int, b: int) -> str:
    return f"... [{a}-{b}]"


def test_split_command_respects_quotes():
    assert split_command("cd src && echo 'a; b' && nl -ba x.py | sed -n '1,5p'\ncat y.py; ls") == [
        "cd src",
        "echo 'a; b'",
        "nl -ba x.py | sed -n '1,5p'",
        "cat y.py",
        "ls",
    ]


def test_read_segments_forms():
    segs = read_segments("cd src && sed -n 10,20p a.py && grep -n x b.py; cat c.ts d.rs README.md && head -n 5 e.go")
    assert [(s.path, s.first, s.numbered) for s in segs] == [
        ("a.py", 10, False),
        ("c.ts", 1, False),
        ("d.rs", 1, False),
        ("e.go", 1, False),
    ]
    assert all(s.command.startswith("cd src && ") for s in segs)
    assert read_segments("nl -ba 'x y.py' | sed -n '3,9p'")[0].path == "x y.py"


@pytest.mark.parametrize(
    ("intent", "terms"),
    [
        ("Let me check how shown_rows handles merged symbols in _gap_rows", {"shown_rows", "_gap_rows"}),
        (
            "The Parser class calls parseArgs and v2_api; see `tokenize` and render()",
            {"parseArgs", "v2_api", "tokenize", "render"},
        ),
        ("Look at class Parser and self.items in config.yaml", {"items"}),
        ("grep -n 'resolve' src && nl -ba src/app/routing.py | sed -n '1,200p'", {"resolve"}),
        ("Understanding the implementation of something important", set()),
    ],
)
def test_extract_terms(intent, terms):
    assert extract_terms(intent) == terms


def test_python_fold_keeps_intent_symbol(tmp_path):
    text, ranges = make_file("py", 80, "target_func")
    (tmp_path / "a.py").write_text(text)
    res = fold(tmp_path, "cat a.py", "Let me inspect target_func to see how it computes y")
    assert res is not None
    out, stats = res
    assert out.startswith(HEADER)
    assert len(out) < 9800 and len(out) < len(text) / 2
    lines = text.splitlines()
    s, e = ranges["target_func"]
    assert "\n".join(lines[s - 1 : e]) in out
    s, e = ranges["member_10"]
    assert lines[s - 1] in out and lines[s] not in out
    assert marker(s + 1, e) in out
    assert stats["regions"][0]["opened"] == ["target_func"]
    assert fold(tmp_path, "cat a.py", "Let me inspect target_func to see how it computes y") == res


def test_no_intent_hit_gives_skeleton_only(tmp_path):
    text, ranges = make_file("py", 80, "target_func")
    (tmp_path / "a.py").write_text(text)
    out, stats = fold(tmp_path, "nl -ba a.py", "nothing relevant here")
    assert stats["regions"][0]["opened"] == []
    assert "y += k" not in out
    s, e = ranges["target_func"]
    assert f"{s:6d}\tdef target_func(a, b):" in out and marker(s + 1, e) in out


def test_mixed_command_keeps_other_output_verbatim(tmp_path):
    py, py_ranges = make_file("py", 80, "target_func")
    ts, ts_ranges = make_file("ts", 60, "member_target")
    (tmp_path / "a.py").write_text(py)
    (tmp_path / "c.ts").write_text(ts)
    (tmp_path / "b.txt").write_text("".join(f"note {i} about member_target\n" for i in range(40)))
    command = "nl -ba a.py | sed -n '1,900p' && grep -n member_target b.txt && cat c.ts"
    output = sh(tmp_path, command)
    calls = []
    res = fold_output(
        command, output, "check member_target", FoldConfig(), lambda c: calls.append(c) or sh(tmp_path, c)
    )
    assert res is not None
    out, stats = res
    assert sh(tmp_path, "grep -n member_target b.txt") in out
    assert calls == ["cat c.ts"]
    assert [r["lang"] for r in stats["regions"]] == ["python", "typescript"]
    assert stats["regions"][1]["opened"] == ["member_target"]
    s, e = ts_ranges["member_5"]
    assert marker(s + 1, e) in out
    s, e = py_ranges["member_3"]
    assert f"{s:6d}\tdef member_3(a, b):" in out and marker(s + 1, e) in out


@pytest.mark.parametrize("sep", [";", " && ", "\n"])
def test_chained_reads_keep_separators(tmp_path, sep):
    (tmp_path / "a.py").write_text(make_file("py", 80, "target_func")[0])
    (tmp_path / "b.rs").write_text(make_file("rs", 60, "target_fn")[0])
    command = sep.join(
        [
            "echo '--- a ---'",
            "nl -ba a.py | sed -n '1,300p'",
            "nl -ba a.py | sed -n '301,700p'",
            "echo '--- b ---'",
            "nl -ba b.rs",
        ]
    )
    out, stats = fold(tmp_path, command, "look at target_func and target_fn")
    assert "--- a ---\n" in out and "--- b ---\n" in out
    assert "== " not in out
    assert [(r["path"], r["folded"]) for r in stats["regions"]] == [("a.py", True), ("a.py", True), ("b.rs", True)]
    assert {n for r in stats["regions"] for n in r["opened"]} == {"target_func", "target_fn"}


@pytest.mark.parametrize(
    ("command", "title"),
    [
        ("cat a.py && cat b.py", True),
        ("cat a.py && echo --- && cat b.py", False),
        ("nl -ba a.py && nl -ba b.py", False),
    ],
)
def test_titles_only_between_adjacent_unnumbered_regions(tmp_path, command, title):
    (tmp_path / "a.py").write_text(make_file("py", 60, "target_func")[0])
    (tmp_path / "b.py").write_text(make_file("py", 60, "other_func")[0])
    out, stats = fold(tmp_path, command, "target_func")
    assert len(stats["regions"]) == 2
    assert ("== b.py ==\n" in out) == title


def test_cat_multiple_files(tmp_path):
    (tmp_path / "a.py").write_text(make_file("py", 60, "target_func")[0])
    (tmp_path / "b.go").write_text(make_file("go", 60, "TargetMethod")[0])
    out, stats = fold(tmp_path, "cat a.py b.go", "TargetMethod")
    assert [(r["path"], r["lang"]) for r in stats["regions"]] == [("a.py", "python"), ("b.go", "go")]
    assert stats["regions"][1]["opened"] == ["TargetMethod"]


def test_unlocated_region_is_kept_verbatim(tmp_path):
    small = "".join(f"value_{i} = {i}\n" for i in range(200))
    (tmp_path / "a.py").write_text(small)
    (tmp_path / "b.py").write_text(make_file("py", 80, "target_func")[0])
    command = "cat a.py && nl -ba b.py"
    out, stats = fold_output(command, sh(tmp_path, command), "target_func", FoldConfig(), lambda c: "changed\n")
    assert small in out
    assert [r["path"] for r in stats["regions"]] == ["b.py"]


def test_container_expanded_one_level(tmp_path):
    lines = ["class Parser:", '    """Parse things."""', ""]
    for i in range(80):
        name = "target_method" if i == 20 else f"method_{i}"
        lines += [f"    def {name}(self, x):", f"        y = x + {i}", f"        z = y * {i}", "        return z", ""]
    (tmp_path / "p.py").write_text("\n".join(lines) + "\n")
    (tmp_path / "q.py").write_text(make_file("py", 60, "unrelated_func")[0])
    out, _ = fold(tmp_path, "cat p.py q.py", "Parser.target_method")
    assert "class Parser:" in out
    assert "    def method_3(self, x):" in out and "        y = x + 3" not in out
    assert "        y = x + 20" in out


def test_container_members_merged_when_budget_is_tight(tmp_path):
    lines = ["class Parser:"]
    for i in range(400):
        name = "target_method" if i == 200 else f"method_{i}"
        lines += [f"    def {name}(self, x):", f"        return x + {i}", ""]
    (tmp_path / "p.py").write_text("\n".join(lines) + "\n")
    out, stats = fold(tmp_path, "cat p.py", "target_method")
    assert stats["regions"][0]["opened"] == ["target_method"]
    assert "        return x + 200" in out
    assert out.count("    def ") == 1
    assert marker(2, 600) in out and marker(605, 1200) in out


@pytest.mark.parametrize(
    ("lang", "target"), [("py", "target_method"), ("ts", "target_method"), ("rs", "target_method")]
)
def test_partial_read_starting_inside_class(tmp_path, lang, target):
    text, ranges = make_file(lang, 120, target)
    (tmp_path / f"f.{lang}").write_text(text)
    first = ranges["member_3"][0] + 2
    out, stats = fold(tmp_path, f"sed -n '{first},5000p' f.{lang}", f"inspect {target}")
    assert stats["regions"][0]["opened"] == [target]
    assert "__Fold__" not in out
    s, e = ranges["member_10"]
    assert marker(s + 1, e) in out
    s, e = ranges[target]
    assert "\n".join(text.splitlines()[s - 1 : e]) in out


@pytest.mark.parametrize(("lang", "target"), [("ts", "member_target"), ("rs", "target_fn"), ("go", "TargetMethod")])
def test_languages(tmp_path, lang, target):
    text, ranges = make_file(lang, 100, target)
    (tmp_path / f"f.{lang}").write_text(text)
    out, stats = fold(tmp_path, f"nl -ba f.{lang}", f"where is {target} defined")
    assert stats["regions"][0]["opened"] == [target]
    s, e = ranges["member_7"]
    assert marker(s + 1, e) in out
    s, e = ranges[target]
    assert all(f"{i:6d}\t" in out for i in range(s, e + 1))


def test_nested_function_is_part_of_its_parent(tmp_path):
    outer = ["def outer_target(rows):", "    seen = []", "", "    def visit(r):", "        seen.append(r)", ""]
    outer += ["    for r in rows:", "        visit(r)", "    return seen", ""]
    (tmp_path / "n.py").write_text("\n".join(outer) + "\n" + make_file("py", 80, "other_func")[0])
    out, stats = fold(tmp_path, "cat n.py", "outer_target")
    assert stats["regions"][0]["opened"] == ["outer_target"]
    assert "\n".join(outer[:9]) in out


def test_large_symbol_shown_around_hits(tmp_path):
    body = [f"    step_{i} = compute({i})" for i in range(1500)]
    body[700] = "    result = special_marker_call(step_699)"
    text = "def huge(a):\n" + "\n".join(body) + "\n    return result\n\n" + make_file("py", 20, "small_func")[0]
    (tmp_path / "h.py").write_text(text)
    out, stats = fold(tmp_path, "cat h.py", "special_marker_call")
    assert stats["regions"][0]["opened"] == ["huge"]
    assert "def huge(a):" in out and "special_marker_call(step_699)" in out
    assert "step_690 = compute(690)" in out and "step_300 = compute(300)" not in out
    assert marker(2, 691) in out


@pytest.mark.parametrize(
    ("command", "files"),
    [
        ("cat a.py", {"a.py": "x = 1\n"}),
        ("grep -rn target_func .", {"a.py": make_file("py", 200, "target_func")[0]}),
        ("cat a.txt", {"a.txt": make_file("py", 200, "target_func")[0]}),
        ("nl -ba a.py && python3 -c \"print('x' * 20000)\"", {"a.py": make_file("py", 200, "target_func")[0]}),
        ("cat a.py", {"a.py": "data = [" + "1, " * 5000 + "]\n"}),
    ],
)
def test_fallbacks(tmp_path, command, files):
    for name, content in files.items():
        (tmp_path / name).write_text(content)
    assert fold(tmp_path, command, "target_func") is None


def test_fold_agent_end_to_end(tmp_path):
    (tmp_path / "a.py").write_text(make_file("py", 80, "target_func")[0])
    (tmp_path / "b.txt").write_text("target_func is called from main\n")
    command = "cat a.py && grep -n target_func b.txt"
    calls = [
        ("Let me read target_func", command),
        ("done", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]
    outputs = [
        make_toolcall_output(
            content,
            [
                {
                    "id": f"c{i}",
                    "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": cmd})},
                }
            ],
            [{"command": cmd, "tool_call_id": f"c{i}"}],
        )
        for i, (content, cmd) in enumerate(calls)
    ]
    config = yaml.safe_load(Path("src/minisweagent/config/mini.yaml").read_text())
    agent = FoldAgent(
        DeterministicToolcallModel(outputs=outputs, observation_template=config["model"]["observation_template"]),
        LocalEnvironment(cwd=str(tmp_path)),
        **config["agent"],
    )
    assert agent.run("task")["exit_status"] == "Submitted"
    obs = next(m for m in agent.messages if m.get("role") == "tool")
    assert "output_head" not in obs["content"]
    assert "[folded code;" in json.loads(obs["content"])["output"]
    assert "1:target_func is called from main" in json.loads(obs["content"])["output"]
    assert obs["extra"]["fold"]["full_output"] == sh(tmp_path, command)
    assert obs["extra"]["fold"]["regions"][0]["opened"] == ["target_func"]
