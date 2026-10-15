"""A model running on this machine as the agent's planner - nothing the agent
sees (tool results, already masked by the guardrails) leaves the laptop.

Talks to Ollama's OpenAI-compatible API (`ollama serve`, default
http://localhost:11434) through the same tool-calling loop as the
Databricks-hosted adapter (agent/databricks_llm.py), so OrchestratorAgent,
the guardrails, the approval gate and the evals are unchanged.

The address is checked: anything but this machine (localhost, 127.0.0.1,
::1) is refused, so a mistyped or injected URL can't quietly send prompts
and tool results elsewhere.
"""

from urllib.parse import urlparse

import httpx

from agent.databricks_llm import DatabricksModel
from agent.llm import Tracer

DEFAULT_LOCAL_MODEL = "qwen3:14b"
DEFAULT_BASE_URL = "http://localhost:11434"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def require_local(base_url: str) -> str:
    host = urlparse(base_url).hostname
    if host not in LOCAL_HOSTS:
        raise ValueError(f"local model must run on this machine (localhost), not {host!r}")
    return base_url.rstrip("/")


class LocalModel(DatabricksModel):
    def __init__(
        self,
        model: str = DEFAULT_LOCAL_MODEL,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 600.0,  # a 14B model on a laptop GPU can take minutes per turn
        transport: httpx.BaseTransport | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        self._base_url = require_local(base_url)
        # No Databricks config: nothing here talks to a workspace.
        super().__init__(model, config=object(), timeout=timeout, transport=transport, tracer=tracer)

    def _url(self) -> str:
        return f"{self._base_url}/v1/chat/completions"

    def _headers(self) -> dict:
        return {}

    def _body_extra(self) -> dict:
        return {"model": self.model}


def local_embedder(
    model: str = "nomic-embed-text", *, prefix: str = "", base_url: str = DEFAULT_BASE_URL, transport=None
):
    """Embeddings from Ollama on this machine, for agent/runbook_search.py.
    nomic-embed-text expects a task prefix: "search_document: " for pages,
    "search_query: " for questions (without them a stuck-sensor question
    missed its runbook)."""
    url = f"{require_local(base_url)}/api/embed"
    client = httpx.Client(timeout=300, transport=transport)

    def embed(texts: list[str]) -> list[list[float]]:
        response = client.post(url, json={"model": model, "input": [prefix + t for t in texts]})
        response.raise_for_status()
        return response.json()["embeddings"]

    return embed


def local_runbook_embedders(base_url: str = DEFAULT_BASE_URL) -> tuple:
    """(page embedder, question embedder) for nomic-embed-text."""
    return (
        local_embedder(prefix="search_document: ", base_url=base_url),
        local_embedder(prefix="search_query: ", base_url=base_url),
    )
