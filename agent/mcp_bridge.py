"""MCP client bridge: lets `agent/orchestrator.py` dispatch DE-mode tool
calls through the real MCP protocol (`mcp_server/server.py`) instead of
calling the Python tool functions directly in-process.

`MCPToolBridge` launches `mcp_server/server.py` as a subprocess over the
standard MCP stdio transport, speaks the real MCP client protocol to it (tool
discovery + tool calls), and exposes two things the orchestrator needs:

- `list_anthropic_tools()`: MCP tool schemas (name/description/input_schema)
  converted into the JSON-schema shape Anthropic's `tools=[...]` parameter
  expects. Not currently used to build `agent.orchestrator.DE_TOOLS` (those
  stay hand-written, matching the DA-mode tool list's existing style) but
  available so a caller — or a test — can prove the MCP server's tool
  schemas line up with what's hand-written there.
- `dispatch(tool_name, tool_input) -> ToolResult`: the same
  `Callable[[str, dict], ToolResult]` shape `OrchestratorAgent._dispatch`
  already uses for in-process calls, so routing through MCP is a drop-in
  swap at the call site, not a rewrite of the dispatch wrapper.

Why a persistent background event loop, not "spawn fresh per call": the MCP
server subprocess re-imports anthropic/structlog/onnxruntime/the mcp SDK
itself on every start (~3-4s cold, dominated by those packages' own import
time, not this project's code) — spawning a brand-new subprocess for every
single tool call would make a test suite exercising dozens of DE-mode
dispatches unusably slow. Instead, one subprocess + one MCP session is
started lazily on first use and kept alive on a dedicated background thread
running its own asyncio event loop; every `dispatch()`/`list_anthropic_tools()`
call after that first one is just a fast local stdio round trip to an
already-running process. `close()` tears the subprocess/thread/loop down
cleanly; `get_default_bridge()` below is a process-wide singleton (see its
own docstring) so the whole test suite pays that one-time startup cost once,
not once per `OrchestratorAgent()`.
"""

import asyncio
import atexit
import sys
import threading
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from agent.llm import ToolResult

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERVER_MODULE = "mcp_server.server"
_START_TIMEOUT_SECONDS = 30.0
_CALL_TIMEOUT_SECONDS = 60.0


class MCPToolBridge:
    def __init__(
        self,
        *,
        command: str | None = None,
        args: list[str] | None = None,
        cwd: Path | str | None = None,
    ) -> None:
        # Launching the SAME interpreter running this process (sys.executable)
        # guarantees the subprocess has the identical environment/dependency
        # set as the caller — no separate "which python is on PATH" question.
        self.command = command or sys.executable
        self.args = args if args is not None else ["-m", DEFAULT_SERVER_MODULE]
        # Defaults to the repo root so `python -m mcp_server.server` resolves
        # `mcp_server`/`agent`/`common`/`ml` as top-level packages the same
        # way running pytest from the repo root does.
        self.cwd = str(cwd) if cwd is not None else str(REPO_ROOT)

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session: ClientSession | None = None
        self._exit_stack_close: object | None = None
        self._start_lock = threading.Lock()

    def _server_params(self) -> StdioServerParameters:
        return StdioServerParameters(command=self.command, args=self.args, cwd=self.cwd)

    # --- lifecycle -----------------------------------------------------

    def _ensure_started(self) -> None:
        if self._session is not None:
            return
        with self._start_lock:
            if self._session is not None:
                return
            loop = asyncio.new_event_loop()
            thread = threading.Thread(target=loop.run_forever, name="mcp-tool-bridge", daemon=True)
            thread.start()
            future = asyncio.run_coroutine_threadsafe(self._start_session(), loop)
            try:
                self._session = future.result(timeout=_START_TIMEOUT_SECONDS)
            except BaseException:
                loop.call_soon_threadsafe(loop.stop)
                thread.join(timeout=5)
                loop.close()
                raise
            self._loop = loop
            self._thread = thread

    async def _start_session(self) -> ClientSession:
        from contextlib import AsyncExitStack

        stack = AsyncExitStack()
        read, write = await stack.enter_async_context(stdio_client(self._server_params()))
        session = await stack.enter_async_context(ClientSession(read, write))
        await session.initialize()
        # Stashed so close() can unwind the same stack (closes the session,
        # then the stdio transport, then lets the subprocess exit on EOF).
        self._exit_stack_close = stack.aclose
        return session

    def close(self) -> None:
        """Tears down the MCP session, stdio transport, background thread,
        and event loop. Safe to call multiple times and safe to call even if
        `dispatch`/`list_anthropic_tools` was never invoked (nothing to
        start started, so nothing to close).
        """
        if self._loop is None:
            return
        loop, thread = self._loop, self._thread
        self._loop = None
        self._thread = None
        self._session = None

        if self._exit_stack_close is not None:
            future = asyncio.run_coroutine_threadsafe(self._exit_stack_close(), loop)
            try:
                future.result(timeout=10)
            except Exception:
                pass  # best-effort teardown — never let a close() failure propagate
            self._exit_stack_close = None

        loop.call_soon_threadsafe(loop.stop)
        if thread is not None:
            thread.join(timeout=5)
        loop.close()

    # --- public API ------------------------------------------------------

    def list_anthropic_tools(self) -> list[dict]:
        """Discovers the MCP server's tools and returns them converted into
        Anthropic `tools=[...]` JSON-schema entries."""
        self._ensure_started()
        future = asyncio.run_coroutine_threadsafe(self._session.list_tools(), self._loop)
        listed = future.result(timeout=_CALL_TIMEOUT_SECONDS)
        return [_mcp_tool_to_anthropic_schema(tool) for tool in listed.tools]

    def dispatch(self, tool_name: str, tool_input: dict) -> ToolResult:
        """Calls `tool_name` on the MCP server with `tool_input` and returns
        the SAME `ToolResult(content, is_error)` shape an in-process call to
        the underlying tool function would have returned — see
        `mcp_server/server.py:_to_call_tool_result` for the matching
        server-side conversion this round-trips against.
        """
        self._ensure_started()
        future = asyncio.run_coroutine_threadsafe(self._session.call_tool(tool_name, tool_input), self._loop)
        result = future.result(timeout=_CALL_TIMEOUT_SECONDS)
        text = "".join(block.text for block in result.content if getattr(block, "type", None) == "text")
        return ToolResult(tool_use_id="", content=text, is_error=bool(result.is_error))


def _mcp_tool_to_anthropic_schema(tool) -> dict:
    return {
        "name": tool.name,
        "description": tool.description or "",
        "input_schema": tool.input_schema,
    }


# --- process-wide default bridge -----------------------------------------
# `agent.orchestrator.OrchestratorAgent` defaults to this shared singleton
# (rather than constructing a fresh `MCPToolBridge` per instance) so the
# ~3-4s one-time subprocess-startup cost described in this module's docstring
# is paid once per process (e.g. once for the whole pytest run), not once per
# `OrchestratorAgent()` — the bridge itself is a stateless RPC layer; all the
# actual per-test state lives in the JSON state files each test points it at
# via `state_path`, so sharing one MCP session across orchestrator instances
# is safe.

_default_bridge: MCPToolBridge | None = None
_default_bridge_lock = threading.Lock()


def get_default_bridge() -> MCPToolBridge:
    global _default_bridge
    if _default_bridge is None:
        with _default_bridge_lock:
            if _default_bridge is None:
                _default_bridge = MCPToolBridge()
    return _default_bridge


def close_default_bridge() -> None:
    """Closes and clears the process-wide default bridge, if one was ever
    started. Registered via `atexit` below; also callable directly (e.g. by
    a test session-scoped fixture that wants a clean teardown rather than
    relying on interpreter exit).
    """
    global _default_bridge
    with _default_bridge_lock:
        if _default_bridge is not None:
            _default_bridge.close()
            _default_bridge = None


atexit.register(close_default_bridge)
