import pytest

from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import Submitted
from minisweagent.synapse import SynapseConfig, SynapseEngine
from minisweagent.synapse.state import EXEC_STALE, LEX_STALE, OPEN, SUPERSEDED


class Runner:
    def __init__(self, env: LocalEnvironment, **config):
        self.env = env
        self.engine = SynapseEngine(SynapseConfig(**config))
        self.step = 0

    def __call__(self, command: str):
        self.step += 1
        output, note = self.engine.run_action({"command": command}, self.env.execute, step=self.step)
        return output, (note.text if note else "")


def test_disabled_without_git(tmp_path):
    runner = Runner(LocalEnvironment(cwd=str(tmp_path)))
    output, note = runner("echo hi")
    assert output["output"].strip() == "hi"
    assert note == ""
    assert runner.engine.disabled == "no_git"


def test_probe_output_never_submits(env):
    runner = Runner(env)
    runner("cat pkg/core.py")
    assert runner.engine.stats["probe_calls"] > 0
    with pytest.raises(Submitted):
        runner("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")


def test_edit_echo_shift_and_line_guard(env, repo):
    runner = Runner(env)
    runner("cat -n pkg/core.py")
    _, note = runner("sed -i '1i import os' pkg/core.py")
    assert "[edit] pkg/core.py: +1 -0 lines" in note
    assert "import os" in note
    assert "[shifted]" in note

    output, note = runner("sed -i '6s/x \\* 2/x * 3/' pkg/core.py")
    assert output["returncode"] == 1
    assert "Not executed" in output["output"]
    assert "L6 -> L7" in output["output"]
    assert "x * 2" in (repo / "pkg" / "core.py").read_text()

    output, note = runner("sed -i '6s/x \\* 2/x * 3/' pkg/core.py")
    assert output["returncode"] == 0
    assert "[no change]" in note

    _, note = runner("sed -i '7s/x \\* 2/x * 3/' pkg/core.py")
    assert "return x * 3" in note
    assert runner.engine.stats["line_guard_blocks"] == 1


@pytest.mark.parametrize("echo", [True, False])
def test_external_change_marks_view_outdated(env, echo):
    runner = Runner(env, edit_echo=echo)
    runner("cat -n pkg/big.py")
    script = (
        "import pathlib; p = pathlib.Path('pkg/big.py'); p.write_text(p.read_text().replace('v10 = 10', 'v10 = 11'))"
    )
    _, note = runner(f'python3 -c "{script}"')
    view = next(e for e in runner.engine.evidence if e.command == "cat -n pkg/big.py")
    assert view.state == LEX_STALE
    if echo:
        assert "10  v10 = 11" in note
        assert "[outdated]" not in note
    else:
        assert "[outdated] earlier output of `cat -n pkg/big.py` (pkg/big.py L1-60)" in note


def test_delta_read(env):
    runner = Runner(env)
    first, _ = runner("cat pkg/big.py")
    assert "v60 = 60" in first["output"]
    second, _ = runner("cat pkg/big.py")
    assert "unchanged since your earlier output" in second["output"]
    assert second["extra"]["synapse_original_output"] == first["output"]
    third, _ = runner("cat pkg/big.py")
    assert "v60 = 60" in third["output"]
    runner("sed -i 's/v5 = 5/v5 = 50/' pkg/big.py")
    fourth, _ = runner("cat pkg/big.py")
    assert "v5 = 50" in fourth["output"]


def test_search_replay(env):
    runner = Runner(env)
    runner("grep -rn compute pkg")
    _, note = runner("printf 'x = compute(3, 4)\\n' >> pkg/use.py")
    assert "`grep -rn compute pkg` would now return different results" in note
    _, note = runner("printf 'y = 1\\n' >> pkg/big.py")
    assert "would now return different results" not in note


def test_contract_change_obligations_and_gate(env, tree_sitter):
    runner = Runner(env)
    runner("cat -n pkg/use.py")
    _, note = runner("sed -i 's/def compute(a, b):/def compute(a, b, c):/' pkg/core.py")
    assert "compute changed its signature (breaking)" in note
    assert "- def compute(a, b):" in note and "+ def compute(a, b, c):" in note
    assert "pkg/use.py:5" in note
    obligations = [o for o in runner.engine.obligations if o.state == OPEN]
    assert [(o.file, o.line) for o in obligations] == [("pkg/use.py", 5)]
    assert runner.engine.footer(step=runner.step + 2).count("pkg/use.py:5") == 1

    output, _ = runner("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
    assert "Submission held back" in output["output"]
    assert "pkg/use.py:5 uses compute" in output["output"]
    assert "no test or reproduction was run" in output["output"]
    with pytest.raises(Submitted):
        runner("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")


def test_fixing_reference_resolves_obligation(env, tree_sitter):
    runner = Runner(env)
    runner("sed -i 's/def compute(a, b):/def compute(a, b, c):/' pkg/core.py")
    _, note = runner("sed -i 's/compute(1, 2)/compute(1, 2, 3)/' pkg/use.py")
    assert all(o.state != OPEN for o in runner.engine.obligations)
    runner("python3 -c 'import pkg.use'")
    with pytest.raises(Submitted):
        runner("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")


def test_body_change_has_no_obligations(env, tree_sitter):
    runner = Runner(env)
    _, note = runner("sed -i 's/return a + b/return b + a/' pkg/core.py")
    assert "compute: body changed" in note
    assert "[contract]" not in note
    assert not runner.engine.obligations


def test_exec_evidence_goes_stale(env):
    runner = Runner(env)
    runner("python3 -c 'import pkg.core; print(pkg.core.compute(1, 2))'")
    _, note = runner("sed -i 's/return a + b/return a - b/' pkg/core.py")
    assert "[tests outdated]" in note
    exec_ev = next(e for e in runner.engine.evidence if e.kind == "exec")
    assert exec_ev.state == EXEC_STALE
    runner("python3 -c 'import pkg.core; print(pkg.core.compute(1, 2))'")
    assert exec_ev.state == SUPERSEDED


def test_eviction_plan(env):
    runner = Runner(env, evict_keep_last=1, evict_min_chars=100, expected_steps=50)
    output, note = runner("cat -n pkg/big.py")
    eids = [e.eid for e in runner.engine.evidence if e.command == "cat -n pkg/big.py"]
    messages = [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "task"},
        {"role": "user", "content": output["output"], "extra": {"synapse_eids": eids}},
        {"role": "assistant", "content": "ok"},
    ]
    assert runner.engine.plan_eviction(messages, step=2) == []
    runner("sed -i 's/v10 = 10/v10 = 11/' pkg/big.py")
    plan = runner.engine.plan_eviction(messages, step=3)
    assert [i for i, _ in plan] == [2]
    assert "removed from context" in plan[0][1]
    messages[2]["extra"]["synapse_evicted"] = True
    assert runner.engine.plan_eviction(messages, step=3) == []
