import subprocess
import time
from pathlib import Path

import pytest

from minisweagent.environments.extra.local_feedback import LocalFeedbackEnvironment
from minisweagent.exceptions import Submitted

SOURCE = "def f():\n    return 1\n\n\ndef g():\n    return 2 + f()\n"
needs_proc = pytest.mark.skipif(not Path("/proc").is_dir(), reason="needs /proc")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "a.py").write_text(SOURCE)
    (tmp_path / "notes.txt").write_text("hello\n")
    subprocess.run(
        "git init -q && git add . && git -c user.name=t -c user.email=t@t commit -qm init",
        shell=True,
        cwd=tmp_path,
        check=True,
    )
    return tmp_path


def run(env: LocalFeedbackEnvironment, command: str) -> str:
    return env.execute({"command": command})["output"]


def test_receipt_shows_lines_scope_and_generated_hunk(repo):
    pytest.importorskip("tree_sitter_language_pack")
    env = LocalFeedbackEnvironment(cwd=str(repo))
    assert run(env, "echo hi") == "hi\n"
    assert (
        run(env, "sed -i 's/return 2/return 3/' a.py")
        == "[edit] a.py L6 (+1/-1) in g, syntax ok\n     6\t    return 3 + f()"
    )
    assert run(env, "sed -i 's/hello/bye/' notes.txt && echo done") == "done\n"


def test_receipt_skips_hunk_written_by_model(repo):
    pytest.importorskip("tree_sitter_language_pack")
    env = LocalFeedbackEnvironment(cwd=str(repo))
    run(env, "true")
    new_source = SOURCE.replace("return 1", "return 10")
    assert run(env, f"cat > a.py <<'EOF'\n{new_source}EOF") == "[edit] a.py L2 (+1/-1) in f, syntax ok"


def test_receipt_reports_new_syntax_errors_only(repo):
    pytest.importorskip("tree_sitter_language_pack")
    env = LocalFeedbackEnvironment(cwd=str(repo))
    run(env, "true")
    assert "[edit] a.py L6 (+1/-1) in g, syntax ERROR near L" in run(env, "sed -i 's/return 2/return (2/' a.py")
    assert run(env, "sed -i 's/return 1/return 5/' a.py") == "[edit] a.py L2 (+1/-1) in f"
    assert run(env, "sed -i 's/return (2/return 2/' a.py").startswith("[edit] a.py L6 (+1/-1) in g, syntax ok\n")


def test_receipt_tracks_created_deleted_and_reverted_files(repo):
    pytest.importorskip("tree_sitter_language_pack")
    (repo / "a.py").write_text(SOURCE + "# dirty before the run\n")
    env = LocalFeedbackEnvironment(cwd=str(repo))
    assert run(env, "true") == ""
    out = run(env, "mkdir -p pkg && printf 'x = 1\\n' > pkg/b.py && rm a.py")
    assert out.splitlines() == ["[edit] a.py: deleted", "[edit] pkg/b.py (created, 1 lines), syntax ok"]
    assert run(env, "git checkout -q a.py && echo ok").startswith("ok\n[edit] a.py (created, 6 lines), syntax ok\n")
    assert run(env, "echo 'x = 2' > pkg/b.py") == "[edit] pkg/b.py L1 (+1/-1), syntax ok"


def test_receipt_limits_number_of_files(repo):
    env = LocalFeedbackEnvironment(cwd=str(repo), receipt_max_files=2, receipt_max_hunk_lines=0)
    run(env, "true")
    lines = run(env, "for i in 1 2 3 4; do echo 'x = 1' > m$i.py; done").splitlines()
    assert [line.split(" (")[0] for line in lines] == ["[edit] m1.py", "[edit] m2.py", "[edit] +2 more files"]


def test_no_receipt_outside_git_repo_or_when_disabled(tmp_path_factory, repo):
    plain = LocalFeedbackEnvironment(cwd=str(tmp_path_factory.mktemp("plain")))
    run(plain, "true")
    assert run(plain, "echo 'x = 1' > c.py && echo ok") == "ok\n"
    env = LocalFeedbackEnvironment(cwd=str(repo), edit_receipt=False)
    run(env, "true")
    assert run(env, "echo 'x = 1' > d.py && echo ok") == "ok\n"


def test_submission_is_not_affected(repo):
    env = LocalFeedbackEnvironment(cwd=str(repo))
    run(env, "true")
    with pytest.raises(Submitted):
        env.execute({"command": "echo 'x = 1' > e.py && echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"})


@needs_proc
def test_wait_replaces_sleep_until_background_process_exits(tmp_path):
    env = LocalFeedbackEnvironment(cwd=str(tmp_path), wait_pattern="feedback_marker_a")
    assert run(env, "sleep 0.1 && echo idle") == "idle\n"
    run(env, "nohup python3 -c 'import time; time.sleep(2)' feedback_marker_a > /dev/null 2>&1 &")
    start = time.monotonic()
    out = run(env, "cd /tmp && sleep 20 && pwd")
    assert out.startswith("[waited ") and out.endswith("\n/tmp\n") and time.monotonic() - start < 8


@needs_proc
def test_wait_gives_up_on_long_running_process(tmp_path):
    env = LocalFeedbackEnvironment(cwd=str(tmp_path), wait_pattern="feedback_marker_b", timeout=12)
    run(env, "nohup python3 -c 'import time; time.sleep(60)' feedback_marker_b > /dev/null 2>&1 &")
    try:
        assert run(env, "sleep 1; echo first").startswith("[background process still running after 2s: python3")
        start = time.monotonic()
        assert run(env, "sleep 1; echo second") == "second\n" and time.monotonic() - start < 1.9
    finally:
        subprocess.run(["pkill", "-f", "feedback_marker_b"])
