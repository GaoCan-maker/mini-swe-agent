from pathlib import Path

import pytest
import yaml

from minisweagent.agents import get_agent
from minisweagent.agents.astshell import ASTShellAgent
from minisweagent.astshell import ASTShell, ASTShellConfig
from minisweagent.astshell.render import PLACEHOLDER, verify
from minisweagent.config import get_config_from_spec
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import Submitted
from minisweagent.models.test_models import DeterministicModel, make_output
from minisweagent.utils.serialize import recursive_merge


def _lines(repo: Path, name: str) -> list[str]:
    return (repo / name).read_text().split("\n")


def _numbered(lines: list[str], a: int, b: int) -> str:
    return "".join(f"{i:>6}\t{lines[i - 1]}\n" for i in range(a, b + 1))


def _run(shell: ASTShell, env: LocalEnvironment, command: str) -> dict:
    return shell.run({"command": command}, env.execute)


@pytest.mark.parametrize(
    "command",
    [
        "sed -n '13,28p' pkg/service.py",
        "nl -ba pkg/service.py | sed -n '5,6p'",
        "cat pkg/small.py",
        "grep -rn nothing_matches_this .",
        "echo hello && ls pkg | sort",
        "cat README.md",
    ],
)
def test_untouched_output_is_identical(shell, env, command):
    out, raw = _run(shell, env, command), env.execute({"command": command})
    assert (out["output"], out["returncode"]) == (raw["output"], raw["returncode"])


@pytest.mark.parametrize(
    ("command", "returncode"),
    [
        ("sed -n '1,5p' missing.py", 2),
        ("sed -n '1,2p' pkg/small.py && false", 1),
        ("sed -n '1,2p' missing.py || echo recovered", 0),
        ("grep -n nothing pkg/small.py", 1),
    ],
)
def test_exit_code_and_errors_preserved(shell, env, command, returncode):
    out, raw = _run(shell, env, command), env.execute({"command": command})
    assert out["returncode"] == raw["returncode"] == returncode
    assert out["output"] == raw["output"]


def test_snap_down_keeps_plain_format(shell, env, repo):
    lines = _lines(repo, "pkg/service.py")
    out = _run(shell, env, "sed -n '24,40p' pkg/service.py")
    assert out["output"] == "\n".join(lines[23:46]) + f"\n{PLACEHOLDER}extended to end of Service.prop_1\n"
    assert out["extra"]["ast_shell"]["blocks"][0]["ops"] == ["snap_down"]


def _plain(lines: list[str], a: int, b: int) -> str:
    return "\n".join(lines[a - 1 : b]) + "\n"


@pytest.mark.parametrize(("numbered", "fmt"), [(True, "numbered"), (False, "anchored")])
def test_fold_tail_and_snap_down(shell, env, repo, numbered, fmt):
    lines = _lines(repo, "pkg/service.py")
    out = _run(shell, env, "nl -ba pkg/service.py | sed -n '20,40p'" if numbered else "sed -n '20,40p' pkg/service.py")
    assert out["output"] == (
        f"{PLACEHOLDER}L20-28: end of Service.prop_0 omitted\n"
        + (_numbered if numbered else _plain)(lines, 29, 46)
        + f"{PLACEHOLDER}extended to end of Service.prop_1\n"
    )
    assert out["extra"]["ast_shell"]["blocks"][0]["format"] == fmt


def test_grep_hit_makes_window_snap_up(shell, env, repo):
    lines = _lines(repo, "pkg/service.py")
    _run(shell, env, "grep -n 'def prop_1(' pkg/service.py")
    out = _run(shell, env, "sed -n '40,60p' pkg/service.py")
    assert out["output"] == (
        f"{PLACEHOLDER}L31-39 prepended: header of Service.prop_1\n"
        + _plain(lines, 31, 64)
        + f"{PLACEHOLDER}extended to end of Service.prop_2\n"
    )
    assert verify(out["output"], lines, 40)
    assert out["extra"]["ast_shell"]["blocks"][0]["format"] == "anchored"


@pytest.mark.parametrize(
    ("command", "end", "last"),
    [
        ("sed -n '24,40p;42,45p' pkg/service.py", 46, "Service.prop_1"),
        ("sed -n '24,40p;44,60p' pkg/service.py", 64, "Service.prop_2"),
    ],
)
def test_overlapping_ranges_merge_into_one(shell, env, repo, command, end, last):
    lines = _lines(repo, "pkg/service.py")
    out = _run(shell, env, command)
    assert out["output"] == _plain(lines, 24, end) + f"{PLACEHOLDER}extended to end of {last}\n"
    assert verify(out["output"], lines, 24)


def test_reads_of_same_file_in_one_command_do_not_repeat_lines(shell, env, repo):
    lines = _lines(repo, "pkg/service.py")
    out = _run(shell, env, "sed -n '24,40p' pkg/service.py && sed -n '44,60p' pkg/service.py")
    assert out["output"] == (
        _plain(lines, 24, 43)
        + f"{PLACEHOLDER}L44-46: shown below\n{PLACEHOLDER}extended to end of Service.prop_1\n"
        + _plain(lines, 44, 64)
        + f"{PLACEHOLDER}extended to end of Service.prop_2\n"
    )
    first, second = out["extra"]["ast_shell"]["blocks"]
    assert (first["ops"], second["ops"]) == (["snap_down", "dedup"], ["snap_down"])


def test_snapshot_reflects_writes_in_same_command(shell, env):
    out = _run(shell, env, "printf 'def g():\\n    return 3\\n' > pkg/small.py && nl -ba pkg/small.py | sed -n '2,2p'")
    assert out["output"] == f"{PLACEHOLDER}prepended: header of g\n     1\tdef g():\n     2\t    return 3\n"


def test_unknown_start_uses_numbered_format(shell, env, repo):
    out = _run(shell, env, "sed -n '1,40p' pkg/service.py | tail -n 17")
    assert out["output"] == _numbered(_lines(repo, "pkg/service.py"), 24, 46) + (
        f"{PLACEHOLDER}extended to end of Service.prop_1\n"
    )
    assert out["extra"]["ast_shell"]["blocks"][0]["format"] == "numbered"


def test_skeleton_of_large_file_numbered(shell, env, repo):
    lines = _lines(repo, "pkg/service.py")
    _run(shell, env, "grep -n 'def prop_5(' pkg/service.py")
    out = _run(shell, env, "cd pkg && cat -n service.py")
    text = out["output"]
    assert text.startswith(f"{PLACEHOLDER}outline: bodies folded, line numbers real\n     1\timport os\n")
    assert (
        f'    14\t    def prop_0(self) -> int:\n    15\t        """Docstring 0."""\n{PLACEHOLDER}L16-28: body folded\n'
        in text
    )
    assert f"{PLACEHOLDER}L735-755: body folded\n" in text
    assert _numbered(lines, 103, 118) in text
    assert verify(text, lines) and len(text) < len("\n".join(lines)) / 2
    assert out["extra"]["ast_shell"]["blocks"][0]["ops"] == ["skeleton_l1"]


def test_skeleton_of_large_file_anchored(shell, env, repo):
    lines = _lines(repo, "pkg/service.py")
    _run(shell, env, "grep -n 'def prop_5(' pkg/service.py")
    out = _run(shell, env, "cd pkg && cat service.py")
    text = out["output"]
    assert text.startswith(f"{PLACEHOLDER}outline: bodies folded\nimport os\n")
    assert f'    def prop_0(self) -> int:\n        """Docstring 0."""\n{PLACEHOLDER}L16-28: body folded\n' in text
    assert f"{PLACEHOLDER}L735-755: body folded\n" in text
    assert _plain(lines, 103, 118) in text
    assert ": omitted\n" not in text
    assert verify(text, lines, 1) and not verify(text, lines)
    block = out["extra"]["ast_shell"]["blocks"][0]
    assert (block["ops"], block["format"]) == (["skeleton_l1"], "anchored")
    assert len(text) < len(_run(shell, env, "cd pkg && cat -n service.py")["output"])


@pytest.mark.parametrize("block_max_chars", [3000, 1500])
def test_compact_skeleton_levels_stay_anchored(env, repo, block_max_chars):
    shell = ASTShell(ASTShellConfig(enabled=True, block_max_chars=block_max_chars, budget_guard=False))
    lines, out = _lines(repo, "pkg/service.py"), _run(shell, env, "cat pkg/service.py")
    block = out["extra"]["ast_shell"]["blocks"][0]
    assert block["ops"][0] in ("skeleton_l2", "skeleton_l3") and block["format"] == "anchored"
    assert verify(out["output"], lines, 1)
    numbered = _run(shell, env, "cat -n pkg/service.py")["output"]
    assert len(out["output"]) < len(numbered)


def test_whole_file_below_threshold_is_not_folded(env, repo):
    shell = ASTShell(ASTShellConfig(enabled=True, skeleton_min_chars=10**6, budget_guard=False))
    assert _run(shell, env, "cat pkg/service.py")["output"] == (repo / "pkg" / "service.py").read_text()
    shell = ASTShell(ASTShellConfig(enabled=True, skeleton_min_chars=10**6))
    assert _run(shell, env, "cat pkg/service.py")["extra"]["ast_shell"]["blocks"][0]["ops"] == ["skeleton_l1", "budget"]


def test_budget_degrades_long_range_read(shell, env, repo):
    out = _run(shell, env, "nl -ba pkg/service.py | sed -n '1,600p'")
    block = out["extra"]["ast_shell"]["blocks"][0]
    assert len(out["output"]) <= 9200 < block["orig_chars"]
    assert "budget" in block["ops"] and verify(out["output"], _lines(repo, "pkg/service.py"))


def test_grep_rerank_puts_definitions_first(shell, env):
    out = _run(shell, env, "grep -rn tail_function . | head -5")
    assert out["output"].split("\n") == [
        "./pkg/service.py",
        "  734: [def L734-755] def tail_function(a, b):",
        "./README.md",
        "  1:tail_function docs",
        "./tests/test_service.py",
        "  2: [test in test_0 L1-2]     assert tail_function(0, 1)",
        "  5: [test in test_1 L4-5]     assert tail_function(1, 1)",
        "  8: [test in test_2 L7-8]     assert tail_function(2, 1)",
        f"{PLACEHOLDER}5/32 matches shown, defs first, tests last; omitted: 27 test",
        "",
    ]


def test_grep_annotation_single_file(shell, env):
    assert _run(shell, env, "grep -n 'return' pkg/small.py")["output"] == "2: [in f L1-2]     return 1\n"


def test_grep_head_fallback_matches_original(env):
    shell = ASTShell(ASTShellConfig(enabled=True, grep_rerank=False))
    command = "grep -rn 'assert tail_function' tests | head -3"
    assert _run(shell, env, command)["output"] == "".join(
        f"tests/test_service.py:{3 * i + 2}: [test in test_{i} L{3 * i + 1}-{3 * i + 2}]     assert tail_function({i}, 1)\n"
        for i in range(3)
    )


def test_seen_fold(env, repo):
    shell = ASTShell(ASTShellConfig(enabled=True, seen_fold=True))
    shell.run({"command": "sed -n '31,46p' pkg/service.py"}, env.execute, turn=1)
    out = shell.run({"command": "nl -ba pkg/service.py | sed -n '13,50p'"}, env.execute, turn=4)
    assert f"{PLACEHOLDER}L31-46: Service.prop_1 unchanged, shown 3 turns ago\n" in out["output"]
    assert _numbered(_lines(repo, "pkg/service.py"), 13, 28) in out["output"]


def test_submit_is_never_rewritten(shell, env):
    with pytest.raises(Submitted):
        _run(shell, env, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT && cat pkg/small.py")


def test_disabled_shell_is_passthrough(env):
    shell = ASTShell(ASTShellConfig(enabled=False))
    assert _run(shell, env, "sed -n '24,40p' pkg/service.py") == env.execute(
        {"command": "sed -n '24,40p' pkg/service.py"}
    )


def test_agent_routes_commands_through_ast_shell(repo):
    config = yaml.safe_load(Path("src/minisweagent/config/default.yaml").read_text())["agent"]
    model = DeterministicModel(
        outputs=[
            make_output("read", [{"command": "sed -n '24,40p' pkg/service.py"}]),
            make_output("done", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]),
        ]
    )
    agent = ASTShellAgent(
        model, LocalEnvironment(cwd=str(repo)), **config, mode="yolo", confirm_exit=False, ast_shell={"enabled": True}
    )
    assert agent.run("Fix Service.prop_1")["exit_status"] == "Submitted"
    observation = next(m for m in agent.messages if "extended to end of Service.prop_1" in str(m.get("content")))
    assert observation["extra"]["ast_shell"]["blocks"][0]["ops"] == ["snap_down"]
    assert agent.serialize()["info"]["ast_shell"]["op_snap_down"] == 1


@pytest.mark.parametrize(("spec", "enabled"), [("harbor_ext/astshell.yaml", True), ("harbor_ext/baseline.yaml", False)])
def test_harbor_configs_layer_on_mini(repo, spec, enabled):
    config = recursive_merge(
        *map(get_config_from_spec, ["mini", spec]), {"agent": {"mode": "yolo", "confirm_exit": False}}
    )
    model = DeterministicModel(
        outputs=[
            make_output("read", [{"command": "sed -n '24,40p' pkg/service.py"}]),
            make_output("done", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]),
        ]
    )
    agent = get_agent(model, LocalEnvironment(cwd=str(repo)), config["agent"])
    assert isinstance(agent, ASTShellAgent)
    assert agent.run("Fix Service.prop_1")["exit_status"] == "Submitted"
    assert any("extended to end of Service.prop_1" in str(m.get("content")) for m in agent.messages) == enabled
    assert ("op_snap_down" in agent.serialize()["info"]["ast_shell"]) == enabled
