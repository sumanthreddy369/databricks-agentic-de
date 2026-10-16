"""Where the agent's model calls may go.

    AGENT_MODEL_POLICY=local_only

keeps every model call on this machine - for teams whose worry about agents
is data leaving for a model provider:

- the planner must run locally (agent/local_llm.py's Ollama model, the
  deterministic rules planner, or no model at all); anything else is refused
  when the agent is built, and the default planner becomes the local model
  (AGENT_LOCAL_MODEL, default qwen3:14b) instead of Claude;
- Genie (a model in the Databricks workspace) is neither offered to the model
  nor dispatched; analyst questions go through compute_metric /
  aggregate_gold_table, which are plain SQL;
- runbook search uses the local embedding model, never the hosted one;
- Langfuse tracing is off (it sends per-turn metadata to Langfuse's servers).

What it doesn't change: the agent still reads its data from Databricks over
SQL, where that data already lives, and every guardrail (masking, injection
scan, approval gate, audit log) runs as before. Unset (the default), any
planner is allowed.
"""

import os

POLICY_ENV = "AGENT_MODEL_POLICY"
LOCAL_ONLY = "local_only"


class PolicyViolation(RuntimeError):
    """A model call that would leave this machine under local_only."""


def local_only() -> bool:
    return os.environ.get(POLICY_ENV, "").strip().lower() == LOCAL_ONLY


def check_planner(planner) -> None:
    if local_only() and not getattr(planner, "runs_locally", False):
        raise PolicyViolation(
            f"{POLICY_ENV}=local_only: {type(planner).__name__} doesn't run on this machine; use the local model "
            "(--planner local) or the rules planner"
        )


def default_planner():
    """The planner when none is given: the local model under local_only,
    Claude otherwise (the original default)."""
    if local_only():
        from agent.local_llm import DEFAULT_LOCAL_MODEL, LocalModel

        return LocalModel(os.environ.get("AGENT_LOCAL_MODEL", DEFAULT_LOCAL_MODEL))
    from agent.llm import Claude

    return Claude()
