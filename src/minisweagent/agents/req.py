"""Interactive agent with a requirements ledger.

The task description is split into requirements. While the agent works, the ledger records which files
changed when and which checks (tests, scripts) passed. On the first submission, requirements that no
passing check exercised after their code last changed are listed once, and the agent can act on them
before submitting again.
"""

from pydantic import Field

from minisweagent.agents.interactive import InteractiveAgent, InteractiveAgentConfig
from minisweagent.exceptions import Submitted
from minisweagent.req import ReqConfig, RequirementLedger


class ReqAgentConfig(InteractiveAgentConfig):
    req: ReqConfig = Field(default_factory=ReqConfig)


class ReqAgent(InteractiveAgent):
    def __init__(self, *args, config_class=ReqAgentConfig, **kwargs):
        super().__init__(*args, config_class=config_class, **kwargs)
        self.ledger = RequirementLedger(self.config.req)

    def run(self, task: str = "", **kwargs) -> dict:
        self.ledger.reset(task)
        return super().run(task, **kwargs)

    def execute_actions(self, message: dict) -> list[dict]:
        actions = message.get("extra", {}).get("actions", [])
        commands = [action["command"] for action in actions]
        outputs = []
        try:
            self._ask_confirmation_or_interrupt(commands)
            for action in actions:
                outputs.append(self.ledger.run_action(action, self.env.execute, step=self.n_calls))
        except Submitted as e:
            self._check_for_new_task_or_submit(e)
        finally:
            result = self.add_messages(
                *self.model.format_observation_messages(message, outputs, self.get_template_vars())
            )
        return result

    def serialize(self, *extra_dicts) -> dict:
        return super().serialize({"info": {"req": self.ledger.summary()}}, *extra_dicts)
