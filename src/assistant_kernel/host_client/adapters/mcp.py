"""Explicitly configured, short-lived stdio MCP query adapter.

Stateful browser actions require a persistent session backend and are not enabled here.
Tool annotations never grant permission; the maintainer supplies the query allowlist.
"""
from __future__ import annotations

import asyncio
import json
from contextlib import AsyncExitStack

from jsonschema import Draft202012Validator


class McpQueryAdapter:
    revision = "1"

    def __init__(self, config: dict):
        self.config = config
        if not isinstance(config.get("command"), str) or not config["command"]:
            raise ValueError("MCP command is required")
        self.tools = frozenset(config.get("query_tools", []))
        self.timeout = float(config.get("timeout_seconds", 20))
        if not 0 < self.timeout <= 30:
            raise ValueError("MCP timeout must be between 0 and 30 seconds")

    async def query(self, action: str, arguments: dict) -> dict:
        from mcp import ClientSession, StdioServerParameters, types
        from mcp.client.stdio import stdio_client

        if action not in ("mcp.discover", "mcp.query"):
            raise ValueError("unsupported MCP action")
        if action == "mcp.discover" and arguments:
            raise ValueError("discovery takes no arguments")
        if action == "mcp.query":
            if set(arguments) != {"tool", "arguments"} or arguments["tool"] not in self.tools:
                raise PermissionError("tool is not in the local query allowlist")
            if not isinstance(arguments["arguments"], dict):
                raise ValueError("tool arguments must be an object")
        server = StdioServerParameters(command=self.config["command"],
            args=self.config.get("args", []), env=self.config.get("env"), cwd=self.config.get("cwd"))
        # Keep enter/exit on the same task: MCP transports use task-local cancel scopes.
        async with AsyncExitStack() as stack:
            read, write = await stack.enter_async_context(stdio_client(server))
            session = await stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            return await self.request(session, action, arguments)

    async def request(self, session, action, arguments):
        from mcp import types
        found = {}
        cursor = None
        seen_cursors = set()
        pages = 0
        while True:
            pages += 1
            if pages > 32:
                raise ValueError("MCP discovery exceeds page limit")
            page = await session.list_tools(params=types.PaginatedRequestParams(cursor=cursor) if cursor else None)
            for tool in page.tools:
                if tool.name in self.tools:
                    found[tool.name] = tool
            cursor = page.next_cursor
            if not cursor:
                break
            if cursor in seen_cursors:
                raise ValueError("MCP discovery repeated a cursor")
            seen_cursors.add(cursor)
            if len(found) > 256:
                raise ValueError("MCP tool inventory exceeds limit")
        if action == "mcp.discover":
            return {"tools": [{"name": t.name, "input_schema": t.input_schema}
                              for t in found.values()], "scope": "configured queries only"}
        tool = found.get(arguments["tool"])
        if tool is None:
            raise ValueError("configured tool is not available on the server")
        Draft202012Validator(tool.input_schema).validate(arguments["arguments"])
        result = await session.call_tool(tool.name, arguments["arguments"])
        if result.is_error:
            raise RuntimeError("MCP server reported a tool error")
        payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        if len(json.dumps(payload).encode()) > 65536:
            raise ValueError("MCP result exceeds inline limit; artifact transport required")
        # Images/files require the artifact pipeline; never silently discard them.
        if any(item.get("type") != "text" for item in payload.get("content", [])):
            raise ValueError("non-text MCP results require artifact transport")
        return {"tool": tool.name, "result": payload}

    def run(self, action: str, arguments: dict | None = None) -> dict:
        async def bounded():
            return await asyncio.wait_for(self.query(action, arguments or {}), timeout=self.timeout)
        return asyncio.run(bounded())
