import pytest
from jinja2 import StrictUndefined, Template

from minisweagent.config import get_config_from_spec
from minisweagent.environments import get_environment


@pytest.mark.parametrize(
    ("environment_class", "timeout", "mentions_receipt"),
    [("local", 30, False), ("local_feedback", 120, True)],
)
def test_batched_prompt_renders(environment_class, timeout, mentions_receipt):
    config = get_config_from_spec("mini_batched.yaml")
    env = get_environment(config["environment"] | {"environment_class": environment_class, "timeout": timeout})
    prompt = Template(config["agent"]["instance_template"], undefined=StrictUndefined).render(
        task="fix it", **env.get_template_vars()
    )
    assert f"times out after {timeout} seconds" in prompt
    assert ("[edit] src/app.py" in prompt) == mentions_receipt
    assert "step-by-step" not in prompt and "fix it" in prompt
