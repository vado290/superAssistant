"""Project/task-owned persistent MCP query sessions; no browser side effects enabled."""
from __future__ import annotations

import asyncio
import threading
import time
from concurrent.futures import TimeoutError as FutureTimeout

from assistant_kernel.host_client.adapters.mcp import McpQueryAdapter


class PersistentQueries(McpQueryAdapter):
    """One transport-owning task per scope, on a dedicated event loop."""

    def __init__(self, config):
        super().__init__(config)
        self.limit = int(config.get("max_sessions", 4))
        self.idle = float(config.get("idle_seconds", 300))
        if not 1 <= self.limit <= 16 or not 1 <= self.idle <= 3600:
            raise ValueError("invalid session limits")
        self.sessions = {}
        self.closed = False
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self.thread.start()

    async def _owner(self, entry):
        from contextlib import AsyncExitStack
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        active = None
        try:
            server = StdioServerParameters(command=self.config["command"], args=self.config.get("args", []),
                                           env=self.config.get("env"), cwd=self.config.get("cwd"))
            async with AsyncExitStack() as stack:
                read, write = await stack.enter_async_context(stdio_client(server))
                session = await stack.enter_async_context(ClientSession(read, write))
                await asyncio.wait_for(session.initialize(), self.timeout)
                while True:
                    active = await entry["queue"].get()
                    if active is None:
                        break
                    action, arguments, future = active
                    if future.cancelled():
                        continue
                    try:
                        result = await asyncio.wait_for(self.request(session, action, arguments), self.timeout)
                        if not future.done():
                            future.set_result(result)
                    except Exception as exc:
                        if not future.done():
                            future.set_exception(exc)
                        # Discard a potentially desynchronised transport, not its scope identity.
                        break
                    finally:
                        active = None
        finally:
            entry["failed"] = True
            pending = [] if active is None else [active]
            while not entry["queue"].empty():
                item = entry["queue"].get_nowait()
                if item is not None:
                    pending.append(item)
            for _, _, future in pending:
                if not future.done():
                    future.set_exception(RuntimeError("MCP session lost; explicitly close the scope before reopening"))

    async def _close_scope(self, key):
        entry = self.sessions.pop(key, None)
        if entry:
            entry["task"].cancel()
            await asyncio.gather(entry["task"], return_exceptions=True)

    async def _dispatch(self, key, action, arguments):
        now = time.monotonic()
        for old, entry in list(self.sessions.items()):
            if old != key and not entry["failed"] and not entry["task"].done() and now - entry["last"] > self.idle:
                await self._close_scope(old)
        if key not in self.sessions:
            if len(self.sessions) >= self.limit:
                raise RuntimeError("MCP session limit reached")
            entry = {"queue": asyncio.Queue(), "last": now, "failed": False}
            entry["task"] = asyncio.create_task(self._owner(entry))
            # Consume task errors; callers receive a stable session-lost result.
            entry["task"].add_done_callback(lambda task: None if task.cancelled() else task.exception())
            self.sessions[key] = entry
        entry = self.sessions[key]
        if entry["failed"] or entry["task"].done():
            raise RuntimeError("MCP session lost; explicitly close the scope before reopening")
        entry["last"] = now
        future = self.loop.create_future()
        await entry["queue"].put((action, arguments, future))
        try:
            return await asyncio.wait_for(future, self.timeout * 2)
        except asyncio.TimeoutError:
            entry["failed"] = True
            entry["task"].cancel()
            raise

    def run_scoped(self, action, arguments, context):
        if self.closed:
            raise RuntimeError("MCP session manager is closed")
        project = context.get("project_id")
        identity = context.get("caller_identity") or {}
        owner = identity.get("task_id") or context.get("run_id")
        if not project or not owner:
            raise PermissionError("trusted project and task/run scope required")
        key = (project, owner)
        if action == "mcp.session.close":
            if arguments:
                raise ValueError("session close takes no arguments")
            work = self._close_scope(key)
        else:
            if action not in ("mcp.discover", "mcp.query"):
                raise ValueError("unsupported MCP action")
            if action == "mcp.discover" and arguments:
                raise ValueError("discovery takes no arguments")
            if action == "mcp.query" and (set(arguments) != {"tool", "arguments"}
                    or arguments["tool"] not in self.tools or not isinstance(arguments["arguments"], dict)):
                raise PermissionError("invalid or unauthorized query")
            work = self._dispatch(key, action, arguments)
        future = asyncio.run_coroutine_threadsafe(work, self.loop)
        try:
            return future.result(self.timeout * 3)
        except FutureTimeout:
            future.cancel()
            raise TimeoutError("MCP session operation timed out") from None

    def close(self):
        if self.closed:
            return
        self.closed = True
        async def shutdown():
            for key in list(self.sessions):
                await self._close_scope(key)
        future = asyncio.run_coroutine_threadsafe(shutdown(), self.loop)
        try:
            future.result(self.timeout * 3)
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.thread.join(self.timeout)
            if not self.thread.is_alive():
                self.loop.close()
