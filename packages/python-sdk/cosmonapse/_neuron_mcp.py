"""
cosmonapse._neuron_mcp
~~~~~~~~~~~~~~~~~~~~~~~
Wrap **any stdio MCP server** as a Neuron.

This does *not* implement an MCP server. It is a thin client wrapper: it spawns
an existing MCP server as a subprocess, speaks the Model Context Protocol over
stdio, and exposes that server's tools behind the ``NeuronFn`` signature. A
TASK becomes an MCP ``tools/call`` and the tool result becomes the Neuron's
output.

Usage
-----
Wrap a published server by command::

    from cosmonapse import Axon, Neuron

    fs = Neuron(
        source="mcp",
        command="npx",
        args=["-y", "@modelcontextprotocol/server-filesystem", "/data"],
        tool="read_file",                # optional default tool
    )
    axon = Axon(neuron_id="files", neuron_fn=fs)

…or use a preset for a standard server (see ``STANDARD_MCP_SERVERS``)::

    fs = Neuron(source="mcp", server="filesystem", args=["/data"])
    web = Neuron(source="mcp", server="fetch")
    git = Neuron(source="mcp", server="git", args=["--repository", "."])

The preset only supplies the launch ``command``/``args`` for a well-known,
separately-published server; anything you pass in ``args`` is appended.

Input dict
----------
================  ===========================================================
key               meaning
================  ===========================================================
``tool``          Tool name to call. Falls back to the ``tool=`` constructor
                  arg, or  -  if the server exposes exactly one tool  -  that tool.
``arguments`` / ``args``  Tool arguments (dict). If omitted, every input key
                  except ``tool``/``arguments``/``args`` is treated as an
                  argument.
``__list_tools__``  When truthy, return the server's tool catalogue instead of
                  calling a tool.
================  ===========================================================

Output dict
-----------
``tools/call``::

    {
        "response": "<concatenated text content>",
        "result": <structured content> | None,
        "is_error": False,
        "content": [ ... raw content blocks ... ],
        "meta": {"tool": ..., "server": ...},
    }

``__list_tools__``::

    {"tools": [{"name": ..., "description": ..., "input_schema": {...}}, ...]}

Soft dependency
---------------
The official ``mcp`` package is required (``pip install mcp``). It is not a
core dependency so projects that don't use MCP neurons don't pull it in.
"""

from __future__ import annotations

import asyncio
from typing import Any

from cosmonapse._neuron_base import _BaseNeuron

# Launch specs for standard, separately-published MCP servers. We wrap them  - 
# we do not ship them. Anything supplied in the constructor `args` is appended
# to the preset args (e.g. allowed directories for filesystem, repo for git).
STANDARD_MCP_SERVERS: dict[str, dict[str, Any]] = {
    # Node-based reference servers (run via npx).
    "filesystem": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-filesystem"],
        "note": "Append one or more allowed directories, e.g. args=['/data'].",
    },
    "memory": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-memory"],
        "note": "Knowledge-graph memory store.",
    },
    "everything": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-everything"],
        "note": "Reference server exercising every MCP feature; handy for tests.",
    },
    "sequentialthinking": {
        "command": "npx",
        "args": ["-y", "@modelcontextprotocol/server-sequential-thinking"],
        "note": "Structured step-by-step reasoning tool.",
    },
    # Python-based reference servers (run via uvx).
    "fetch": {
        "command": "uvx",
        "args": ["mcp-server-fetch"],
        "note": "Fetch a URL and return its content as markdown/text.",
    },
    "git": {
        "command": "uvx",
        "args": ["mcp-server-git"],
        "note": "Read/inspect a git repo. Append --repository <path>.",
    },
    "time": {
        "command": "uvx",
        "args": ["mcp-server-time"],
        "note": "Current time and timezone conversions.",
    },
}

_CONTROL_KEYS = {"tool", "arguments", "args", "__list_tools__"}


def _require_mcp():
    try:
        import mcp  # noqa: F401
        return mcp
    except ImportError:
        raise ImportError(
            "The 'mcp' package is required for MCP-server Neuron wrappers.\n"
            "Install it with:  pip install mcp"
        ) from None


def _resolve_launch(
    command: str | None,
    args: list[str] | None,
    server: str | None,
) -> tuple[str, list[str]]:
    """Combine an explicit command / a named preset / extra args into a spec."""
    extra = list(args or [])

    if server is not None:
        preset = STANDARD_MCP_SERVERS.get(server)
        if preset is None:
            available = ", ".join(sorted(STANDARD_MCP_SERVERS))
            raise ValueError(
                f"Unknown MCP server preset {server!r}. Available: {available}. "
                f"(Or pass command=/args= to wrap any other stdio MCP server.)"
            )
        base_cmd = command or preset["command"]
        return base_cmd, [*preset["args"], *extra]

    if command is None:
        raise ValueError(
            "Neuron(source='mcp', ...) needs either `command` (+optional `args`) "
            "or a `server` preset name."
        )
    return command, extra


def _extract_call_result(result: Any) -> dict[str, Any]:
    """Normalise an MCP CallToolResult into a plain Neuron-output dict."""
    texts: list[str] = []
    content = getattr(result, "content", None) or []
    for item in content:
        text = getattr(item, "text", None)
        if text is not None:
            texts.append(text)

    structured = getattr(result, "structuredContent", None)
    is_error = bool(getattr(result, "isError", False))

    try:
        raw_content = [
            c.model_dump(mode="json") if hasattr(c, "model_dump") else c
            for c in content
        ]
    except Exception:
        raw_content = []

    return {
        "response": "\n".join(texts),
        "result": structured,
        "is_error": is_error,
        "content": raw_content,
    }


def _serialise_tools(result: Any) -> dict[str, Any]:
    tools = getattr(result, "tools", None) or []
    out = []
    for t in tools:
        out.append(
            {
                "name": getattr(t, "name", None),
                "description": getattr(t, "description", None),
                "input_schema": getattr(t, "inputSchema", None),
            }
        )
    return {"tools": out}


class _MCPNeuron(_BaseNeuron):
    """Expose a stdio MCP server's tools as a Neuron.

    A single long-lived background task owns the stdio connection and the MCP
    ``ClientSession``; requests are funnelled to it through a queue. This keeps
    every anyio cancel scope inside one task (the MCP stdio client requires the
    connection to be opened and closed from the same task), while letting many
    TASKs reuse the one spawned subprocess.
    """

    def __init__(
        self,
        command: str | None = None,
        args: list[str] | None = None,
        server: str | None = None,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        tool: str | None = None,
        client_name: str = "cosmonapse",
        client_version: str = "0.2.0",
    ) -> None:
        _require_mcp()  # fail fast with a clear message
        self.command, self.args = _resolve_launch(command, args, server)
        self.server = server
        self.env = env
        self.cwd = cwd
        self.tool = tool
        self.client_name = client_name
        self.client_version = client_version

        self._req_q: asyncio.Queue[Any] | None = None
        self._worker: asyncio.Task[Any] | None = None
        self._start_lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public NeuronFn entry point
    # ------------------------------------------------------------------

    async def __call__(self, input: dict[str, Any], context: list[Any]) -> dict[str, Any]:
        if input.get("__list_tools__"):
            result = await self._request("list", {})
            return _serialise_tools(result)

        tool = input.get("tool") or self.tool
        arguments = input.get("arguments")
        if arguments is None:
            arguments = input.get("args")
        if arguments is None:
            arguments = {k: v for k, v in input.items() if k not in _CONTROL_KEYS}

        if not tool:
            # No explicit tool: if the server exposes exactly one, use it.
            listing = _serialise_tools(await self._request("list", {}))
            names = [t["name"] for t in listing["tools"]]
            if len(names) == 1:
                tool = names[0]
            else:
                raise ValueError(
                    "MCP Neuron could not determine which tool to call. "
                    f"Pass tool=... (server exposes: {names})."
                )

        result = await self._request("call", {"tool": tool, "arguments": arguments})
        out = _extract_call_result(result)
        out["meta"] = {"tool": tool, "server": self.server, "command": self.command}
        return out

    # ------------------------------------------------------------------
    # Background session runner
    # ------------------------------------------------------------------

    async def _ensure_started(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        async with self._start_lock:
            if self._worker is not None and not self._worker.done():
                return
            loop = asyncio.get_event_loop()
            self._req_q = asyncio.Queue()
            started: asyncio.Future[None] = loop.create_future()
            self._worker = asyncio.create_task(self._run(started))
            await started  # propagates startup errors (e.g. command not found)

    async def _run(self, started: asyncio.Future[None]) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=self.command,
            args=self.args,
            env=self.env,
            cwd=self.cwd,
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    if not started.done():
                        started.set_result(None)
                    assert self._req_q is not None
                    while True:
                        item = await self._req_q.get()
                        if item is None:  # shutdown sentinel
                            break
                        fut, kind, payload = item
                        try:
                            if kind == "list":
                                res = await session.list_tools()
                            else:
                                res = await session.call_tool(
                                    payload["tool"], payload["arguments"]
                                )
                            if not fut.done():
                                fut.set_result(res)
                        except Exception as exc:  # noqa: BLE001
                            if not fut.done():
                                fut.set_exception(exc)
        except Exception as exc:  # noqa: BLE001  -  startup / transport failure
            if not started.done():
                started.set_exception(exc)
            else:
                # Fail any in-flight request so callers don't hang forever.
                self._drain_with_error(exc)

    def _drain_with_error(self, exc: BaseException) -> None:
        if self._req_q is None:
            return
        while not self._req_q.empty():
            item = self._req_q.get_nowait()
            if item is None:
                continue
            fut = item[0]
            if not fut.done():
                fut.set_exception(exc)

    async def _request(self, kind: str, payload: dict[str, Any]) -> Any:
        await self._ensure_started()
        assert self._req_q is not None
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        await self._req_q.put((fut, kind, payload))
        return await fut

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        worker = self._worker
        if worker is None:
            return
        if self._req_q is not None and not worker.done():
            await self._req_q.put(None)
        try:
            await asyncio.wait_for(asyncio.shield(worker), timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            worker.cancel()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._worker = None
            self._req_q = None
