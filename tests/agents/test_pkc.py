import json
import subprocess
import sys

import pytest

from minisweagent.agents.pkc import PKCAgent, commands
from minisweagent.agents.pkc import ctx as pkc_ctx
from minisweagent.agents.pkc.commands import is_green, parse_command
from minisweagent.agents.pkc.fold import split_parts
from minisweagent.agents.pkc.keyframe import Candidate, build_working_set
from minisweagent.agents.pkc.ledger import Ledger
from minisweagent.agents.pkc.notes import NoteContext, validate
from minisweagent.agents.pkc.symbols import Symbol, find, index, outline
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import (
    DeterministicModel,
    DeterministicToolcallModel,
    make_output,
    make_toolcall_output,
)
from minisweagent.models.utils.cache_control import cache_breakpoints, set_cache_control

# --- command classification ---


def test_parse_command_read_range_and_cwd():
    segs = parse_command("cd /testbed && nl -ba src/a.py | sed -n '10,20p'")
    assert [(s.category, s.read) for s in segs] == [("read", ("src/a.py", 10, 20))]


def test_parse_command_heredoc_is_edit():
    segs = parse_command("cat > pkg/a.py <<'EOF'\nx = 1\nEOF")
    assert {s.category for s in segs} == {"edit"}


def test_parse_command_ctx_show_is_read():
    assert parse_command("ctx show o3 | head -n 5")[0].category == "read"


def test_is_green():
    assert is_green("pytest -q", 0, "3 passed in 0.1s")
    assert not is_green("pytest -q", 0, "1 failed, 2 passed")
    assert not is_green("pytest -q", 1, "3 passed")
    assert is_green("pytest -q 2>&1 | tail -5", 1, "3 passed")


def test_test_summary_and_key():
    out = "FAILED tests/test_a.py::test_x - assert 0\n1 failed, 2 passed in 0.2s"
    assert commands.test_summary(out, 1) == "FAIL 1 (test_x)"
    assert commands.test_summary("5 passed", 0) == "PASS 5"
    assert commands.test_key("cd /r && pytest tests -q 2>&1 | tail -20") == commands.test_key("pytest tests -q")


# --- symbols ---


def test_index_python_qualnames_and_decorators():
    text = "class A:\n    @staticmethod\n    def f():\n        pass\n\ndef g():\n    return 1\n"
    syms = index("m.py", text)
    assert Symbol("A", 1, 4) in syms
    assert Symbol("A.f", 2, 4) in syms
    assert find(syms, "f") == Symbol("A.f", 2, 4)
    assert find(syms, "G") == Symbol("g", 6, 7)


def test_index_brace_language():
    text = "func A() {\n\treturn\n}\n\nfunc B() {\n}\n"
    assert index("x.go", text) == [Symbol("A", 1, 3), Symbol("B", 5, 6)]


def test_outline_from_numbered_read():
    out = "     10\tdef a():\n     11\t    pass\n     12\tdef b():\n     13\t    pass"
    assert outline(out) == [("a", 10, 11), ("b", 12, 13)]


# --- notes ---


def _note_ctx(records: Ledger, files: dict[str, str]) -> NoteContext:
    return NoteContext(
        exists=lambda p: p in files,
        symbols=lambda p: index(p, files.get(p, "")),
        record=records.get,
        latest_with=records.latest_with,
        resolve=lambda p: p,
    )


def test_validate_checkpoint():
    ledger = Ledger()
    ledger.add(
        1, {"command": "grep -rn multiply pkg"}, {"output": "pkg/a.py:3:def multiply(a, b):", "returncode": 0}, ""
    )
    files = {"pkg/a.py": "def add(a, b):\n    return a + b\n\ndef multiply(a, b):\n    return a * b\n"}
    block = """
TARGETS: pkg/a.py::add and pkg/new.py::helper
EXEMPLARS: pkg/a.py::nothere similar code
RULED_OUT: multiply is unrelated o0
UNVERIFIED: the caller passes ints o0
DONE_WHEN: pytest tests passes
"""
    items, problems = validate("checkpoint", block, _note_ctx(ledger, files), 2000)
    by_field = {i.field: i for i in items}
    assert by_field["TARGETS"].symbols == [("pkg/a.py", "add")]
    assert "EXEMPLARS" not in by_field
    assert by_field["RULED_OUT"].refs == ["o0"]
    assert by_field["UNVERIFIED"].refs == []
    assert any("no such symbol pkg/a.py::nothere" in p for p in problems)
    assert any("citation o0 does not match" in p for p in problems)


def test_validate_drops_code_and_line_numbers():
    items, problems = validate(
        "delta", "TODO: fix it at L12-30\nTODO: x = foo(); y = bar(); z()\n", _note_ctx(Ledger(), {}), 2000
    )
    assert [i.text for i in items] == ["fix it at"]
    assert "contains code" in problems[0]


# --- working set ---


def test_working_set_dedup_nest_and_merge():
    text = "\n".join(
        ["class A:", "    def f(self):", "        return 1", "", "    def g(self):", "        return 2", "", ""]
        + ["", "", "def h():", "    x = 3", "    return x"]
    )
    syms = {s.name: s for s in index("a.py", text)}
    cands = [Candidate(1, "a.py", syms["A.f"]), Candidate(2, "a.py", syms["A"]), Candidate(3, "a.py", syms["h"])]
    visible = [("o4", "def h():\n    x = 3\n    return x\n")]
    ws = build_working_set(cands, {"a.py": text}, visible, budget_chars=10_000, max_lines=150, step=7, sha={})
    assert ws.symbols == ["a.py::A"]
    assert ws.text.count("=== a.py") == 1
    assert "a.py::h   see o4 (current)" in ws.text


def test_working_set_budget_keeps_higher_priority():
    text = "def f():\n    return 1\n"
    cands = [Candidate(2, "b.py", Symbol("f", 1, 2)), Candidate(1, "a.py", Symbol("f", 1, 2))]
    ws = build_working_set(cands, {"a.py": text, "b.py": text}, [], budget_chars=120, max_lines=150, step=1, sha={})
    assert ws.symbols == ["a.py::f"]


# --- fold helpers ---


def test_split_parts_by_echo_separator():
    ledger = Ledger()
    cmd = "echo '=== A'; cat a.py; echo '=== B'; grep -rn foo src"
    out = "=== A\nx = 1\ny = 2\n=== B\nsrc/b.py:3:foo()\n"
    rec = ledger.add(1, {"command": cmd}, {"output": out, "returncode": 0}, "")
    parts = split_parts(rec)
    assert [(p.pid, p.category, p.path) for p in parts] == [("o0.1", "read", "a.py"), ("o0.2", "search", None)]
    assert ledger.restore_text("o0.2") is None
    rec.parts = parts
    assert ledger.restore_text("o0.2") == "=== B\nsrc/b.py:3:foo()\n"


def test_ctx_refs():
    assert pkc_ctx.refs("ctx show o12 | head -5 && ctx show '#e3'") == (["o12", "e3"], False)
    assert pkc_ctx.refs("for i in o1 o2; do ctx show $i; done")[1]
    assert pkc_ctx.refs("ctx show src/a.py::A.f; ctx show src/a.py:10-20") == (
        ["src/a.py::A.f", "src/a.py:10-20"],
        False,
    )


def test_set_cache_control_breakpoints():
    messages = [{"role": "user", "content": f"m{i}"} for i in range(4)]
    messages[1]["extra"] = {"cache_breakpoint": True}
    assert cache_breakpoints(messages) == [1]
    out = set_cache_control(messages, breakpoints=[1])
    assert [isinstance(m["content"], list) for m in out] == [False, True, False, True]


# --- end to end in a temporary git repo ---

ORIGINAL = (
    '"""Arithmetic helpers.\n\nPadding docstring line.\n"""\n\n\n'
    "def add_numbers(a, b):\n    total = a + b\n    return total - 1\n\n\n"
    "def subtract(a, b):\n    result = a - b\n    return result\n\n\n"
    "def multiply(a, b):\n    product = a * b\n    return product\n"
    + "".join(f"\n\ndef helper_{i}(value):\n    scaled = value * {i}\n    return scaled + {i}\n" for i in range(12))
)
EDIT = "cat > pkg/core.py <<'EOF'\n" + ORIGINAL.replace("return total - 1", "return total") + "EOF"
CHECKPOINT = """<checkpoint>
TARGETS: pkg/core.py::add_numbers
EXEMPLARS: pkg/core.py::subtract same shape
RULED_OUT: multiply is unrelated o1
DONE_WHEN: pytest tests/test_core.py passes
</checkpoint>"""
DELTA = """<delta>
DONE: pkg/core.py::add_numbers e1
CONFIRMED: tests in tests/test_core.py pass o3
TODO: submit
</delta>"""
STEPS = [
    ("Read the module.", "cat pkg/core.py"),
    ("Find definitions.", 'grep -rn "def " pkg'),
    ("Fix the off-by-one.", EDIT),
    (CHECKPOINT, 'pytest() { echo "1 passed in 0.01s"; }; pytest tests/test_core.py'),
    (DELTA, "ctx show o0 | head -n 3"),
    ("Done.", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
]
JSON_TEMPLATE = '{"returncode": {{ output.returncode }}, "output": {{ output.output | tojson }}}'


def _git_repo(path):
    (path / "pkg").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "pkg" / "core.py").write_text(ORIGINAL)
    (path / "pkg" / "big.py").write_text(ORIGINAL)
    (path / "tests" / "test_core.py").write_text("def test_add():\n    pass\n")
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=path, check=True)


def _text_model():
    outputs = [
        make_output(f"{thought}\n\n```mswea_bash_command\n{cmd}\n```", [{"command": cmd}]) for thought, cmd in STEPS
    ]
    return DeterministicModel(outputs=outputs)


def _toolcall_model():
    outputs = []
    for i, (thought, cmd) in enumerate(STEPS):
        call = {
            "id": f"call_{i}",
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": cmd})},
        }
        outputs.append(make_toolcall_output(thought, [call], [{"command": cmd, "tool_call_id": f"call_{i}"}]))
    return DeterministicToolcallModel(outputs=outputs, observation_template=JSON_TEMPLATE)


@pytest.mark.skipif(sys.platform == "win32", reason="ctx is a POSIX sh script")
@pytest.mark.parametrize("make_model", [_text_model, _toolcall_model])
def test_pkc_end_to_end(tmp_path, make_model):
    repo = tmp_path / "repo"
    _git_repo(repo)
    pkc = {
        "min_steps": 1,
        "min_gap": 1,
        "min_gain": 10,
        "keep_turns": 1,
        "break_even": False,
        "edit_stub_min_chars": 50,
    }
    agent = PKCAgent(
        make_model(),
        LocalEnvironment(cwd=str(repo)),
        system_template="You are a helpful assistant.",
        instance_template="{{task}}",
        cost_limit=0,
        pkc=pkc | {"ctx": {"dir": str(tmp_path / "pkc")}},
    )
    assert agent.run("Fix add_numbers.")["exit_status"] == "Submitted"

    assert "ctx show o12" in agent.messages[0]["content"]
    obs = [m for m in agent.messages if m["role"] in ("user", "tool") and "extra" in m and "raw_output" in m["extra"]]
    assert len(obs) == 5
    json_obs = make_model is _toolcall_model
    assert ('"id": "o2", "edit_id": "e1"' if json_obs else "#o2 #e1") in obs[2]["content"]
    assert "<checkpoint>" in obs[2]["content"]
    assert "<delta>" in obs[3]["content"]
    assert agent.e1_step == 3 and agent.g1_step == 4

    assert "[o0 folded] cat pkg/core.py" in obs[0]["content"]
    assert "outline: add_numbers" in obs[0]["content"]
    assert "scaled + 11" not in obs[0]["content"]
    assert "scaled + 11" in obs[0]["extra"]["pkc_original_content"]
    assert "[o1 ruled out: CP.RULED_OUT[1]]" in obs[1]["content"]
    assert "1 passed" in obs[3]["content"]

    edit_msg = agent.messages[[i for i, m in enumerate(agent.messages) if m is obs[2]][0] - 1]
    if json_obs:
        assert "[e1 folded]" in json.loads(edit_msg["tool_calls"][0]["function"]["arguments"])["command"]
        assert "pkc_original_tool_calls" in edit_msg["extra"]
    else:
        assert "[e1 folded]" in edit_msg["content"] and "helper_5" not in edit_msg["content"]

    host = obs[4]["content"]
    assert "[o0 from step 1; NOTE: pkg/core.py changed since then]" in host
    assert '<keyframe id="K1" step="5" reason="first_green"' in host
    assert "e1 pkg/core.py  +1/-1" in host
    assert "CONFIRMED" in host and "PASS 1 (o3, step 4)" in host
    assert agent.keyframes[0].ws.symbols == ["pkg/core.py::add_numbers", "pkg/core.py::subtract"]
    assert "7\tdef add_numbers(a, b):" in host and "9\t    return total\n" in host
    assert host.rstrip().endswith("</keyframe>")

    assert agent.stats["keyframes"] == 1 and agent.stats["restores"] == 1 and agent.stats["saved_chars"] > 0
    i_host = next(i for i, m in enumerate(agent.messages) if m is obs[4])
    assert agent.messages[i_host - 1]["extra"]["cache_breakpoint"]
    assert agent.messages[1]["extra"]["cache_breakpoint"]
    assert agent.serialize()["info"]["pkc"]["keyframe_steps"] == [5]


@pytest.mark.skipif(sys.platform == "win32", reason="ctx is a POSIX sh script")
def test_second_keyframe_supersedes_working_set(tmp_path):
    repo = tmp_path / "repo"
    _git_repo(repo)
    new_file = "cat > pkg/extra.py <<'EOF'\ndef triple(value):\n    tripled = value * 3\n    return tripled\nEOF"
    steps = STEPS[:5] + [
        ("Undo.", "git checkout -- pkg/core.py"),
        ("Look elsewhere.", "cat pkg/big.py"),
        ("New module.", new_file),
        ("Check.", "ls pkg"),
        ("Done.", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]
    outputs = [make_output(f"{t}\n\n```mswea_bash_command\n{c}\n```", [{"command": c}]) for t, c in steps]
    pkc = {"min_steps": 1, "min_gap": 1, "min_gain": 10, "keep_turns": 1, "break_even": False}
    agent = PKCAgent(
        DeterministicModel(outputs=outputs),
        LocalEnvironment(cwd=str(repo)),
        system_template="s",
        instance_template="{{task}}",
        cost_limit=0,
        pkc=pkc | {"ctx": {"dir": str(tmp_path / "pkc")}},
    )
    agent.run("t")
    k1_host, k2_host = (h.obs_msg for h in agent.hosts)
    assert (
        "Reverted: pkg/core.py. Code for these files shown in K1 is stale."
        in agent.ledger.records[5].obs_msg["content"]
    )
    assert [k.step for k in agent.keyframes] == [5, 9]
    assert "[K1 working set superseded by K2] pkg/core.py::add_numbers, pkg/core.py::subtract" in k1_host["content"]
    assert "def add_numbers(a, b):" not in k1_host["content"]
    assert "def add_numbers(a, b):" in k1_host["extra"]["pkc_original_content"]
    assert "[o6 folded] cat pkg/big.py" in agent.ledger.records[6].obs_msg["content"]
    assert "pkg/extra.py::triple" not in agent.keyframes[1].ws.symbols
    assert "pkg/extra.py::triple   see e3 (current)" in agent.keyframes[1].ws.text
    assert '<keyframe id="K2"' in k2_host["content"] and "e3 pkg/extra.py  +3/-0 (new file)" in k2_host["content"]
    marked = [i for i, m in enumerate(agent.messages) if m.get("extra", {}).get("cache_breakpoint")]
    hosts = [next(i for i, m in enumerate(agent.messages) if m is h) for h in (k1_host, k2_host)]
    assert marked == [1, hosts[0] - 1, hosts[1] - 1]


@pytest.mark.skipif(sys.platform == "win32", reason="ctx is a POSIX sh script")
def test_ctx_symbol_restore_follows_file_changes(tmp_path):
    repo = tmp_path / "repo"
    _git_repo(repo)
    cmds = [
        "ctx show pkg/core.py::subtract",
        "sed -i '1i # new first line' pkg/core.py && ctx show pkg/core.py::subtract",
        "ctx list",
    ]
    agent = PKCAgent(
        DeterministicModel(
            outputs=[make_output("", [{"command": c}]) for c in cmds]
            + [make_output("", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])]
        ),
        LocalEnvironment(cwd=str(repo)),
        system_template="s",
        instance_template="{{task}}",
        cost_limit=0,
        pkc={"ctx": {"dir": str(tmp_path / "pkc")}},
    )
    agent.run("t")
    outs = [r.raw for r in agent.ledger.records]
    assert outs[0].split("\n")[0].split("\t") == ["    12", "def subtract(a, b):"]
    assert "[ranges recomputed after file change]" in outs[1] and "    13\tdef subtract(a, b):" in outs[1]
    assert outs[2] == "(nothing folded)\n"
