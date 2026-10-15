"""agent/databricks_llm.py: a Databricks-hosted model behind the same
run_tool_loop interface as Claude, driven here by a fake endpoint (no
network)."""

import json

import httpx
import pytest

from agent.databricks_llm import DatabricksModel, openai_tools
from agent.llm import TRUNCATED_ANSWER, ToolResult

TOOLS = [
    {
        "name": "check_job_status",
        "description": "Status of a pipeline",
        "input_schema": {"type": "object", "properties": {"job": {"type": "string"}}},
    }
]


class _Config:
    host = "https://example.cloud.databricks.com/"

    def authenticate(self):
        return {"Authorization": "Bearer test-token"}


def _model(replies, seen):
    replies = iter(replies)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)))
        status, body = next(replies)
        return httpx.Response(status, json=body)

    return DatabricksModel("databricks-test", config=_Config(), transport=httpx.MockTransport(handler))


def _tool_call(name, arguments, call_id="call_1"):
    message = {
        "role": "assistant",
        "content": None,
        "tool_calls": [{"id": call_id, "type": "function", "function": {"name": name, "arguments": arguments}}],
    }
    return 200, {"choices": [{"message": message, "finish_reason": "tool_calls"}]}


def _answer(text, finish="stop"):
    return 200, {"choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": finish}]}


def test_tools_are_offered_as_functions_and_results_fed_back():
    seen, dispatched = [], []
    model = _model([_tool_call("check_job_status", '{"job": "streaming"}'), _answer("All healthy.")], seen)

    def dispatch(name, args):
        dispatched.append((name, args))
        return ToolResult("x", '{"status": "RUNNING"}')

    messages = [{"role": "user", "content": "Health check."}]
    assert model.run_tool_loop("system prompt", messages, TOOLS, dispatch) == "All healthy."
    assert dispatched == [("check_job_status", {"job": "streaming"})]
    url, auth, first = seen[0]
    assert url == "https://example.cloud.databricks.com/serving-endpoints/databricks-test/invocations"
    assert auth == "Bearer test-token"
    assert first["messages"][0] == {"role": "system", "content": "system prompt"}
    assert first["tools"] == openai_tools(TOOLS)
    assert seen[1][2]["messages"][-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": '{"status": "RUNNING"}',
    }
    assert messages[-1] == {"role": "assistant", "content": "All healthy."}


def test_bad_tool_arguments_go_back_to_the_model_not_to_the_tool():
    seen, dispatched = [], []
    model = _model([_tool_call("check_job_status", "{not json"), _answer("Sorry.")], seen)
    model.run_tool_loop("s", [{"role": "user", "content": "q"}], TOOLS, lambda n, a: dispatched.append(n))
    assert dispatched == []
    assert "not valid JSON" in seen[1][2]["messages"][-1]["content"]


def test_cut_off_answer_is_not_presented_as_finished():
    model = _model([_answer("The pipeline is", finish="length")], [])
    assert model.run_tool_loop("s", [{"role": "user", "content": "q"}], [], lambda n, a: None) == TRUNCATED_ANSWER


def test_reasoning_blocks_are_not_part_of_the_answer():
    content = [{"type": "reasoning", "summary": "thinking"}, {"type": "text", "text": "Done."}]
    model = _model([_answer(content)], [])
    assert model.run_tool_loop("s", [{"role": "user", "content": "q"}], [], lambda n, a: None) == "Done."


def test_rate_limit_is_retried_and_a_bad_request_is_not(monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)
    seen = []
    model = _model([(429, {}), _answer("ok")], seen)
    assert model.run_tool_loop("s", [{"role": "user", "content": "q"}], [], lambda n, a: None) == "ok"
    assert len(seen) == 2
    model = _model([(400, {"message": "bad parameter"})], [])
    with pytest.raises(RuntimeError, match="HTTP 400"):
        model.run_tool_loop("s", [{"role": "user", "content": "q"}], [], lambda n, a: None)


def test_max_turns_ends_the_loop():
    model = _model([_tool_call("check_job_status", "{}")] * 2, [])
    answer = model.run_tool_loop(
        "s", [{"role": "user", "content": "q"}], TOOLS, lambda n, a: ToolResult("x", "{}"), 2
    )
    assert answer.startswith("Gave up")


def test_objects_sent_as_json_strings_are_decoded_where_the_schema_says_object():
    """Seen live (Qwen 3.5): `filters` as a JSON-encoded string, repeated
    after the tool's error until the turn limit."""
    from agent.databricks_llm import coerce_arguments

    schema = {
        "properties": {"filters": {"type": "object"}, "group_by": {"type": "array"}, "table": {"type": "string"}}
    }
    args = {"filters": '{"unit": "ICU"}', "group_by": '["unit"]', "table": '{"not": "decoded"}'}
    assert coerce_arguments(args, schema) == {
        "filters": {"unit": "ICU"},
        "group_by": ["unit"],
        "table": '{"not": "decoded"}',
    }
    # Not the declared type, or not JSON: left for the tool to reject.
    assert coerce_arguments({"filters": '["unit"]'}, schema) == {"filters": '["unit"]'}
    assert coerce_arguments({"filters": "unit = 'ICU'"}, schema) == {"filters": "unit = 'ICU'"}


def test_malformed_model_output_is_retried_as_the_endpoint_advises(monkeypatch):
    monkeypatch.setattr("tenacity.nap.time.sleep", lambda s: None)
    seen = []
    bad = (400, {"error_code": "BAD_REQUEST", "message": "Model response did not respect the required format."})
    model = _model([bad, _answer("ok")], seen)
    assert model.run_tool_loop("s", [{"role": "user", "content": "q"}], [], lambda n, a: None) == "ok"
    assert len(seen) == 2
