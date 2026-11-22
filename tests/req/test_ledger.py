import pytest

from minisweagent.exceptions import Submitted
from minisweagent.req import ReqConfig, RequirementLedger
from minisweagent.req.ledger import MISSING, STALE, UNVERIFIED, VERIFIED

from .conftest import TASK, git

SUBMIT = "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"
IMPL = """cat > pkg/slicing.py <<'EOF'
_cache = {}


def parse_step(text):
    return int(text)


def reset_cache():
    _cache.clear()
EOF"""
CHECK = """python3 -c "from pkg.slicing import parse_step; assert parse_step('2') == 2; print('parse_step ok')" """


class Runner:
    def __init__(self, env, **config):
        self.env = env
        self.ledger = RequirementLedger(ReqConfig(**config))
        self.ledger.reset(TASK)
        self.step = 0

    def __call__(self, command: str) -> dict:
        self.step += 1
        return self.ledger.run_action({"command": command}, self.env.execute, step=self.step)

    def status(self, needle: str) -> str:
        clause = next(c for c in self.ledger.clauses if needle in c.text)
        return self.ledger.statuses[clause.cid]


def test_clauses_from_task():
    ledger = RequirementLedger(ReqConfig())
    ledger.reset(TASK)
    texts = [c.text for c in ledger.clauses]
    assert texts[0] == "Add stepped slicing helpers."
    assert not any("new branch" in t for t in texts)
    assert len(texts) == 4


def test_gate_lists_unchecked_requirements_once(env):
    run = Runner(env)
    run(IMPL)
    assert run(CHECK)["returncode"] == 0
    held = run(SUBMIT)
    assert "Submission held back" in held["output"]
    assert held["extra"] == {"req_gate": True}
    assert run.status("parse_step") == VERIFIED
    assert run.status("slice step cannot be 0") == MISSING
    assert run.status("reset_cache") == UNVERIFIED
    assert "slice step cannot be 0" in held["output"]
    assert "parse_step()" not in held["output"].split("Covered by")[0]
    with pytest.raises(Submitted):
        run(SUBMIT)
    summary = run.ledger.summary()
    assert summary["gate_fired"] and summary["checks"][0]["passed"]
    assert summary["last_change"] == {"pkg/slicing.py": 1}


def test_edit_after_check_makes_requirement_stale(env):
    run = Runner(env)
    run(IMPL)
    run(CHECK)
    run("sed -i 's/return int(text)/return int(text.strip())/' pkg/slicing.py")
    run(SUBMIT)
    assert run.status("parse_step") == STALE


def test_passing_test_suite_after_edit_keeps_requirement_verified(env):
    run = Runner(env)
    run(IMPL)
    run(CHECK)
    run("sed -i 's/return int(text)/return int(text.strip())/' pkg/slicing.py")
    run(
        "mkdir -p tests && cat > tests/test_core.py <<'EOF'\nimport unittest\n\n\n"
        "class T(unittest.TestCase):\n    def test_ok(self):\n        self.assertTrue(True)\nEOF"
    )
    run("python3 -m unittest discover -s tests -q")
    run(SUBMIT)
    assert run.status("parse_step") == VERIFIED
    assert run.status("reset_cache") == UNVERIFIED


def test_meta_clauses_and_grouped_rendering():
    ledger = RequirementLedger(ReqConfig())
    ledger.reset(
        "## Rules\n- Add `alpha_rule()` support.\n- Add `beta_rule()` support.\n"
        "- Exact trace text format is implementation-defined.\n"
    )
    statuses = ledger.evaluate({"pkg/rules.py": "def alpha_rule(): pass"})
    assert statuses == {1: UNVERIFIED, 2: MISSING, 3: "vague"}
    message = ledger.render(statuses)
    assert message.count("[Rules]") == 1
    assert "#1 (implemented, but no passing check exercises it) Add `alpha_rule()` support." in message
    assert "#2 (no matching code change found)" in message
    assert "implementation-defined" not in message


def test_render_drops_least_actionable_items_first():
    ledger = RequirementLedger(ReqConfig(max_gate_chars=1700))
    ledger.reset("\n".join(f"- Add `rule_{i}()` with a fairly long explanation of the behavior." for i in range(12)))
    statuses = {c.cid: STALE if c.cid % 2 else MISSING for c in ledger.clauses}
    message = ledger.render(statuses)
    assert "Not shown for space:" in message
    assert "#2 (no matching code change found)" in message
    assert "#1 (" not in message.split("Not shown")[0]


def test_failed_check_does_not_verify(env):
    run = Runner(env)
    run(IMPL)
    run("""python3 -c "from pkg.slicing import parse_step; assert parse_step('2') == 3" """)
    run(SUBMIT)
    assert run.status("parse_step") == UNVERIFIED


def test_script_file_content_counts_as_evidence(env):
    run = Runner(env)
    run(IMPL)
    run(
        "cat > check_slicing.py <<'EOF'\nfrom pkg.slicing import reset_cache\nreset_cache()\nprint('done')\nEOF\n"
        "python3 check_slicing.py"
    )
    run(SUBMIT)
    assert run.status("reset_cache") == VERIFIED


def test_changes_committed_on_new_branch_are_still_seen(env, repo):
    run = Runner(env)
    run(IMPL)
    run("git checkout -q -b feature && git add -A && git commit -q -m impl")
    held = run(SUBMIT)
    assert "Submission held back" in held["output"]
    assert run.status("reset_cache") == UNVERIFIED
    assert git(repo, "status", "--porcelain") == ""


def test_no_gate_without_changes(env):
    run = Runner(env)
    with pytest.raises(Submitted):
        run(SUBMIT)
    assert run.ledger.stats["gate_no_diff"] == 1


def test_no_gate_when_everything_is_verified(env):
    run = Runner(env)
    run(
        IMPL.replace(
            "return int(text)",
            "if int(text) == 0:\n        raise ValueError('slice step cannot be 0')\n    return int(text)",
        )
    )
    run(
        "cat > check_slicing.py <<'EOF'\n"
        "from pkg.slicing import parse_step, reset_cache\n"
        "assert parse_step('2') == 2\n"
        "reset_cache()\n"
        "try:\n    parse_step('0')\nexcept ValueError as e:\n    assert str(e).startswith('slice step cannot be 0')\n"
        "print('stepped slicing helpers ok')\n"
        "EOF\npython3 check_slicing.py"
    )
    with pytest.raises(Submitted):
        run(SUBMIT)
    assert set(run.ledger.statuses.values()) == {VERIFIED}


def test_disabled_outside_git(tmp_path):
    from minisweagent.environments.local import LocalEnvironment

    env = LocalEnvironment(cwd=str(tmp_path), timeout=30)
    run = Runner(env)
    run("echo hi > a.py")
    with pytest.raises(Submitted):
        run(SUBMIT)
    assert run.ledger.disabled == "no_git"


def test_disabled_by_config(env):
    run = Runner(env, enabled=False)
    run(IMPL)
    with pytest.raises(Submitted):
        run(SUBMIT)
    assert run.ledger.stats["probe_calls"] == 0
