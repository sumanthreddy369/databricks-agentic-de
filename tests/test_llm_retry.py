"""Proves agent/llm.py's guardrails work with zero real network calls:

- `Claude._create_message` retries only transient/network-shaped Anthropic
  errors (anthropic.APIConnectionError/RateLimitError), not arbitrary
  exceptions, and eventually returns success.
- `Tracer` (optional Langfuse tracing) is a genuine no-op with no
  LANGFUSE_* credentials set, and `run_tool_loop` invokes an injected tracer
  once per turn and once per tool dispatch.

The fake Anthropic-client-shaped object here is `claude._client.messages`
with its `create` method monkeypatched — `anthropic.Anthropic(...)` itself is
constructed (matching how `Claude.__init__` always does), but no real
`.messages.create(...)` call is ever allowed to run.
"""

import anthropic
import httpx
import pytest

from agent.llm import Claude, ToolResult, Tracer


class _FakeTextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _FakeUsage:
    def __init__(self, input_tokens: int = 10, output_tokens: int = 5) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _FakeTextResponse:
    def __init__(self, text: str) -> None:
        self.content = [_FakeTextBlock(text)]
        self.stop_reason = "end_turn"
        self.usage = _FakeUsage()


class _FakeToolUseBlock:
    type = "tool_use"
    id = "tu_1"
    name = "some_tool"
    input: dict = {}


class _FakeToolUseResponse:
    def __init__(self) -> None:
        self.content = [_FakeToolUseBlock()]
        self.stop_reason = "tool_use"
        self.usage = _FakeUsage()


def _connection_error() -> anthropic.APIConnectionError:
    """A real anthropic.APIConnectionError, constructed with a real (but
    never-sent) httpx.Request — no network call happens building this."""
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")
    return anthropic.APIConnectionError(request=request)


# --- retry behavior ----------------------------------------------------


def test_retries_transient_error_then_succeeds(monkeypatch):
    claude = Claude(api_key="test-key-never-sent")
    calls = {"count": 0}

    def flaky_create(**kwargs):
        calls["count"] += 1
        if calls["count"] < 3:
            raise _connection_error()
        return _FakeTextResponse("all good")

    monkeypatch.setattr(claude._client.messages, "create", flaky_create)

    answer = claude.run_tool_loop(
        system="sys", messages=[{"role": "user", "content": "hi"}], tools=[], dispatch=lambda *_: None
    )

    assert answer == "all good"
    assert calls["count"] == 3  # two transient failures, then success


def test_gives_up_after_stop_after_attempt_and_reraises(monkeypatch):
    claude = Claude(api_key="test-key-never-sent")
    calls = {"count": 0}

    def always_flaky(**kwargs):
        calls["count"] += 1
        raise _connection_error()

    monkeypatch.setattr(claude._client.messages, "create", always_flaky)

    with pytest.raises(anthropic.APIConnectionError):
        claude.run_tool_loop(
            system="sys", messages=[{"role": "user", "content": "hi"}], tools=[], dispatch=lambda *_: None
        )

    assert calls["count"] == 3  # exactly 3 attempts, per the retry config


def test_does_not_retry_non_transient_errors(monkeypatch):
    claude = Claude(api_key="test-key-never-sent")
    calls = {"count": 0}

    def not_retryable(**kwargs):
        calls["count"] += 1
        raise ValueError("not a transient/network-shaped error")

    monkeypatch.setattr(claude._client.messages, "create", not_retryable)

    with pytest.raises(ValueError):
        claude.run_tool_loop(
            system="sys", messages=[{"role": "user", "content": "hi"}], tools=[], dispatch=lambda *_: None
        )

    assert calls["count"] == 1  # no retry for a non-transient exception shape


# --- Langfuse tracer no-op / injection -----------------------------------


def test_tracer_is_noop_without_credentials(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    tracer = Tracer()

    assert tracer.enabled is False
    # Must not raise and must not attempt any network access.
    tracer.trace_turn(0, latency_s=0.1, usage={"input_tokens": 1, "output_tokens": 1})
    tracer.trace_tool(0, "some_tool", latency_s=0.05)


def test_run_tool_loop_invokes_injected_tracer_per_turn_and_tool(monkeypatch):
    claude = Claude(api_key="test-key-never-sent")
    calls = {"count": 0}

    def create(**kwargs):
        calls["count"] += 1
        return _FakeToolUseResponse() if calls["count"] == 1 else _FakeTextResponse("done")

    monkeypatch.setattr(claude._client.messages, "create", create)

    class _SpyTracer:
        def __init__(self) -> None:
            self.turns: list[int] = []
            self.tools: list[tuple[int, str]] = []

        def trace_turn(self, turn, *, latency_s, usage) -> None:
            self.turns.append(turn)

        def trace_tool(self, turn, tool_name, *, latency_s) -> None:
            self.tools.append((turn, tool_name))

    spy = _SpyTracer()
    claude.tracer = spy

    def dispatch(tool_name, tool_input):
        return ToolResult(tool_use_id="tu_1", content="ok")

    answer = claude.run_tool_loop(
        system="sys", messages=[{"role": "user", "content": "hi"}], tools=[], dispatch=dispatch
    )

    assert answer == "done"
    assert spy.turns == [0, 1]
    assert spy.tools == [(0, "some_tool")]
