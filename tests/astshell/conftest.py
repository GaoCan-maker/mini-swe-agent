import textwrap
from pathlib import Path

import pytest

pytest.importorskip("tree_sitter_language_pack")

from minisweagent.astshell import ASTShell, ASTShellConfig  # noqa: E402
from minisweagent.environments.local import LocalEnvironment  # noqa: E402


def _python_source() -> str:
    methods = "\n".join(
        textwrap.dedent(f'''
            @property
            def prop_{i}(self) -> int:
                """Docstring {i}."""
                value = {i}
                value += 1
                value += 2
                value += 3
                value += 4
                value += 5
                value += 6
                value += 7
                value += 8
                value += 9
                value += 10
                value += 11
                return value
        ''').replace("\n", "\n    ")
        for i in range(40)
    )
    return (
        "import os\nimport sys\n\n\n"
        "def helper(x):\n    return x + 1\n\n\n"
        "# A comment about Service\n"
        f"class Service:\n    name = 'svc'\n{methods}\n\n\n"
        "def tail_function(a, b):\n" + "".join(f"    a += {i}\n" for i in range(20)) + "    return a + b\n"
    )


GO_SOURCE = """package services

import "fmt"

type ContainerService struct {
\tName string
\tID   int
}

// CreateContainer creates a container.
func (s *ContainerService) CreateContainer(name string,
\tid int) error {
\tif name == "" {
\t\treturn fmt.Errorf("empty")
\t}
\treturn nil
}

func NewContainerService() *ContainerService {
\treturn &ContainerService{}
}
"""

TS_SOURCE = """import { x } from "y";

export class Store {
  private items: string[] = [];

  add(item: string): void {
    this.items.push(item);
  }
}

export const handler = (req: Request) => {
  return req;
};

interface Shape {
  area(): number;
}
"""

RUST_SOURCE = """use std::fmt;

/// A point.
#[derive(Debug)]
pub struct Point {
    x: i32,
}

impl fmt::Display for Point {
    fn fmt(&self, f: &mut fmt::Formatter) -> fmt::Result {
        write!(f, "{}", self.x)
    }
}

fn helper() -> i32 {
    1
}
"""


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "service.py").write_text(_python_source())
    (tmp_path / "pkg" / "small.py").write_text("def f():\n    return 1\n")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_service.py").write_text(
        "".join(f"def test_{i}():\n    assert tail_function({i}, 1)\n\n" for i in range(30))
    )
    (tmp_path / "svc.go").write_text(GO_SOURCE)
    (tmp_path / "store.ts").write_text(TS_SOURCE)
    (tmp_path / "point.rs").write_text(RUST_SOURCE)
    (tmp_path / "README.md").write_text("tail_function docs\n")
    return tmp_path


@pytest.fixture
def env(repo: Path) -> LocalEnvironment:
    return LocalEnvironment(cwd=str(repo))


@pytest.fixture
def shell() -> ASTShell:
    return ASTShell(ASTShellConfig(enabled=True))
