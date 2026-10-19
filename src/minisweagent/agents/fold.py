"""Agent that folds overflowing code reads down to symbol signatures, keeping symbols relevant to its current intent."""

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.agents.utils.symbol_fold import FoldConfig, fold_output


class FoldAgentConfig(AgentConfig, FoldConfig):
    pass


class FoldAgent(DefaultAgent):
    def __init__(self, *args, config_class: type = FoldAgentConfig, **kwargs):
        super().__init__(*args, config_class=config_class, **kwargs)

    def _intent(self, message: dict) -> str:
        commands = [
            a["command"] for m in self.messages for a in m.get("extra", {}).get("actions", []) if "command" in a
        ]
        n_current = len(message.get("extra", {}).get("actions", []))
        content = message.get("content")
        return "\n".join(
            [
                content if isinstance(content, str) else str(content or ""),
                message.get("reasoning_content") or "",
                *commands[-(n_current + self.config.intent_history) :],
            ]
        )

    def _run_read(self, command: str) -> str:
        return self.env.execute({"command": command}, timeout=self.config.locate_timeout)["output"]

    def execute_actions(self, message: dict) -> list[dict]:
        actions = message.get("extra", {}).get("actions", [])
        outputs = [self.env.execute(action) for action in actions]
        intent = self._intent(message)
        for action, out in zip(actions, outputs):
            if out.get("returncode") == 0 and (
                res := fold_output(action["command"], out.get("output", ""), intent, self.config, self._run_read)
            ):
                out["extra"] = out.get("extra", {}) | {"fold": res[1] | {"full_output": out["output"]}}
                out["output"] = res[0]
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))
