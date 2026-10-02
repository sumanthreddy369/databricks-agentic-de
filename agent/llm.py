"""Thin wrapper around the Anthropic SDK — no agent framework, no LangChain.

Deliberately just a plain class with a tool-calling loop, mirroring the
`abhay` sibling project's core/model_router.py shape (a small dataclass +
direct SDK calls). `Claude` is injected into `OrchestratorAgent` rather than
constructed globally, so tests can substitute a scripted fake object with the
same `run_tool_loop` signature and make zero real network calls.

Two guardrails live here:
- Retry + timeout: `_create_message` wraps the real `messages.create(...)`
  call in `tenacity.retry` (exponential backoff, 3 attempts), retrying only
  on `anthropic.APIConnectionError`/`anthropic.RateLimitError` — transient,
  retry-worthy failures — never on every exception (a genuine bad-request or
  auth error should surface immediately, not be silently retried 3 times).
  `timeout` is passed straight through to the SDK call.
- Optional Langfuse tracing (`Tracer`): a genuine no-op — no import of
  `langfuse`, no network calls — unless both `LANGFUSE_PUBLIC_KEY` and
  `LANGFUSE_SECRET_KEY` are set. Injectable the same way `Claude` itself is,
  so the test suite never needs real Langfuse credentials. NOTE: the actual
  Langfuse span/trace calls below have not been exercised against a real
  Langfuse account in this environment (no credentials here) — they're
  written defensively (best-effort, swallow tracing errors) precisely
  because tracing must never be able to break the agent's real job.
"""

import os
import time
from collections.abc import Callable
from dataclasses import dataclass

import anthropic
import tenacity

DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_TOKENS = 16000
REFUSAL_ANSWER = "The model declined this request; no answer was produced."
TRUNCATED_ANSWER = "The model's response was cut off before it finished; no answer was produced."

# Retry only failures shaped like "the network/service hiccuped, try again" —
# never a blanket `except Exception`, which would also retry e.g. a 400 bad
# request or an auth failure that retrying can never fix.
_RETRYABLE_ANTHROPIC_ERRORS = (anthropic.APIConnectionError, anthropic.RateLimitError)


@dataclass
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


class Tracer:
    """Optional Langfuse tracing wrapper around one `run_tool_loop` call.

    `enabled` is False (and every method below a true no-op) unless both
    `LANGFUSE_PUBLIC_KEY` and `LANGFUSE_SECRET_KEY` are present (as
    constructor args or environment variables) — the default `Claude()`
    therefore makes zero Langfuse network calls, which is what the whole
    pytest suite exercises. `LANGFUSE_HOST` is optional and defaults to
    Langfuse's own default (cloud) when unset.

    Any object exposing `trace_turn(turn, *, latency_s, usage)` and
    `trace_tool(turn, tool_name, *, latency_s)` can be substituted for this
    class (the same injection pattern `Claude` itself uses), which is how
    tests assert tracing was invoked without needing real credentials.
    """

    def __init__(
        self,
        public_key: str | None = None,
        secret_key: str | None = None,
        host: str | None = None,
    ) -> None:
        public_key = public_key or os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret_key = secret_key or os.environ.get("LANGFUSE_SECRET_KEY")
        self.enabled = bool(public_key and secret_key)
        self._client = None
        if self.enabled:
            # Imported lazily: only reached when real credentials are
            # configured, so `import langfuse` is never on the hot path for
            # local dev / CI / the test suite.
            from langfuse import Langfuse

            self._client = Langfuse(
                public_key=public_key,
                secret_key=secret_key,
                host=host or os.environ.get("LANGFUSE_HOST"),
            )

    def trace_turn(self, turn: int, *, latency_s: float, usage: dict | None) -> None:
        if not self.enabled:
            return
        self._safe_trace(
            name=f"orchestrator-turn-{turn}",
            metadata={"turn": turn, "latency_s": latency_s, "usage": usage or {}},
        )

    def trace_tool(self, turn: int, tool_name: str, *, latency_s: float) -> None:
        if not self.enabled:
            return
        self._safe_trace(
            name=f"tool-{tool_name}",
            metadata={"turn": turn, "tool": tool_name, "latency_s": latency_s},
        )

    def _safe_trace(self, *, name: str, metadata: dict) -> None:
        # Tracing is observability, not correctness — a Langfuse outage or an
        # SDK-shape mismatch must never break the agent's actual tool loop.
        try:
            self._client.trace(name=name, metadata=metadata)
        except Exception:
            pass


class Claude:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-sonnet-5",
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        tracer: Tracer | None = None,
    ) -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model
        self.timeout = timeout
        self.tracer = tracer if tracer is not None else Tracer()

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type(_RETRYABLE_ANTHROPIC_ERRORS),
        stop=tenacity.stop_after_attempt(3),
        wait=tenacity.wait_exponential(multiplier=0.5, max=4),
        reraise=True,
    )
    def _create_message(self, *, system: str, messages: list[dict], tools: list[dict]):
        return self._client.messages.create(
            model=self.model,
            # Current models think by default, and thinking counts toward
            # max_tokens; 1024 could cut a turn off mid-thought. 16000 keeps a
            # non-streaming call well inside SDK timeouts.
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,
            tools=tools,
            timeout=self.timeout,
        )

    def run_tool_loop(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        dispatch: Callable[[str, dict], ToolResult],
        max_turns: int = 6,
    ) -> str:
        """Drives a standard Anthropic tool-use loop: call the model, and if
        it asks for tools, dispatch each one and feed the results back as a
        user turn, until the model returns a plain text answer or max_turns
        is exhausted. `messages` is mutated in place so callers (and tests)
        can inspect the full transcript afterward.
        """
        for turn in range(max_turns):
            start = time.monotonic()
            response = self._create_message(system=system, messages=messages, tools=tools)
            latency_s = time.monotonic() - start

            usage = None
            response_usage = getattr(response, "usage", None)
            if response_usage is not None:
                usage = {
                    "input_tokens": getattr(response_usage, "input_tokens", None),
                    "output_tokens": getattr(response_usage, "output_tokens", None),
                }
            self.tracer.trace_turn(turn, latency_s=latency_s, usage=usage)

            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason == "refusal":
                # A declined request is not an answer; say so plainly rather
                # than returning whatever (often empty) text came back.
                return REFUSAL_ANSWER
            if response.stop_reason == "max_tokens":
                # Cut off mid-response: returning the partial text as if it
                # were a finished answer would read as a real conclusion.
                return TRUNCATED_ANSWER
            if response.stop_reason != "tool_use":
                return "".join(b.text for b in response.content if b.type == "text")
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_start = time.monotonic()
                    result = dispatch(block.name, block.input)
                    self.tracer.trace_tool(turn, block.name, latency_s=time.monotonic() - tool_start)
                    tool_results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result.content,
                            "is_error": result.is_error,
                        }
                    )
            messages.append({"role": "user", "content": tool_results})
        return "Gave up after max_turns without a final answer."
