from pathlib import Path

import yaml

from minisweagent.agents import get_agent, get_agent_class
from minisweagent.agents.synapse import SynapseAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import (
    DeterministicModel,
    DeterministicToolcallModel,
    make_output,
    make_toolcall_output,
)

MINI_CONFIG = Path(__file__).parents[2] / "src" / "minisweagent" / "config" / "mini.yaml"


def _toolcall_model(commands: list[str], observation_template: str) -> DeterministicToolcallModel:
    outputs = []
    for i, command in enumerate(commands):
        call = {"id": f"call_{i}", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
        outputs.append(make_toolcall_output(f"step {i}", [call], [{"command": command, "tool_call_id": f"call_{i}"}]))
    return DeterministicToolcallModel(outputs=outputs, observation_template=observation_template)


def _agent(env: LocalEnvironment, commands: list[str], **synapse) -> SynapseAgent:
    model = DeterministicModel(outputs=[make_output(f"step {i}", [{"command": c}]) for i, c in enumerate(commands)])
    return SynapseAgent(
        model,
        env,
        system_template="You are a helpful assistant.",
        instance_template="{{task}}",
        mode="yolo",
        confirm_exit=False,
        cost_limit=0,
        synapse=synapse,
    )


def test_agent_class_registered():
    assert get_agent_class("synapse") is SynapseAgent


def test_agent_attaches_notes_and_submits(env):
    agent = _agent(
        env,
        [
            "cat -n pkg/big.py",
            "sed -i 's/v10 = 10/v10 = 11/' pkg/big.py",
            "python3 -c 'import pkg.big'",
            "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
        ],
    )
    result = agent.run("fix it")
    assert result["exit_status"] == "Submitted"
    assert "synapse" in agent.messages[0]["content"]
    edit_obs = agent.messages[5]
    assert "<synapse>" in edit_obs["content"]
    assert "[edit] pkg/big.py" in edit_obs["content"]
    assert edit_obs["extra"]["synapse_eids"]
    data = agent.serialize()
    assert data["info"]["synapse"]["actions"] == 4
    assert data["info"]["synapse"]["modified_files"] == ["pkg/big.py"]


def test_agent_gate_holds_once(env):
    agent = _agent(
        env,
        [
            "sed -i 's/v10 = 10/v10 = 11/' pkg/big.py",
            "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
            "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
        ],
    )
    result = agent.run("fix it")
    assert result["exit_status"] == "Submitted"
    held = [m for m in agent.messages if "Submission held back" in str(m.get("content", ""))]
    assert len(held) == 1


def test_agent_with_mini_config_toolcalls_and_footer(env, tree_sitter):
    config = yaml.safe_load(MINI_CONFIG.read_text())
    commands = [
        "cat -n pkg/core.py",
        "sed -i 's/def compute(a, b):/def compute(a, b, c):/' pkg/core.py",
        "cat -n pkg/big.py",
        "echo hi",
        "sed -i 's/compute(1, 2)/compute(1, 2, 3)/' pkg/use.py",
        "python3 -c 'import pkg.use'",
        "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT",
    ]
    model = _toolcall_model(commands, config["model"]["observation_template"])
    agent_config = {**config["agent"], "agent_class": "synapse", "mode": "yolo", "confirm_exit": False, "cost_limit": 0}
    agent = get_agent(model, env, agent_config)
    assert isinstance(agent, SynapseAgent)
    result = agent.run("change compute")
    assert result["exit_status"] == "Submitted"
    contract_obs = agent.messages[5]
    assert contract_obs["role"] == "tool"
    assert "[contract] compute changed its signature (breaking)" in contract_obs["content"]
    footers = [m["extra"]["synapse_footer"] for m in agent.messages if (m.get("extra") or {}).get("synapse_footer")]
    assert footers and "pkg/use.py:5 still uses compute" in footers[0]
    assert all("<synapse_status>" not in str(m.get("content")) for m in agent.messages)
    assert not any("Submission held back" in str(m.get("content")) for m in agent.messages)


def test_agent_disabled_is_plain(env):
    agent = _agent(env, ["cat pkg/big.py", "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"], enabled=False)
    result = agent.run("fix it")
    assert result["exit_status"] == "Submitted"
    assert all("<synapse>" not in str(m.get("content", "")) for m in agent.messages)
    assert "synapse" not in agent.messages[0]["content"]
