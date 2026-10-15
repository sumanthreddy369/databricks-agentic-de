"""A Databricks-hosted model as the agent's planner: the same
`run_tool_loop` interface as agent.llm.Claude, so OrchestratorAgent, the
guardrails and the evals don't change.

Calls a Model Serving endpoint (Foundation Model APIs, e.g.
`databricks-meta-llama-3-3-70b-instruct`) through its OpenAI-compatible chat
API: Anthropic-style tool definitions are converted to function tools, and
tool results go back as `tool` messages. On Databricks Free Edition these
endpoints are available without a paid key.

Auth comes from databricks-sdk's Config: a CLI profile (`profile="free"`),
the DATABRICKS_* environment, or a job's runtime credentials. The token is
only ever put in a request header.
"""

import json
import time
from collections.abc import Callable

import httpx
import tenacity

from agent.llm import TRUNCATED_ANSWER, ToolResult, Tracer

DEFAULT_ENDPOINT = "databricks-qwen35-122b-a10b"  # 14/14 scenario evals, 3 runs each (docs/results.md)
DEFAULT_TIMEOUT_SECONDS = 120.0
MAX_TOKENS = 4096


class _Retryable(Exception):
    pass


def openai_tools(tools: list[dict]) -> list[dict]:
    """Anthropic tool definitions -> OpenAI function tools."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object", "properties": {}},
            },
        }
        for t in tools
    ]


def _text(content) -> str:
    """Message content as text: a string, or (some models, e.g. gpt-oss) a
    list of blocks of which only the `text` ones are the answer."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")


def coerce_arguments(arguments: dict, schema: dict | None) -> dict:
    """Some hosted models (seen live with Qwen 3.5) send a nested object or
    array as a JSON-encoded string - `"filters": "{\"unit\": \"ICU\"}"` - and
    repeat it after the tool's error. Where the tool's schema says the
    property is an object or array and the string decodes to exactly that,
    pass the decoded value; anything else is left for the tool to reject."""
    properties = (schema or {}).get("properties", {})
    out = dict(arguments)
    for key, value in arguments.items():
        expected = properties.get(key, {}).get("type")
        if expected not in ("object", "array") or not isinstance(value, str):
            continue
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict if expected == "object" else list):
            out[key] = decoded
    return out


class DatabricksModel:
    def __init__(
        self,
        endpoint: str = DEFAULT_ENDPOINT,
        *,
        profile: str | None = None,
        config=None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.BaseTransport | None = None,
        tracer: Tracer | None = None,
    ) -> None:
        if config is None:
            from databricks.sdk.core import Config

            config = Config(profile=profile) if profile else Config()
        self._config = config
        self.model = endpoint
        self.timeout = timeout
        self._http = httpx.Client(timeout=timeout, transport=transport)
        self.tracer = tracer if tracer is not None else Tracer()

    @tenacity.retry(
        retry=tenacity.retry_if_exception_type((_Retryable, httpx.TransportError)),
        # Free Edition rate-limits bursts (429s for over a minute seen during
        # evaluations), so back off for up to a minute before giving up.
        stop=tenacity.stop_after_attempt(6),
        wait=tenacity.wait_exponential(multiplier=2, max=60),
        reraise=True,
    )
    def _chat(self, messages: list[dict], tools: list[dict]) -> dict:
        body = {"messages": messages, "max_tokens": MAX_TOKENS, "temperature": 0}
        if tools:
            body["tools"] = tools
        response = self._http.post(
            f"{self._config.host.rstrip('/')}/serving-endpoints/{self.model}/invocations",
            headers=self._config.authenticate(),
            json=body,
        )
        if response.status_code in (429, 500, 502, 503, 504):
            raise _Retryable(f"{response.status_code}")
        if response.status_code == 400 and "did not respect the required format" in response.text:
            # The endpoint's own advice for a malformed tool call (seen live with
            # Llama 3.3): retry. Other 400s are real request errors.
            raise _Retryable("400 malformed model output")
        if response.status_code != 200:
            # The body says why (e.g. an unsupported parameter); never the request.
            raise RuntimeError(
                f"serving endpoint {self.model}: HTTP {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    def run_tool_loop(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        dispatch: Callable[[str, dict], ToolResult],
        max_turns: int = 6,
    ) -> str:
        chat = [{"role": "system", "content": system}] + [
            {
                "role": m["role"],
                "content": m["content"] if isinstance(m["content"], str) else json.dumps(m["content"]),
            }
            for m in messages
        ]
        function_tools = openai_tools(tools)
        schemas = {t["name"]: t.get("input_schema") for t in tools}
        for turn in range(max_turns):
            start = time.monotonic()
            payload = self._chat(chat, function_tools)
            self.tracer.trace_turn(turn, latency_s=time.monotonic() - start, usage=payload.get("usage"))
            choice = payload["choices"][0]
            message = choice["message"]
            calls = message.get("tool_calls") or []
            if not calls:
                if choice.get("finish_reason") == "length":
                    return TRUNCATED_ANSWER
                answer = _text(message.get("content"))
                messages.append({"role": "assistant", "content": answer})
                return answer
            chat.append({"role": "assistant", "content": _text(message.get("content")), "tool_calls": calls})
            for call in calls:
                name = call["function"]["name"]
                try:
                    arguments = json.loads(call["function"].get("arguments") or "{}")
                except json.JSONDecodeError:
                    result = ToolResult(call["id"], "error: tool arguments were not valid JSON", is_error=True)
                else:
                    tool_start = time.monotonic()
                    arguments = (
                        coerce_arguments(arguments, schemas.get(name)) if isinstance(arguments, dict) else {}
                    )
                    result = dispatch(name, arguments)
                    self.tracer.trace_tool(turn, name, latency_s=time.monotonic() - tool_start)
                chat.append({"role": "tool", "tool_call_id": call["id"], "content": result.content})
        return "Gave up after max_turns without a final answer."
