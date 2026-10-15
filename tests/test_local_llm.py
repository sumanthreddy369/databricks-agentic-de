"""agent/local_llm.py: the planner on this machine (Ollama). It must refuse
any address that isn't this machine, and speak the same tool-calling loop."""

import json

import httpx
import pytest

from agent.llm import ToolResult
from agent.local_llm import LocalModel, local_embedder, require_local


@pytest.mark.parametrize("url", ["http://localhost:11434", "http://127.0.0.1:11434/", "http://[::1]:11434"])
def test_this_machine_is_allowed(url):
    assert require_local(url).startswith("http://")


@pytest.mark.parametrize(
    "url",
    ["https://api.example.com", "http://192.168.1.20:11434", "http://localhost.evil.com:11434", "http://10.0.0.5"],
)
def test_anything_else_is_refused(url):
    with pytest.raises(ValueError):
        LocalModel(base_url=url)
    with pytest.raises(ValueError):
        local_embedder(base_url=url)


def test_tool_loop_against_ollama_shape():
    seen = []
    replies = iter(
        [
            {
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": "",
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "check_job_status", "arguments": "{}"},
                                }
                            ],
                        },
                    }
                ]
            },
            {
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "<think>checking</think>All healthy."},
                    }
                ]
            },
        ]
    )

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization"), json.loads(request.content)))
        return httpx.Response(200, json=next(replies))

    model = LocalModel("qwen3:14b", transport=httpx.MockTransport(handler))
    answer = model.run_tool_loop("s", [{"role": "user", "content": "q"}], [], lambda n, a: ToolResult("x", "{}"))
    assert answer == "All healthy."  # thinking stripped
    url, auth, body = seen[0]
    assert url == "http://localhost:11434/v1/chat/completions"
    assert auth is None  # no credentials of any kind
    assert body["model"] == "qwen3:14b"


def test_local_embeddings_call_ollama_on_this_machine():
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "http://localhost:11434/api/embed"
        texts = json.loads(request.content)["input"]
        return httpx.Response(200, json={"embeddings": [[1.0, 0.0]] * len(texts)})

    assert local_embedder(transport=httpx.MockTransport(handler))(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]
