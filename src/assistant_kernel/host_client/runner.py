"""PC executor (A batch): long-poll Kernel for read-only operations, run the adapter, report the result.

Run in the logged-in user session:  python -m assistant_kernel.host_client.runner config.json
config.json: {"kernel_url": "https://host:port", "device_id": "pc1", "token_file": "token.txt",
              "obs": {"host": "127.0.0.1", "port": 4455, "password_file": "obs-password.txt"}}
Only the fixed read actions of the adapters are executed; nothing else is accepted from the server.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from pathlib import Path

from assistant_kernel.host_client.adapters.obs import ObsAdapter


def _post(url: str, token: str, body: dict | None = None, timeout: int = 30) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as response:   # TLS is verified by default
        return json.loads(response.read())


def handle(adapters: dict, action: str, arguments: dict | None = None, revision: str = "1", context: dict | None = None) -> tuple[str, dict]:
    name = action.split(".", 1)[0]
    adapter = adapters.get(name)
    try:
        if adapter is None:
            return "failed", {"error": {"code": "CAPABILITY_UNAVAILABLE", "message": f"no adapter for {action}"}}
        if revision != getattr(adapter, "revision", "1"):
            raise ValueError("unsupported capability revision")
        if hasattr(adapter, "run_scoped"):
            return "succeeded", adapter.run_scoped(action, arguments or {}, context or {})
        return "succeeded", adapter.run(action, arguments) if arguments else adapter.run(action)
    except Exception as exc:   # noqa: BLE001 - reported to Kernel as a failed operation
        return "failed", {"error": {"code": "EXECUTION_FAILED", "message": str(exc)[:300]}}


def upload_artifact(root: str, token: str, operation: dict, store, reference: dict) -> dict:
    """Upload a scoped local artifact; return a verified project-relative reference."""
    import hashlib
    content = store.read(reference["artifact_id"], operation)
    digest = hashlib.sha256(content).hexdigest()
    req = urllib.request.Request(
        f"{root}/operations/{operation['operation_id']}/artifact", data=content, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/octet-stream",
                 "X-Operation-Token": operation["operation_token"], "X-Content-SHA256": digest})
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.loads(response.read(65536))
    if (result.get("sha256") != digest or result.get("size") != len(content)
            or result.get("project_id") != operation["project_id"]
            or result.get("operation_id") != operation["operation_id"]):
        raise ValueError("artifact upload receipt mismatch")
    return result


def validate_url(url: str) -> str:
    parsed = urlsplit(url)
    if (parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('kernel_url must be an HTTP(S) origin without credentials or query')
    if parsed.scheme == 'http' and parsed.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('remote Kernel connections require HTTPS')
    return url.rstrip('/')


def save_pending(path: Path, value: dict) -> None:
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value), 'utf-8')
    temp.replace(path)


def report_pending(root, token, item, pending):
    from assistant_kernel.host_client.artifacts import ArtifactStore
    body = item['body']
    result = body.get('result', {})
    refs = result.get('local_artifact_refs', [])
    if refs:
        store = ArtifactStore(pending.parent / 'artifacts')
        uploaded = [upload_artifact(root, token, item, store, ref) for ref in refs]
        result = {**result, 'artifact_refs': uploaded}
        result.pop('local_artifact_refs', None)
        item['body'] = {**body, 'result': result}
        save_pending(pending, item)
    _post(f"{root}/operations/{item['operation_id']}/result", token, item['body'])
    pending.unlink()


def cycle(root: str, token: str, adapters: dict, pending: Path) -> None:
    if pending.exists():
        item = json.loads(pending.read_text('utf-8'))
        from assistant_kernel.host_client.journal import OperationJournal
        journal = OperationJournal(pending.with_suffix(".sqlite3"))
        body = item.get('body')
        if body is None:
            previous = journal.begin(item)
            body = {'operation_token': item['operation_token'], **(previous or {
                'state': 'failed', 'result': {'error': {'code': 'EXECUTION_UNKNOWN',
                'message': 'Execution interrupted; inspect resource before retrying.'}}})}
        item['body'] = body
        report_pending(root, token, item, pending)
        return
    op = _post(f'{root}/claim', token, timeout=40).get('operation')
    if not op:
        return
    save_pending(pending, op)
    from assistant_kernel.host_client.journal import OperationJournal
    journal = OperationJournal(pending.with_suffix(".sqlite3"))
    previous = journal.begin(op)
    if previous is not None:
        op["body"] = {"operation_token": op["operation_token"], **previous}
        save_pending(pending, op)
        report_pending(root, token, op, pending)
        return
    state, result = handle(adapters, op['action'], op.get('arguments'), op.get('capability_revision', '1'), context=op)
    op['body'] = {'operation_token': op['operation_token'], 'state': state, 'result': result}
    journal.finish(op, op['body'])
    save_pending(pending, op)
    report_pending(root, token, op, pending)


def main(config_path: str) -> None:
    base = Path(config_path).parent
    cfg = json.loads(Path(config_path).read_text("utf-8"))
    token = (base / cfg["token_file"]).read_text("utf-8").strip()
    root = f"{validate_url(cfg['kernel_url'])}/api/v1/host/{cfg['device_id']}"
    state_dir = base / cfg.get('state_dir', 'host-state')
    state_dir.mkdir(parents=True, exist_ok=True)
    pending = state_dir / 'pending.json'
    adapters = {"obs": ObsAdapter(cfg.get("obs", {}), base)}
    if cfg.get("mcp"):
        from assistant_kernel.host_client.adapters.mcp import McpQueryAdapter
        if cfg["mcp"].get("persistent_queries", False):
            from assistant_kernel.host_client.adapters.sessions import PersistentQueries
            adapters["mcp"] = PersistentQueries(cfg["mcp"])
        else:
            adapters["mcp"] = McpQueryAdapter(cfg["mcp"])
    delay = 1.0
    try:
        while True:
            try:
                cycle(root, token, adapters, pending)
                delay = 1.0
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403, 404, 422):
                    raise RuntimeError('Device authorization or endpoint rejected; pending result retained') from None
                time.sleep(delay)
                delay = min(delay * 2, 30)
            except (urllib.error.URLError, TimeoutError, OSError):
                time.sleep(delay)
                delay = min(delay * 2, 30)
    finally:
        for adapter in adapters.values():
            if hasattr(adapter, "close"):
                adapter.close()


if __name__ == "__main__":
    main(sys.argv[1])
