"""Interactive agent with SYNAPSE context management.

Every action goes through the SYNAPSE engine, which reports what the action changed, which earlier observations
became outdated and what a contract change still requires. Outdated observations are evicted when it pays off.
"""

from pydantic import Field

from minisweagent.agents.interactive import InteractiveAgent, InteractiveAgentConfig
from minisweagent.exceptions import Submitted
from minisweagent.models.utils.content_string import get_content_string
from minisweagent.synapse import SynapseConfig, SynapseEngine


class SynapseAgentConfig(InteractiveAgentConfig):
    synapse: SynapseConfig = Field(default_factory=SynapseConfig)


class SynapseAgent(InteractiveAgent):
    def __init__(self, *args, config_class=SynapseAgentConfig, **kwargs):
        super().__init__(*args, config_class=config_class, **kwargs)
        self.synapse = SynapseEngine(self.config.synapse)
        cfg = self.config.synapse
        if cfg.enabled and cfg.prompt_addendum:
            self.config.system_template = self.config.system_template.rstrip() + "\n\n" + cfg.prompt_addendum
        model_config = getattr(self.model, "config", None)
        if cfg.enabled and cfg.footer and getattr(model_config, "set_cache_control", None) == "default_end":
            model_config.set_cache_control = "last_two"  # type: ignore[union-attr]

    def run(self, task: str = "", **kwargs) -> dict:
        self.synapse.reset()
        return super().run(task, **kwargs)

    def query(self) -> dict:
        footer = self.synapse.footer(self.n_calls + 1) if self.config.mode != "human" else ""
        footer_msg = self.model.format_message(role="user", content=footer) if footer else None
        if footer_msg is not None:
            self.messages.append(footer_msg)
        try:
            message = super().query()
        finally:
            if footer_msg is not None:
                self.messages = [m for m in self.messages if m is not footer_msg]
        if footer:
            message.setdefault("extra", {})["synapse_footer"] = footer
        self.synapse.observe_text(get_content_string(message))
        return message

    def execute_actions(self, message: dict) -> list[dict]:
        actions = message.get("extra", {}).get("actions", [])
        commands = [action["command"] for action in actions]
        outputs = []
        try:
            self._ask_confirmation_or_interrupt(commands)
            for action in actions:
                outputs.append(self._run_action(action))
        except Submitted as e:
            self._check_for_new_task_or_submit(e)
        finally:
            observations = self.model.format_observation_messages(message, outputs, self.get_template_vars())
            result = self.add_messages(*[self._attach_note(m) for m in observations])
        self._evict()
        return result

    def _run_action(self, action: dict) -> dict:
        output, note = self.synapse.run_action(action, self.env.execute, step=self.n_calls)
        if note is not None:
            extra = output.setdefault("extra", {})
            if note.eids:
                extra["synapse_eids"] = list(note.eids)
            if note.text:
                extra["synapse_note"] = note.text
        return output

    @staticmethod
    def _attach_note(message: dict) -> dict:
        note = (message.get("extra") or {}).get("synapse_note")
        if not note:
            return message
        content = message.get("content")
        if isinstance(content, str):
            message["content"] = content + "\n" + note
        elif isinstance(content, list):
            message["content"] = [*content, {"type": "text", "text": note}]
        return message

    def _evict(self) -> None:
        for i, stub in self.synapse.plan_eviction(self.messages, self.n_calls):
            msg = self.messages[i]
            extra = msg.setdefault("extra", {})
            extra["synapse_evicted_content"] = msg["content"]
            extra["synapse_evicted"] = True
            msg["content"] = stub

    def serialize(self, *extra_dicts) -> dict:
        return super().serialize({"info": {"synapse": self.synapse.summary()}}, *extra_dicts)
