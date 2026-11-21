import shutil
import subprocess
from pathlib import Path

import pytest

from minisweagent.environments.local import LocalEnvironment

CORE = """def compute(a, b):
    return a + b


def helper(x):
    return x * 2
"""

USE = """from pkg.core import compute


def run():
    return compute(1, 2)
"""

BIG = "".join(f"v{i} = {i}\n" for i in range(1, 61))


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    if shutil.which("git") is None:
        pytest.skip("git not available")
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "core.py").write_text(CORE)
    (root / "pkg" / "use.py").write_text(USE)
    (root / "pkg" / "big.py").write_text(BIG)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def env(repo: Path) -> LocalEnvironment:
    return LocalEnvironment(cwd=str(repo), timeout=30)


@pytest.fixture
def tree_sitter():
    pytest.importorskip("tree_sitter_language_pack")
    from minisweagent.synapse.symbols import tree_sitter_available

    if not tree_sitter_available():
        pytest.skip("tree-sitter parsers not available")
