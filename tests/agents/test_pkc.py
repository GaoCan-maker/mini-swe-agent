import json
import subprocess
import sys

import pytest

from minisweagent.agents.pkc import PKCAgent, commands
from minisweagent.agents.pkc import ctx as pkc_ctx
from minisweagent.agents.pkc.commands import is_green, parse_command
from minisweagent.agents.pkc.fold import history_lines, search_summary
from minisweagent.agents.pkc.keyframe import Candidate, Keyframe, WorkingSet, build_working_set
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


# --- working set and keyframe ---


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


def test_superseded_keyframe_becomes_one_line():
    kf = Keyframe(1, 5, '\n\n<keyframe id="K1">\n## Edits\n' + "e1 a.py +1/-1\n" * 50, WorkingSet("ws " * 100))
    full = len(str(kf))
    assert kf.supersede(2) == full - len(str(kf))
    assert str(kf) == "\n\n[keyframe K1 superseded by K2]"


# --- history index ---


def test_history_lines_split_at_events_and_merge_to_budget():
    ledger = Ledger()
    for step, cmd, out in (
        (1, "cat pkg/a.py", "x = 1\n"),
        (2, "grep -rn foo pkg", "pkg/a.py:1:foo\n"),
        (3, "sed -i 's/x/y/' pkg/a.py", ""),
        (4, "pytest -q", "1 passed in 0.1s"),
        (5, "cat pkg/b.py", "y = 2\n"),
    ):
        rec = ledger.add(step, {"command": cmd}, {"output": out, "returncode": 0}, "")
        if step == 3:
            rec.eid = 1
    events = [(3, "first edit"), (4, "first green test")]
    lines = history_lines(ledger.records, events, 5, 10_000)
    assert lines[0] == "steps 1-3 (until first edit): read pkg/a.py o0; 1 searches; edits e1"
    assert lines[1].startswith("steps 4-4 (until first green test): last test pytest -q PASS 1 (o3)")
    assert lines[2] == "steps 5-5: read pkg/b.py o4"
    merged = history_lines(ledger.records, events, 5, len(lines[2]) + 120)
    assert len(merged) < 3 and merged[0].startswith("steps 1-4 (until first green test)")


def test_search_summary():
    total, hits = search_summary("a.py:1:x\na.py:4:x\nb.py:2:x\n", 2)
    assert total == 3 and hits == "a.py:1,4 (+1)"


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


# --- scheduling ---


def _agent(tmp_path, outputs=(), cwd=None, **pkc):
    return PKCAgent(
        DeterministicModel(outputs=list(outputs)),
        LocalEnvironment(cwd=str(cwd or tmp_path)),
        system_template="s",
        instance_template="{{task}}",
        cost_limit=0,
        pkc=pkc | {"ctx": {"dir": str(tmp_path / "pkc")}},
    )


def _rec(ledger, step, cmd, edit_files=()):
    rec = ledger.add(step, {"command": cmd}, {"output": "", "returncode": 0}, "")
    rec.edit_files = list(edit_files)
    return rec


def test_due_gates_phase_marks_and_defers_edit_bursts(tmp_path):
    agent = _agent(tmp_path, min_steps=1, min_gap=1, max_defer=2)
    edit = [_rec(agent.ledger, 20, "python3 - <<'PY'\nopen('a.py', 'w').write('x')\nPY", ["a.py"])]
    calm = [_rec(agent.ledger, 21, "cat a.py")]
    agent.prompt_tokens = 50_000
    assert agent._due(20, calm) is None
    agent.marks = [("new_target", 18)]
    assert agent._due(20, calm) is None
    agent.prompt_tokens = 70_000
    assert agent._due(20, edit) is None and agent.due_since == 20
    assert agent._due(21, calm) == ("new_target", 70_000)
    assert agent._due(22, edit) is None and agent.due_since == 22
    assert agent._due(23, edit) is None
    assert agent._due(24, edit) == ("new_target", 70_000)
    agent.marks, agent.prompt_tokens = [], 130_000
    assert agent._due(25, calm) == ("watermark", 130_000)
    agent.last_try, agent.config.pkc.min_gap = 23, 5
    assert agent._due(26, calm) is None and agent._due(28, calm) is not None


def test_depth_requirements(tmp_path):
    agent = _agent(tmp_path)
    assert agent._depth_ok("watermark", 130_000, 64_000)
    assert agent._depth_ok("watermark", 80_000, 44_000)
    assert not agent._depth_ok("watermark", 130_000, 70_000)
    assert agent._depth_ok("first_green", 100_000, 79_000)
    assert not agent._depth_ok("first_green", 100_000, 81_000)


def test_recent_hi_keeps_turns_within_budget(tmp_path):
    agent = _agent(tmp_path, keep_turns=1)
    agent.messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "t"}]
    for step, size in ((1, 1000), (2, 100), (3, 100)):
        msg = {"role": "assistant", "content": "", "extra": {}}
        agent.step_msgs[step] = msg
        agent.messages += [msg, {"role": "user", "content": "x" * size}]
    assert agent._recent_hi(300) == 1
    assert agent._recent_hi(0) == 2
    assert agent._recent_hi(10_000) == 0
    agent.config.pkc.keep_turns = 2
    assert agent._recent_hi(0) == 1
    agent.fold_hi = 1
    assert agent._recent_hi(0) == 1


def test_reread_after_fold_becomes_hot_and_enters_working_set(tmp_path):
    repo = tmp_path / "repo"
    _git_repo(repo)
    agent = _agent(tmp_path, cwd=repo)
    agent.files.setup()
    agent.ledger.add(1, {"command": "cat pkg/core.py"}, {"output": ORIGINAL, "returncode": 0}, agent.files.root)
    assert agent._working_set(5, 1).symbols[0].startswith("pkg/core.py::<lines 1-")
    agent.folded_reads = {"pkg/core.py": 5}
    rec = agent.ledger.add(8, {"command": "sed -n '12,14p' pkg/core.py"}, {"output": "", "returncode": 0}, "")
    agent._update_hot(8, [rec])
    assert agent.hot == {"pkg/core.py"} and agent.hot_ranges == {"pkg/core.py": (12, 14)}
    assert agent.stats["rereads_after_fold"] == 1
    ws = agent._working_set(9, 1)
    assert "pkg/core.py::<lines 12-14>" in ws.symbols and "12\tdef subtract(a, b):" in ws.text


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
KEEP: o1
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
# Fold at every phase event, regardless of size, depth and edit bursts.
EAGER = {
    "checkpoint_at": "e1",
    "min_steps": 1,
    "min_gap": 1,
    "min_prompt_tokens": 0,
    "min_fold_fraction": -10.0,
    "high_water": 0,
    "defer_edit_burst": False,
    "keep_turns": 1,
    "recent_budget": 0,
}


def _git_repo(path):
    (path / "pkg").mkdir(parents=True)
    (path / "tests").mkdir()
    (path / "pkg" / "core.py").write_text(ORIGINAL)
    (path / "pkg" / "big.py").write_text(ORIGINAL * 3)
    (path / "tests" / "test_core.py").write_text("def test_add():\n    pass\n")
    for cmd in (["init", "-q"], ["add", "."], ["-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"]):
        subprocess.run(["git", *cmd], cwd=path, check=True)


def _text_outputs(steps):
    return [make_output(f"{t}\n\n```mswea_bash_command\n{c}\n```", [{"command": c}]) for t, c in steps]


def _text_model(steps=STEPS):
    return DeterministicModel(outputs=_text_outputs(steps))


def _toolcall_model(steps=STEPS):
    outputs = []
    for i, (thought, cmd) in enumerate(steps):
        call = {
            "id": f"call_{i}",
            "type": "function",
            "function": {"name": "bash", "arguments": json.dumps({"command": cmd})},
        }
        outputs.append(make_toolcall_output(thought, [call], [{"command": cmd, "tool_call_id": f"call_{i}"}]))
    return DeterministicToolcallModel(outputs=outputs, observation_template=JSON_TEMPLATE)


def _spy(model) -> list[list[dict]]:
    sent, query = [], model.query

    def spy(messages, **kwargs):
        sent.append(list(messages))
        return query(messages, **kwargs)

    model.query = spy
    return sent


def _run(tmp_path, model, **pkc) -> PKCAgent:
    repo = tmp_path / "repo"
    _git_repo(repo)
    agent = PKCAgent(
        model,
        LocalEnvironment(cwd=str(repo)),
        system_template="You are a helpful assistant.",
        instance_template="{{task}}",
        cost_limit=0,
        pkc=pkc | {"ctx": {"dir": str(tmp_path / "pkc")}},
    )
    agent.run("Fix add_numbers.")
    return agent


@pytest.mark.skipif(sys.platform == "win32", reason="ctx is a POSIX sh script")
@pytest.mark.parametrize("make_model", [_text_model, _toolcall_model])
def test_pkc_end_to_end(tmp_path, make_model):
    model = make_model()
    sent = _spy(model)
    agent = _run(tmp_path, model, **EAGER)
    assert agent.messages[-1]["extra"]["exit_status"] == "Submitted"

    assert "ctx show o12" in agent.messages[0]["content"]
    obs = [r.obs_msg for r in agent.ledger.records if r.obs_msg is not None]
    assert len(obs) == 5
    json_obs = make_model is _toolcall_model
    assert ('"id": "o2", "edit_id": "e1"' if json_obs else "#o2 #e1") in obs[2]["content"]
    assert "<checkpoint>" in obs[2]["content"]
    assert "<delta>" in obs[3]["content"] and "Older turns will be removed" in obs[3]["content"]
    assert agent.e1_step == 3 and agent.g1_step == 4

    # Turns 1-4 are kept in the trajectory, untouched, but no longer sent.
    assert [m["extra"].get("pkc_hidden") for m in agent.messages[2:10]] == [5] * 8
    assert all("pkc_hidden" not in m.get("extra", {}) for m in agent.messages[:2] + agent.messages[10:])
    assert "scaled + 11" in obs[0]["content"]
    assert [len(s) for s in sent] == [2, 4, 6, 8, 10, 4]
    last = sent[-1]
    assert [m["role"] for m in last[:3]] == ["system", "user", "assistant"]
    assert last[3] is obs[4] and all("scaled + 11" not in (m.get("content") or "") for m in last)
    if json_obs:
        assert last[2]["tool_calls"][0]["id"] == "call_4" and last[3]["role"] == "tool"

    host = obs[4]["content"]
    assert "[o0 from step 1; NOTE: pkg/core.py changed since then]" in host
    assert '<keyframe id="K1" step="5" reason="first_green" removed_steps="1-4">' in host
    assert "steps 1-3 (until first edit): read pkg/core.py o0; 1 searches; edits e1" in host
    assert "pkg/core.py   add_numbers L7-9" in host and "edited (e1)" in host
    assert "e1 pkg/core.py  +1/-1" in host
    assert "PASS 1 (o3, step 4)" in host
    assert "CP.TARGETS" in host and "CONFIRMED" in host and "KEEP" in host
    assert "--- o1 (step 2) grep -rn" in host and "pkg/core.py:7:def add_numbers(a, b):" in host
    assert agent.keyframes[0].ws.symbols == ["pkg/core.py::add_numbers", "pkg/core.py::subtract"]
    assert "7\tdef add_numbers(a, b):" in host and "9\t    return total\n" in host
    assert host.rstrip().endswith("</keyframe>")

    assert agent.stats["keyframes"] == 1 and agent.stats["phase_folds"] == 1 and agent.stats["hidden_turns"] == 4
    assert agent.stats["restores"] == 1 and agent.stats["hidden_chars"] > 3000
    i_host = next(i for i, m in enumerate(agent.messages) if m is obs[4])
    assert agent.messages[i_host - 1]["extra"]["cache_breakpoint"]
    assert agent.messages[1]["extra"]["cache_breakpoint"]
    info = agent.serialize()["info"]["pkc"]
    assert info["keyframe_steps"] == [5] and info["keyframe_reasons"] == ["first_green"]
    assert info["fold_log"][0]["hi"] == 4 and info["fold_log"][0]["after_est"] < info["fold_log"][0]["before"]


@pytest.mark.skipif(sys.platform == "win32", reason="ctx is a POSIX sh script")
def test_second_fold_after_revert_keeps_a_single_keyframe(tmp_path):
    new_file = "cat > pkg/extra.py <<'EOF'\ndef triple(value):\n    tripled = value * 3\n    return tripled\nEOF"
    steps = STEPS[:5] + [
        ("Undo.", "git checkout -- pkg/core.py"),
        ("Look elsewhere.", "cat pkg/big.py"),
        ("New module.", new_file),
        ("Check.", "ctx list"),
        ("Done.", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"),
    ]
    model = _text_model(steps)
    sent = _spy(model)
    agent = _run(tmp_path, model, **EAGER)
    assert [k.step for k in agent.keyframes] == [5, 7] and agent.kf_reasons == ["first_green", "revert"]
    assert agent.fold_hi == 6
    for messages in sent[5:]:
        assert sum('<keyframe id="' in (m.get("content") or "") for m in messages) == 1
    k2 = str(agent.keyframes[1])
    assert 'removed_steps="1-6"' in k2
    assert "(until first edit)" in k2 and "(until K1)" in k2
    assert "e2 pkg/core.py" in k2 and "CP.TARGETS" in k2
    assert agent.keyframes[0].superseded is None
    assert agent.marks == []
    listed = agent.ledger.records[8].raw
    assert listed.startswith("o0  step 1  read  cat pkg/core.py") and "o5 e2  step 6" in listed


@pytest.mark.skipif(sys.platform == "win32", reason="ctx is a POSIX sh script")
def test_checkpoint_requested_with_first_delta(tmp_path):
    steps = STEPS[:3] + [("Run the tests.", STEPS[3][1])] + STEPS[4:]
    agent = _run(tmp_path, _text_model(steps), **EAGER | {"checkpoint_at": "first_fold"})
    obs = [r.obs_msg for r in agent.ledger.records if r.obs_msg is not None]
    assert agent.e1_step == 3 and "<checkpoint>" not in obs[2]["content"]
    assert "write both blocks" in obs[3]["content"] and "<checkpoint>" in obs[3]["content"]
    assert agent.unanswered["checkpoint"] == 1 and agent.stats["deltas"] == 1
    assert [k.step for k in agent.keyframes] == [5]


@pytest.mark.skipif(sys.platform == "win32", reason="ctx is a POSIX sh script")
def test_watermark_fold_needs_enough_depth(tmp_path):
    steps = [("Read.", "cat pkg/big.py"), ("List.", "ls pkg"), ("List.", "ls tests")]
    steps += [("Done.", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")]
    pkc = {"min_steps": 1, "min_gap": 1, "keep_turns": 1, "recent_budget": 0, "ws_budget": 100}
    agent = _run(tmp_path, _text_model(steps), **pkc | {"high_water": 1000, "low_water": 1000})
    assert agent.keyframes and agent.kf_reasons == ["watermark"] and agent.stats["watermark_folds"] == 1
    assert agent.stats["skipped_shallow"] == 1 and agent.fold_log[0]["hi"] == 2
    obs = [r.obs_msg for r in agent.ledger.records]
    assert "write both blocks" in obs[1]["content"]

    agent = _run(tmp_path / "b", _text_model(steps), **pkc | {"high_water": 1000, "low_water": 0, "ws_budget": 8000})
    assert agent.keyframes == [] and agent.stats["skipped_shallow"] >= 1


@pytest.mark.skipif(sys.platform == "win32", reason="ctx is a POSIX sh script")
def test_ctx_symbol_restore_follows_file_changes(tmp_path):
    repo = tmp_path / "repo"
    _git_repo(repo)
    cmds = [
        "ctx show pkg/core.py::subtract",
        "sed -i '1i # new first line' pkg/core.py && ctx show pkg/core.py::subtract",
        "ctx list",
    ]
    agent = _agent(
        tmp_path,
        [make_output("", [{"command": c}]) for c in cmds]
        + [make_output("", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])],
        cwd=repo,
    )
    agent.run("t")
    outs = [r.raw for r in agent.ledger.records]
    assert outs[0].split("\n")[0].split("\t") == ["    12", "def subtract(a, b):"]
    assert "[ranges recomputed after file change]" in outs[1] and "    13\tdef subtract(a, b):" in outs[1]
    assert outs[2] == "(nothing folded)\n"
