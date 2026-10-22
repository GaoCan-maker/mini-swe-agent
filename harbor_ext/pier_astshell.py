"""Pier agent that installs a local mini-swe-agent wheel (with AST-Shell) instead of the PyPI release.

Usage: pier run ... --agent-import-path harbor_ext.pier_astshell:PierASTShellAgent
    --ak package_spec=<wheel url> --ak config_file=<repo>/harbor_ext/astshell.yaml
"""

import shlex

from pier.agents.installed.mini_swe_agent import MiniSweAgent

TREE_SITTER = "tree-sitter tree-sitter-language-pack"
# Parsers are downloaded at first use; the agent run has no internet access, so fetch them at build time.
PREFETCH = (
    "import tree_sitter_language_pack as p; p.download(['python', 'go', 'typescript', 'tsx', 'javascript', 'rust'])"
)


class PierASTShellAgent(MiniSweAgent):
    def __init__(self, package_spec: str, *args, **kwargs):
        self._package_spec = package_spec
        super().__init__(*args, **kwargs)

    def install_spec(self):
        spec = super().install_spec()
        step = spec.steps[1]
        for anchor in ("uv tool install mini-swe-agent\n", "\nmini-swe-agent --help\n"):
            if anchor not in step.run:
                raise RuntimeError(f"Pier install script changed, cannot find {anchor!r}")
        step.run = step.run.replace(
            "uv tool install mini-swe-agent\n", f"uv tool install --force {shlex.quote(self._package_spec)}\n", 1
        ).replace(
            "\nmini-swe-agent --help\n",
            f'\n{{ uv pip install --python "$python_bin" {TREE_SITTER} && "$python_bin" -c {shlex.quote(PREFETCH)}; }}'
            ' || echo "ast-shell: tree-sitter unavailable" >&2\n'
            "mini-swe-agent --help\n",
            1,
        )
        return spec
