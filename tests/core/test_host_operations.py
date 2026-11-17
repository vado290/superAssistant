"""Host operations: action policy, idempotent submit, claim only under a live lease, token-checked results."""
import hashlib
import json

import pytest

from assistant_kernel.core import host_operations as ops
from assistant_kernel.core.db import Database, one

TOKEN = "device-secret"
TOK = "11111111-1111-1111-1111-111111111111"


@pytest.fixture
def devices(kernel_settings):
    path = kernel_settings.data_root / "config"
    path.mkdir(parents=True, exist_ok=True)
    (path / "host-devices.json").write_text(json.dumps({"devices": {"pc1": {
        "token_sha256": hashlib.sha256(TOKEN.encode()).hexdigest(),
        "projects": {"p1": ["obs.status", "obs.scenes"]}}}}), "utf-8")
    return kernel_settings


async def make_run(settings, project="p1", status="running"):
    async with Database(settings).tx() as c:
        # Like a real claim: a running run always holds an unexpired lease.
        row = await one(c, "INSERT INTO core.runs(project_id, run_type, status, claim_token, lease_until) "
                           "VALUES (%s,'supervisor',%s,%s, now() + interval '15 minutes') RETURNING run_id",
                        (project, status, TOK))
    return str(row["run_id"])


async def submit(settings, run_id, **kw):
    args = dict(project_id="p1", run_id=run_id, claim_token=TOK, owner_key=run_id, device_id="pc1",
                action="obs.status", arguments={}, request_key="k1")
    args.update(kw)
    return await ops.submit(settings, **args)


def test_authenticate(devices):
    assert ops.authenticate(devices, "pc1", TOKEN)
    with pytest.raises(PermissionError):
        ops.authenticate(devices, "pc1", "bad")
    with pytest.raises(PermissionError):
        ops.authenticate(devices, "nope", TOKEN)


async def test_capabilities_are_per_project(devices):
    assert (await ops.capabilities(devices, "p1"))["devices"][0]["actions"].keys() == {"obs.status", "obs.scenes"}
    assert (await ops.capabilities(devices, "p2"))["devices"] == []


async def test_policy_and_idempotency(devices, kernel):
    run_id = await make_run(devices)
    first = await submit(devices, run_id)
    assert first["state"] == "queued"
    again = await submit(devices, run_id)
    assert again["operation_id"] == first["operation_id"] and again["reused"]
    with pytest.raises(ValueError):
        await submit(devices, run_id, action="obs.scenes")   # same key, different request
    with pytest.raises(PermissionError):
        await submit(devices, run_id, action="obs.record.start", request_key="k2")
    with pytest.raises(PermissionError):
        await submit(devices, run_id, project_id="p2", request_key="k3")
    with pytest.raises(ValueError):
        await submit(devices, run_id, arguments={"x": 1}, request_key="k4")


async def test_claim_report_and_status(devices, kernel):
    run_id = await make_run(devices)
    op = await submit(devices, run_id)
    claimed = await ops.claim(devices, "pc1")
    assert claimed["operation_id"] == op["operation_id"] and claimed["arguments"] == {}
    assert await ops.claim(devices, "pc1") is None
    with pytest.raises(PermissionError):
        await ops.report(devices, "pc1", op["operation_id"], "wrong", "succeeded", {})
    assert (await ops.report(devices, "pc1", op["operation_id"], claimed["operation_token"], "succeeded",
                             {"recording": False}))["state"] == "succeeded"
    # a repeated report does not overwrite the final result
    await ops.report(devices, "pc1", op["operation_id"], claimed["operation_token"], "failed", {})
    status = await ops.status(devices, project_id="p1", operation_id=op["operation_id"])
    assert status["state"] == "succeeded" and status["result"] == {"recording": False}
    with pytest.raises(KeyError):
        await ops.status(devices, project_id="p2", operation_id=op["operation_id"])


async def test_lost_lease_is_not_claimed(devices, kernel):
    run_id = await make_run(devices)
    await submit(devices, run_id)
    async with Database(devices).tx() as c:
        await c.execute("UPDATE core.runs SET status='failed' WHERE run_id=%s", (run_id,))
    assert await ops.claim(devices, "pc1") is None


async def test_reused_request_reports_actual_state(devices, kernel):
    run_id = await make_run(devices)
    first = await submit(devices, run_id)
    claimed = await ops.claim(devices, 'pc1')
    await ops.report(devices, 'pc1', first['operation_id'], claimed['operation_token'], 'succeeded', {})
    assert (await submit(devices, run_id))['state'] == 'succeeded'


async def test_unknown_can_recover_with_authenticated_result(devices, kernel):
    run_id = await make_run(devices)
    first = await submit(devices, run_id)
    claimed = await ops.claim(devices, 'pc1')
    async with Database(devices).tx() as c:
        await c.execute("UPDATE core.host_operations SET expires_at=now()-interval '1 second' WHERE id=%s",
                        (first['operation_id'],))
    assert (await ops.status(devices, project_id='p1', operation_id=first['operation_id']))['state'] == 'unknown'
    await ops.report(devices, 'pc1', first['operation_id'], claimed['operation_token'], 'succeeded', {})
    assert (await ops.status(devices, project_id='p1', operation_id=first['operation_id']))['state'] == 'succeeded'


def test_client_replays_result_without_reexecuting(tmp_path, monkeypatch):
    from assistant_kernel.host_client import runner
    class Adapter:
        count = 0
        def run(self, action):
            self.count += 1
            return {'recording': False}
    adapter = Adapter()
    reports = []
    def post(url, token, body=None, **kwargs):
        if url.endswith('/claim'):
            return {'operation': {'operation_id': 'op', 'operation_token': 'secret', 'action': 'obs.status'}}
        reports.append(body)
        if len(reports) == 1:
            raise OSError('disconnected')
        return {'state': 'succeeded'}
    monkeypatch.setattr(runner, '_post', post)
    pending = tmp_path / 'pending.json'
    with pytest.raises(OSError):
        runner.cycle('https://test', 'token', {'obs': adapter}, pending)
    runner.cycle('https://test', 'token', {'obs': adapter}, pending)
    assert adapter.count == 1 and reports[0] == reports[1] and not pending.exists()


def test_client_requires_tls_for_remote():
    from assistant_kernel.host_client.runner import validate_url
    assert validate_url('https://kernel.test:8443/') == 'https://kernel.test:8443'
    assert validate_url('http://127.0.0.1:8000')
    for url in ('http://remote.test', 'https://user:secret@kernel.test', 'file:///tmp'):
        with pytest.raises(ValueError):
            validate_url(url)


async def test_mcp_to_device_api_roundtrip(devices, kernel, project_files, tmp_path):
    import os
    import sys
    import asyncio
    import httpx
    from fastapi import FastAPI
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from assistant_kernel.api.host import build_router
    from assistant_kernel.core.executors.common import workspace_snapshot
    from .test_scoped_tools_core import private_config, payload
    run_id = await make_run(devices)
    snapshot = workspace_snapshot(devices, 'p1', run_id, 'host-test', candidates=False)
    tools = ('get_authorized_workspace', 'host_capabilities', 'host_submit', 'host_status')
    env = private_config(devices, {'run_id': run_id, 'claim_token': TOK}, run_id,
                         snapshot, tmp_path, tools=tools, stage='design')
    app = FastAPI()
    app.include_router(build_router(devices))
    async def scenario():
        server = StdioServerParameters(command=sys.executable, args=['-m', 'assistant_kernel.core.tools'], env=env)
        async with stdio_client(server) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            await session.call_tool('get_authorized_workspace', {})
            capabilities = payload(await session.call_tool('host_capabilities', {}))
            assert capabilities['devices'][0]['device_id'] == 'pc1'
            op = payload(await session.call_tool('host_submit',
                         {'device_id': 'pc1', 'action': 'obs.status', 'request_key': 'integration'}))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
                assert (await client.post('/api/v1/host/pc1/claim')).status_code == 401
                headers = {'Authorization': 'Bearer ' + TOKEN}
                claimed = (await client.post('/api/v1/host/pc1/claim', headers=headers)).json()['operation']
                assert claimed['operation_id'] == op['operation_id']
                response = await client.post('/api/v1/host/pc1/operations/' + op['operation_id'] + '/result',
                    headers=headers, json={'operation_token': claimed['operation_token'],
                                          'state': 'succeeded', 'result': {'recording': False}})
                assert response.status_code == 200
            result = payload(await session.call_tool('host_status', {'operation_id': op['operation_id']}))
            assert result['state'] == 'succeeded' and result['result']['recording'] is False
    await asyncio.wait_for(scenario(), timeout=45)
    receipts = list(tmp_path.glob('mcp-runtime.*.json'))
    assert receipts and all(c['status'] == 'succeeded' for c in json.loads(receipts[0].read_text())['calls'])


@pytest.fixture
def structured_action(devices, monkeypatch):
    spec = {"revision": "1", "effect": "observe", "input_schema": {
        "type": "object", "properties": {"text": {"type": "string", "maxLength": 30}},
        "required": ["text"], "additionalProperties": False}}
    monkeypatch.setitem(ops.READ_ACTIONS, "test.echo", spec)
    path = devices.data_root / "config" / "host-devices.json"
    data = json.loads(path.read_text("utf-8"))
    data["devices"]["pc1"]["projects"]["p1"].append("test.echo")
    path.write_text(json.dumps(data), "utf-8")
    return spec


async def test_structured_arguments_revision_and_idempotency(devices, kernel, structured_action):
    run_id = await make_run(devices)
    first = await submit(devices, run_id, action="test.echo", arguments={"text": "hello"}, capability_revision="1")
    with pytest.raises(ValueError):
        await submit(devices, run_id, action="test.echo", arguments={"text": "changed"})
    for arguments, revision in [({}, "1"), ({"text": 42}, "1"), ({"text": "ok"}, "2")]:
        with pytest.raises(ValueError):
            await submit(devices, run_id, action="test.echo", arguments=arguments, capability_revision=revision)
    claimed = await ops.claim(devices, "pc1")
    assert claimed["operation_id"] == first["operation_id"]
    assert claimed["arguments"] == {"text": "hello"}
    assert claimed["capability_revision"] == "1"
    assert claimed["caller_identity"]["run_type"] == "supervisor"


async def test_side_effect_policy_rechecked_at_claim(devices, kernel, structured_action):
    from psycopg.types.json import Jsonb
    structured_action["effect"] = "interact"
    run_id = await make_run(devices)
    with pytest.raises(PermissionError):
        await submit(devices, run_id, action="test.echo", arguments={"text": "ok"})
    # A model-supplied build stage cannot turn a Supervisor into a PM.
    async with Database(devices).tx() as c:
        await c.execute("UPDATE core.runs SET input=%s WHERE run_id=%s", (Jsonb({"stage": "build"}), run_id))
    with pytest.raises(PermissionError):
        await submit(devices, run_id, action="test.echo", arguments={"text": "ok"})
    async with Database(devices).tx() as c:
        task = await one(c, "INSERT INTO core.tasks(project_id,kind,origin,goal,state,stage) VALUES ('p1','delivery','user','test','pm','build') RETURNING task_id")
        await c.execute("UPDATE core.runs SET run_type='pm_turn', task_id=%s, base=%s WHERE run_id=%s",
                        (task["task_id"], Jsonb({"state_revision": 0}), run_id))
    await submit(devices, run_id, action="test.echo", arguments={"text": "ok"})
    assert (await ops.claim(devices, "pc1"))["effect"] == "interact"
    await submit(devices, run_id, action="test.echo", arguments={"text": "ok"}, request_key="second")
    async with Database(devices).tx() as c:
        await c.execute("UPDATE core.tasks SET state_revision=1 WHERE task_id=%s", (task["task_id"],))
    assert await ops.claim(devices, "pc1") is None
    with pytest.raises(PermissionError):
        await submit(devices, run_id, action="test.echo", arguments={"text": "ok"}, request_key="stale")


async def test_lease_and_changed_revision_are_rejected(devices, kernel, structured_action):
    run_id = await make_run(devices)
    await submit(devices, run_id, action="test.echo", arguments={"text": "ok"})
    structured_action["revision"] = "2"
    assert await ops.claim(devices, "pc1") is None
    async with Database(devices).tx() as c:
        await c.execute("UPDATE core.runs SET status='failed' WHERE run_id=%s", (run_id,))
    with pytest.raises(PermissionError):
        await submit(devices, run_id, request_key="expired")


def test_argument_bounds_and_pc_forwarding():
    from assistant_kernel.host_client import runner
    spec = {"revision": "1", "input_schema": {"type": "object"}}
    with pytest.raises(ValueError):
        ops.validate_arguments(spec, {"x": "x" * 4097})
    nested = {}
    for _ in range(14):
        nested = {"x": nested}
    with pytest.raises(ValueError):
        ops.validate_arguments(spec, nested)
    class Adapter:
        revision = "2"
        def run(self, action, arguments):
            return {"action": action, **arguments}
    assert runner.handle({"test": Adapter()}, "test.echo", {"text": "ok"}, "2") == (
        "succeeded", {"action": "test.echo", "text": "ok"})
    assert runner.handle({"test": Adapter()}, "test.echo", {"text": "ok"}, "1")[0] == "failed"


async def test_artifact_import_bound_to_operation(devices, kernel):
    run_id = await make_run(devices)
    op = await submit(devices, run_id)
    claimed = await ops.claim(devices, 'pc1')
    content = b'fixture artifact'
    digest = hashlib.sha256(content).hexdigest()
    args = (devices, 'pc1', op['operation_id'], claimed['operation_token'], content, digest)
    first = await ops.import_artifact(*args)
    assert await ops.import_artifact(*args) == first
    path = devices.data_root / 'vaults' / 'p1' / first['path']
    assert path.read_bytes() == content
    with pytest.raises(PermissionError):
        await ops.import_artifact(devices, 'pc1', op['operation_id'], 'wrong', content, digest)
    with pytest.raises(ValueError):
        await ops.import_artifact(*args[:-1], 'wrong')
    assert list(path.parent.iterdir()) == [path]
