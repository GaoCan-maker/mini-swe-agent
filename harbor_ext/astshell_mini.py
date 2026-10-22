"""Harbor agent that installs this repository (with AST-Shell) instead of mini-swe-agent from PyPI.

Usage: harbor run --agent-import-path harbor_ext.astshell_mini:ASTShellMiniSweAgent ...
with a config that sets `agent.agent_class: astshell` (see `harbor_ext/astshell.yaml`).
"""

import shlex
from pathlib import Path

from harbor.agents.installed.mini_swe_agent import _MINI_SWE_AGENT_PYTHON_VERSION, MiniSweAgent
from harbor.environments.base import BaseEnvironment

REPO = Path(__file__).resolve().parent.parent
UPLOAD_DIR = "/tmp/astshell-src"


class ASTShellMiniSweAgent(MiniSweAgent):
    async def install(self, environment: BaseEnvironment) -> None:
        await self.ensure_system_dependencies(environment, ("curl", "bash", "build_tools", "git"))
        await environment.upload_dir(REPO / "src", f"{UPLOAD_DIR}/src")
        for name in ("pyproject.toml", "README.md", "LICENSE.md"):
            await environment.upload_file(REPO / name, f"{UPLOAD_DIR}/{name}")
        await self.exec_as_root(environment, command=f"chmod -R a+rX {UPLOAD_DIR}")
        py = shlex.quote(_MINI_SWE_AGENT_PYTHON_VERSION)
        await self.exec_as_agent(
            environment,
            command=(
                "set -euo pipefail; "
                "if ! command -v uv >/dev/null 2>&1; then curl -LsSf https://astral.sh/uv/install.sh | sh; fi && "
                'if ! grep -q \'export PATH="$HOME/.local/bin:$PATH"\' "$HOME/.bashrc" 2>/dev/null; then'
                '  echo \'export PATH="$HOME/.local/bin:$PATH"\' >> "$HOME/.bashrc"; fi && '
                'if [ -f "$HOME/.local/bin/env" ]; then source "$HOME/.local/bin/env"; fi && '
                'export PATH="$HOME/.local/bin:$PATH" && '
                f'rm -rf "$HOME/astshell-src" && cp -r {UPLOAD_DIR} "$HOME/astshell-src" && '
                f"uv python install {py} && "
                f'uv tool install --force --python {py} "$HOME/astshell-src" '
                "--with litellm --with orjson --with fastapi --with tree-sitter --with tree-sitter-language-pack && "
                "mini-swe-agent --help >/dev/null && "
                f'"$(uv tool dir)/mini-swe-agent/bin/python" -c '
                "'import tree_sitter_language_pack as p; p.get_parser(\"python\")'"
            ),
        )
