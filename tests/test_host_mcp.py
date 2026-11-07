"""Real stdio MCP connector checks; no desktop or production configuration is used."""
import sys

import pytest

from assistant_kernel.host_client.adapters.mcp import McpQueryAdapter


@pytest.fixture
def adapter(tmp_path):
    server = tmp_path / "server.py"
    server.write_text('''from mcp.server.mcpserver import MCPServer
m = MCPServer("query-fixture")
@m.tool()
def echo(text: str) -> str:
    return text
@m.tool()
def broken() -> str:
    raise ValueError("fixture failure")
if __name__ == "__main__":
    m.run(transport="stdio")
''', encoding="utf-8")
    return McpQueryAdapter({"command": sys.executable, "args": [str(server)],
                            "query_tools": ["echo", "broken"], "timeout_seconds": 15})


def test_discover_and_call(adapter):
    found = adapter.run("mcp.discover")
    assert {t["name"] for t in found["tools"]} == {"echo", "broken"}
    result = adapter.run("mcp.query", {"tool": "echo", "arguments": {"text": "hello"}})
    assert result["result"]["content"][0]["text"] == "hello"


def test_unlisted_tool_rejected_before_launch(adapter):
    adapter.config["command"] = "must-not-be-launched"
    with pytest.raises(PermissionError):
        adapter.run("mcp.query", {"tool": "delete", "arguments": {}})


def test_invalid_arguments_and_server_error(adapter):
    def leaves(exc):
        children = getattr(exc, "exceptions", ())
        return [leaf for child in children for leaf in leaves(child)] if children else [exc]
    from jsonschema import ValidationError
    with pytest.raises(Exception) as invalid:
        adapter.run("mcp.query", {"tool": "echo", "arguments": {"text": 42}})
    assert any(isinstance(e, ValidationError) for e in leaves(invalid.value))
    with pytest.raises(Exception) as broken:
        adapter.run("mcp.query", {"tool": "broken", "arguments": {}})
    assert any(isinstance(e, RuntimeError) and "tool error" in str(e) for e in leaves(broken.value))


def test_timeout_bounds():
    with pytest.raises(ValueError):
        McpQueryAdapter({"command": "unused", "timeout_seconds": 60})


def test_persistent_state_isolation_and_close(tmp_path):
    from assistant_kernel.host_client.adapters.sessions import PersistentQueries
    server = tmp_path / "stateful.py"
    server.write_text('''from mcp.server.mcpserver import MCPServer
m = MCPServer("state-fixture")
count = 0
@m.tool()
def count_calls() -> str:
    global count
    count += 1
    return str(count)
if __name__ == "__main__":
    m.run(transport="stdio")
''', encoding="utf-8")
    adapter = PersistentQueries({"command": sys.executable, "args": [str(server)],
                                 "query_tools": ["count_calls"], "timeout_seconds": 15})
    def context(project, task="t1"):
        return {"project_id": project, "caller_identity": {"task_id": task}}
    def query(project, task="t1"):
        result = adapter.run_scoped("mcp.query", {"tool": "count_calls", "arguments": {}}, context(project, task))
        return result["result"]["content"][0]["text"]
    try:
        assert query("p1") == "1"
        assert query("p1") == "2"
        assert query("p2") == "1"
        assert query("p1", "t2") == "1"
        with pytest.raises(PermissionError):
            adapter.run_scoped("mcp.discover", {}, {})
        adapter.run_scoped("mcp.session.close", {}, context("p1"))
        assert query("p1") == "1"
    finally:
        adapter.close()
    assert not adapter.thread.is_alive()
    with pytest.raises(RuntimeError, match="closed"):
        query("p1")


def test_operation_journal_recovery(tmp_path):
    from assistant_kernel.host_client.journal import OperationJournal
    path = tmp_path / "operations.sqlite3"
    op = {"operation_id": "o1", "project_id": "p1", "action": "test", "arguments": {"x": 1}}
    journal = OperationJournal(path)
    assert journal.begin(op) is None
    assert OperationJournal(path).begin(op)["result"]["error"]["code"] == "EXECUTION_UNKNOWN"
    journal.finish(op, {"state": "succeeded", "result": {"ok": True}, "operation_token": "not-persisted"})
    assert OperationJournal(path).begin(op) == {"state": "succeeded", "result": {"ok": True}}
    with pytest.raises(ValueError, match="identity changed"):
        journal.begin({**op, "arguments": {"x": 2}})
    assert b"not-persisted" not in path.read_bytes()


def test_failed_scope_survives_idle_cleanup(adapter):
    import asyncio
    from assistant_kernel.host_client.adapters.sessions import PersistentQueries
    manager = PersistentQueries(adapter.config)
    async def check():
        task = asyncio.create_task(asyncio.sleep(0))
        await task
        manager.sessions[("p1", "t1")] = {"queue": asyncio.Queue(), "last": 0,
                                              "failed": True, "task": task}
        await manager._dispatch(("p2", "t2"), "mcp.discover", {})
        assert ("p1", "t1") in manager.sessions
        with pytest.raises(RuntimeError, match="session lost"):
            await manager._dispatch(("p1", "t1"), "mcp.discover", {})
    try:
        asyncio.run_coroutine_threadsafe(check(), manager.loop).result(45)
    finally:
        manager.close()


def test_artifact_scope_size_and_integrity(tmp_path):
    from assistant_kernel.host_client.artifacts import ArtifactStore
    store = ArtifactStore(tmp_path / "artifacts", max_bytes=8)
    context = {"project_id": "p1", "caller_identity": {"task_id": "t1"}}
    ref = store.stage(b"example", context)
    assert store.read(ref["artifact_id"], context) == b"example"
    assert "path" not in ref
    with pytest.raises(PermissionError):
        store.read(ref["artifact_id"], {"project_id": "p2", "run_id": "r1"})
    with pytest.raises(ValueError):
        store.stage(b"oversized", context)
    with pytest.raises(ValueError):
        store.read("../outside", context)
    (store.root / ref["artifact_id"] / "content").write_bytes(b"changed")
    with pytest.raises(ValueError, match="integrity"):
        store.read(ref["artifact_id"], context)


def test_artifact_http_upload_and_report(tmp_path, monkeypatch):
    import hashlib
    import json
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer
    from assistant_kernel.host_client.artifacts import ArtifactStore
    from assistant_kernel.host_client import runner
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            content = self.rfile.read(int(self.headers['Content-Length']))
            assert self.headers['Authorization'] == 'Bearer device'
            received.append((self.path, content))
            if self.path.endswith('/artifact'):
                assert self.headers['X-Operation-Token'] == 'op-secret'
                response = {'operation_id': 'o1', 'project_id': 'p1', 'size': len(content),
                            'sha256': hashlib.sha256(content).hexdigest(), 'path': 'runs/host-artifacts/o1/file'}
            else:
                response = {'state': 'succeeded'}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
    server = HTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    op = {'operation_id': 'o1', 'project_id': 'p1', 'run_id': 'r1', 'operation_token': 'op-secret'}
    store = ArtifactStore(tmp_path / 'artifacts')
    ref = store.stage(b'fixture', op)
    pending = tmp_path / 'pending.json'
    op['body'] = {'state': 'succeeded', 'result': {'local_artifact_refs': [ref]}}
    runner.save_pending(pending, op)
    try:
        runner.report_pending(f'http://127.0.0.1:{server.server_port}', 'device', op, pending)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert not pending.exists()
    assert received[0][1] == b'fixture'
    result = json.loads(received[1][1])['result']
    assert 'local_artifact_refs' not in result
    assert result['artifact_refs'][0]['sha256'] == hashlib.sha256(b'fixture').hexdigest()
