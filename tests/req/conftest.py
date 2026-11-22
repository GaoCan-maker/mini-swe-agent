import shutil
import subprocess
from pathlib import Path

import pytest

from minisweagent.environments.local import LocalEnvironment

TASK = """Please solve this issue: Add stepped slicing helpers.

## Expected Behavior

1. Step handling
- Add `parse_step()` that returns the step as an integer.
- A step of `0` must raise an error that starts with: `slice step cannot be 0`.
- Expose `reset_cache()` to clear the slice cache.

IMPORTANT: Please work on this in a new branch from main and commit everything when you are done.
"""

CORE = """def compute(a, b):
    return a + b
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "core.py").write_text(CORE)
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.com")
    git(root, "config", "user.name", "t")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def env(repo: Path) -> LocalEnvironment:
    return LocalEnvironment(cwd=str(repo), timeout=30)
