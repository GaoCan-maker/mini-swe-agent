from pathlib import Path

import yaml

from minisweagent.agents import get_agent, get_agent_class
from minisweagent.agents.req import ReqAgent
from minisweagent.models.test_models import (
    DeterministicModel,
    DeterministicToolcallModel,
    make_output,
    make_toolcall_output,
)

from .conftest import TASK
from .test_ledger import CHECK, IMPL, SUBMIT

MINI_CONFIG = Path(__file__).parents[2] / "src" / "minisweagent" / "config" / "mini.yaml"


def _agent(env, commands: list[str], **req) -> ReqAgent:
    model = DeterministicModel(outputs=[make_output(f"step {i}", [{"command": c}]) for i, c in enumerate(commands)])
    return ReqAgent(
        model,
        env,
        system_template="You are a helpful assistant.",
        instance_template="{{task}}",
        mode="yolo",
        confirm_exit=False,
        cost_limit=0,
        req=req,
    )


def test_agent_class_registered():
    assert get_agent_class("req") is ReqAgent


def test_agent_holds_first_submission_once(env):
    agent = _agent(env, [IMPL, CHECK, SUBMIT, SUBMIT])
    result = agent.run(TASK)
    assert result["exit_status"] == "Submitted"
    held = [m for m in agent.messages if "Submission held back" in str(m.get("content", ""))]
    assert len(held) == 1
    assert held[0]["extra"]["req_gate"] is True
    data = agent.serialize()["info"]["req"]
    assert data["enabled"] and data["gate_fired"]
    assert data["n_clauses"] == 4
    assert {c["status"] for c in data["clauses"]} >= {"verified", "missing"}


def test_agent_disabled_behaves_like_interactive(env):
    agent = _agent(env, [IMPL, SUBMIT], enabled=False)
    result = agent.run(TASK)
    assert result["exit_status"] == "Submitted"
    assert agent.n_calls == 2
    assert agent.serialize()["info"]["req"]["disabled_reason"] == "config"


def test_agent_with_mini_config(env):
    config = yaml.safe_load(MINI_CONFIG.read_text())
    agent_config = {**config["agent"], "agent_class": "req", "mode": "yolo", "confirm_exit": False, "cost_limit": 0}
    outputs = []
    for i, command in enumerate([IMPL, SUBMIT, SUBMIT]):
        call = {"id": f"call_{i}", "type": "function", "function": {"name": "bash", "arguments": "{}"}}
        outputs.append(make_toolcall_output(f"step {i}", [call], [{"command": command, "tool_call_id": f"call_{i}"}]))
    model = DeterministicToolcallModel(outputs=outputs, observation_template=config["model"]["observation_template"])
    agent = get_agent(model, env, agent_config)
    assert isinstance(agent, ReqAgent)
    result = agent.run(TASK)
    assert result["exit_status"] == "Submitted"
    held = [m for m in agent.messages if "Submission held back" in str(m.get("content", ""))]
    assert len(held) == 1
