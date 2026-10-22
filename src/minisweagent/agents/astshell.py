"""Interactive agent whose shell output is rewritten by AST-Shell. See design/ast_shell_design.md."""

from minisweagent import Environment, Model
from minisweagent.agents.interactive import InteractiveAgent, InteractiveAgentConfig
from minisweagent.astshell import ASTShell, ASTShellConfig
from minisweagent.utils.serialize import recursive_merge


class ASTShellAgentConfig(InteractiveAgentConfig):
    ast_shell: ASTShellConfig = ASTShellConfig()


class _ASTShellEnv:
    """Forwards everything to the wrapped environment, but routes `execute` through AST-Shell."""

    def __init__(self, env: Environment, agent: "ASTShellAgent"):
        self._env, self._agent = env, agent

    def execute(self, action: dict, cwd: str = "", **kwargs) -> dict:
        return self._agent.ast_shell.run(
            action, lambda a: self._env.execute(a, cwd, **kwargs), turn=self._agent.n_calls
        )

    def __getattr__(self, name: str):
        return getattr(self._env, name)


class ASTShellAgent(InteractiveAgent):
    def __init__(self, model: Model, env: Environment, *, config_class: type = ASTShellAgentConfig, **kwargs):
        super().__init__(model, _ASTShellEnv(env, self), config_class=config_class, **kwargs)
        self.ast_shell = ASTShell(self.config.ast_shell)

    def run(self, task: str = "", **kwargs) -> dict:
        self.ast_shell.reset(task=task)
        return super().run(task, **kwargs)

    def serialize(self, *extra_dicts) -> dict:
        return recursive_merge(super().serialize(), {"info": {"ast_shell": dict(self.ast_shell.stats)}}, *extra_dicts)
