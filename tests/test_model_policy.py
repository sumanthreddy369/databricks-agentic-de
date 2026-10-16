"""AGENT_MODEL_POLICY=local_only (agent/model_policy.py): every model call
stays on this machine. Genie, hosted planners, hosted embeddings and
Langfuse are refused or switched off; data access and guardrails unchanged."""

import json

import pytest

from agent import model_policy
from agent.local_llm import LocalModel
from agent.mcp_bridge import server_environment
from agent.orchestrator import DA_TOOLS, OrchestratorAgent
from agent.rules_planner import RulesPlanner


@pytest.fixture
def local_only(monkeypatch):
    monkeypatch.setenv("AGENT_MODEL_POLICY", "local_only")


class _Bridge:
    def dispatch(self, name, tool_input):
        raise AssertionError("not used")


class _HostedModel:
    """Any planner without runs_locally - a hosted API client."""

    def run_tool_loop(self, *a, **k):
        raise AssertionError("must never be called")


class _AsksGenie:
    runs_locally = True

    def __init__(self):
        self.offered = None

    def run_tool_loop(self, system, messages, tools, dispatch, max_turns=6):
        self.offered = [t["name"] for t in tools]
        result = dispatch("ask_genie", {"question": "How many patients are in the ICU?"})
        return result.content


def _agent(tmp_path, planner):
    return OrchestratorAgent(
        claude=planner, state_path=tmp_path / "s.json", audit_log_path=tmp_path / "a.jsonl", mcp_bridge=_Bridge()
    )


def test_hosted_planner_is_refused_before_it_sees_anything(local_only, tmp_path):
    with pytest.raises(model_policy.PolicyViolation):
        _agent(tmp_path, _HostedModel())


def test_local_and_no_model_planners_are_allowed(local_only, tmp_path):
    assert isinstance(_agent(tmp_path, RulesPlanner()).claude, RulesPlanner)
    assert isinstance(_agent(tmp_path, LocalModel()).claude, LocalModel)


def test_default_planner_is_the_local_model(local_only, tmp_path, monkeypatch):
    monkeypatch.setenv("AGENT_LOCAL_MODEL", "qwen3.5:4b")
    planner = _agent(tmp_path, None).claude
    assert isinstance(planner, LocalModel) and planner.model == "qwen3.5:4b"


def test_genie_is_not_offered_and_refused_if_asked(local_only, tmp_path):
    planner = _AsksGenie()
    result = _agent(tmp_path, planner).handle("How many patients are in the ICU?", mode="da")
    assert "ask_genie" not in planner.offered
    assert {t["name"] for t in DA_TOOLS} - set(planner.offered) == {"ask_genie"}
    assert "Genie is off" in json.loads(result.answer)["error"]


def test_without_the_policy_nothing_changes(monkeypatch, tmp_path):
    monkeypatch.delenv("AGENT_MODEL_POLICY", raising=False)
    planner = _AsksGenie()
    _agent(tmp_path, planner).handle("How many patients are in the ICU?", mode="da")
    assert "ask_genie" in planner.offered
    model_policy.check_planner(_HostedModel())  # allowed


def test_langfuse_tracing_is_off(local_only, monkeypatch):
    from agent.llm import Tracer

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    assert Tracer().enabled is False


def test_runbook_search_never_uses_the_hosted_embeddings(local_only, monkeypatch):
    import agent.tools.problem_catalog as pc

    class Index:
        def search(self, q, k):
            return [("M4", 0.60)]  # above the hosted floor (0.55), below the local one (0.64)

        def hybrid(self, q, k):
            return ["M4"]

    monkeypatch.setenv("RUNBOOK_SEARCH", "embeddings")
    monkeypatch.setattr(pc, "_semantic_index", Index())
    # Treated as local: the local floor applies, so this falls back to keyword.
    assert json.loads(pc.lookup_problem(query="recipe for bread").content)["ranking"] == "keyword"


def test_the_policy_reaches_the_tool_server(local_only):
    assert server_environment()["AGENT_MODEL_POLICY"] == "local_only"
