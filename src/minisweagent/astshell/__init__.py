"""AST-Shell: symbol-aware rewriting of the output of the shell commands an agent already uses.
See design/ast_shell_design.md.
"""

from minisweagent.astshell.config import ASTShellConfig
from minisweagent.astshell.shell import ASTShell

__all__ = ["ASTShell", "ASTShellConfig"]
