"""Thin wrapper around the Anthropic SDK — no agent framework, no LangChain.

Deliberately just a plain class with a tool-calling loop, mirroring the
`abhay` sibling project's core/model_router.py shape (a small dataclass +
direct SDK calls). `Claude` is injected into `OrchestratorAgent` rather than
constructed globally, so tests can substitute a scripted fake object with the
same `run_tool_loop` signature and make zero real network calls.
"""

from collections.abc import Callable
from dataclasses import dataclass

import anthropic


@dataclass
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


class Claude:
    def __init__(self, api_key: str | None = None, model: str = "claude-sonnet-5") -> None:
        self._client = anthropic.Anthropic(api_key=api_key)
        self.model = model

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
        for _ in range(max_turns):
            response = self._client.messages.create(
                model=self.model, max_tokens=1024, system=system, messages=messages, tools=tools
            )
            messages.append({"role": "assistant", "content": response.content})
            if response.stop_reason != "tool_use":
                return "".join(b.text for b in response.content if b.type == "text")
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = dispatch(block.name, block.input)
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
