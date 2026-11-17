from __future__ import annotations

from datetime import datetime, timezone
import sys

import pytest

from assistant_kernel.core.project_access import (
    AccessPolicyError,
    AccessRevisionConflict,
    normalize_envelope,
    policy_allows,
)


def test_cross_project_tools_are_project_level_platform_tools():
    from assistant_kernel.core.executors.common import PROJECT_ACCESS_TOOLS, PLATFORM_TOOLS, effective_tools
    assert PROJECT_ACCESS_TOOLS <= PLATFORM_TOOLS
    assert PROJECT_ACCESS_TOOLS <= set(effective_tools(PROJECT_ACCESS_TOOLS, []))
    assert "read_project_document" not in PROJECT_ACCESS_TOOLS
    assert "read_cross_project_document" in PROJECT_ACCESS_TOOLS


def test_self_is_default_and_hash_is_stable():
    envelope, digest = normalize_envelope({})
    assert envelope == {
        "schema_version": "project-access.v1",
        "mode": "self",
        "targets": [],
        "excluded": [],
        "include_future": False,
        "read_scopes": ["catalog", "documents", "history", "summary", "tasks"],
        "expires_at": None,
        "supplemental": [],
    }
    assert len(digest) == 64


def test_specific_is_project_level_and_never_writes():
    envelope, _ = normalize_envelope(
        {"mode": "specific", "targets": ["project-b", "project-c"], "read_scopes": ["history"]},
        source_project_id="project-a",
    )
    assert envelope["targets"] == ["project-b", "project-c"]
    assert policy_allows(envelope, "project-a", "project-b", "history")
    assert not policy_allows(envelope, "project-a", "project-b", "tasks")
    # The trusted service binds the database policy to its source; an envelope is not a bearer token.
    assert not policy_allows(envelope, "project-a", "project-c", "write")


def test_all_except_and_future_projects_are_explicit():
    envelope, _ = normalize_envelope(
        {"mode": "all_except", "excluded": ["project-c"], "include_future": True},
        source_project_id="project-a", frozen_targets=["project-b", "project-c"],
    )
    assert policy_allows(envelope, "project-a", "project-b", "summary")
    assert not policy_allows(envelope, "project-a", "project-c", "summary")
    assert policy_allows(envelope, "project-a", "project-d", "summary", eligible_targets={"project-d"})


def test_expiry_is_fail_closed():
    envelope, _ = normalize_envelope({"mode": "all", "include_future": True},
                                     source_project_id="project-a", frozen_targets=["project-b"])
    envelope["expires_at"] = "2020-01-01T00:00:00Z"
    assert not policy_allows(envelope, "project-a", "project-b", "summary",
                             now=datetime(2026, 1, 1, tzinfo=timezone.utc))


@pytest.mark.parametrize("raw", [
    {"mode": "specific"},
    {"mode": "self", "targets": ["project-b"]},
    {"mode": "all", "excluded": ["project-b"]},
    {"mode": "specific", "targets": ["project-b"], "include_future": True},
    {"mode": "specific", "targets": ["project-b"], "read_scopes": ["files"]},
    {"mode": "all", "write_scopes": ["modify"]},
])
def test_invalid_or_write_policy_is_rejected(raw):
    with pytest.raises(AccessPolicyError):
        normalize_envelope(raw, source_project_id="project-a", frozen_targets=["project-b"])


from datetime import timedelta
from assistant_kernel.core.project_access import ProjectAccessService
from assistant_kernel.core.db import one, all_rows


@pytest.fixture
async def access_setup(kernel):
    settings = kernel.settings.model_copy(update={"project_access_enabled": True})
    from assistant_kernel.core.kernel import Kernel
    k = Kernel(settings, triage=False)
    for pid in ("project-a", "project-b"):
        await k.ensure_project(pid)
    async with k.db.tx() as c:
        for pid in ("project-a", "project-b"):
            await c.execute("""INSERT INTO core.project_lifecycle(project_id,lifecycle_state,registration_origin,ready_at)
                VALUES (%s,'ready','legacy_import',now()) ON CONFLICT DO NOTHING""", (pid,))
    return k, ProjectAccessService(settings)


def request(scopes=None):
    return {"targets": ["project-b"], "read_scopes": scopes or ["summary"],
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).replace(microsecond=0).isoformat(),
            "reason": "Review project progress"}


async def test_disabled_is_fail_closed_with_lifecycle_tracking(kernel):
    service = ProjectAccessService(kernel.settings)
    await kernel.ensure_project("existing-compatible")
    async with kernel.db.tx() as c:
        row = await one(c, "SELECT lifecycle_state FROM core.project_lifecycle WHERE project_id='existing-compatible'")
        assert row["lifecycle_state"] == "ready"
    with pytest.raises(PermissionError):
        await service.current("project-a")
    with pytest.raises(PermissionError):
        await kernel.request_project_access("project-a", request(), idempotency_key="disabled")


async def test_directory_minimal_and_pending_excluded(access_setup):
    k, service = access_setup
    async with k.db.tx() as c:
        await c.execute("UPDATE core.project_lifecycle SET lifecycle_state='pending' WHERE project_id='project-b'")
    rows = await service.discoverable_projects("project-a")
    assert {r["project_id"] for r in rows} == {"p1", "p2", "project-a"}
    assert all(set(r) == {"project_id", "display_name", "kind", "lifecycle_state"} for r in rows)
    with pytest.raises(PermissionError):
        await service.read_project_summary("project-a", "project-b")


async def test_cas_revocation_and_tamper_fail_closed(access_setup):
    k, service = access_setup
    assert (await service.current("project-a"))["revision"] == 0
    with pytest.raises(PermissionError):
        await service.read_project_summary("project-a", "project-b")
    await service.replace("project-a", 0, {"mode": "specific", "targets": ["project-b"], "read_scopes": ["summary"]})
    assert (await service.read_project_summary("project-a", "project-b"))["active_tasks"] == 0
    with pytest.raises(AccessRevisionConflict):
        await service.replace("project-a", 0, {})
    await service.replace("project-a", 1, {})
    with pytest.raises(PermissionError):
        await service.read_project_summary("project-a", "project-b")
    async with k.db.tx() as c:
        await c.execute("UPDATE core.project_access_policies SET envelope_sha256=repeat('0',64) WHERE is_current")
    with pytest.raises(AccessPolicyError):
        await service.current("project-a")


async def test_approval_sql_card_details_and_no_target_writes(access_setup):
    k, service = access_setup
    from assistant_kernel.core.gateway.render import render_item
    created = await k.request_project_access("project-a", request(), idempotency_key="approve")
    card = await k.card("project-a")
    item = card["items"][0]
    shown = render_item(item)
    assert "project-b" in shown["text"] and "summary" in shown["text"] and "Expires at" in shown["text"]
    result = await k.act("project-a", item["item_id"], "approve")
    assert result["access_status"] == "approved"
    assert (await service.current("project-a"))["revision"] == 1
    await service.read_project_summary("project-a", "project-b")
    assert not (k.settings.data_root / "vaults" / "project-b").exists()
    async with k.db.tx() as c:
        assert (await one(c, "SELECT count(*) AS n FROM core.task_events WHERE project_id='project-b'"))["n"] == 0
        assert (await one(c, "SELECT count(*) AS n FROM core.task_events WHERE project_id='project-a' AND kind='project_access_read'"))["n"] == 1
        assert (await one(c, "SELECT count(*) AS n FROM core.tasks"))["n"] == 0


async def test_expired_approval_commits_terminal_status(access_setup):
    k, service = access_setup
    created = await k.request_project_access("project-a", request(), idempotency_key="expire")
    item = (await k.card("project-a"))["items"][0]
    async with k.db.tx() as c:
        await c.execute("UPDATE core.project_access_requests SET candidate_expires_at=now()-interval '1 second' WHERE request_id=%s", (created["request_id"],))
    result = await k.act("project-a", item["item_id"], "approve")
    assert result["access_status"] == "expired"
    async with k.db.tx() as c:
        assert (await one(c, "SELECT status FROM core.project_access_requests WHERE request_id=%s", (created["request_id"],)))["status"] == "expired"
    assert (await service.current("project-a"))["revision"] == 0
    assert await k.card("project-a") is None


async def test_request_idempotency_and_semantic_dedup(access_setup):
    k, _ = access_setup
    raw = request()
    first = await k.request_project_access("project-a", raw, idempotency_key="retry")
    replay = await k.request_project_access("project-a", raw, idempotency_key="retry")
    duplicate = await k.request_project_access("project-a", raw, idempotency_key="another")
    assert first["request_id"] == replay["request_id"] == duplicate["request_id"]
    assert len((await k.card("project-a"))["items"]) == 1
    with pytest.raises(AccessPolicyError):
        await k.request_project_access("project-a", {**raw, "read_scopes": ["tasks"]}, idempotency_key="retry")


async def test_page_revocation_and_target_unchanged(access_setup):
    k, service = access_setup
    async with k.db.tx() as c:
        for n in range(3):
            await c.execute("INSERT INTO core.tasks(project_id,kind,origin,goal) VALUES('project-b','delivery','user',%s)", (f"Task {n}",))
    await service.replace("project-a", 0, {"mode": "specific", "targets": ["project-b"], "read_scopes": ["tasks"]})
    first = await service.read_project_tasks("project-a", "project-b", limit=1)
    assert first["next_cursor"] and len(first["items"]) == 1
    second = await service.read_project_tasks("project-a", "project-b", limit=1, cursor=first["next_cursor"])
    assert second["items"][0]["task_id"] != first["items"][0]["task_id"]
    await service.replace("project-a", 1, {})
    with pytest.raises(PermissionError):
        await service.read_project_tasks("project-a", "project-b", cursor=first["next_cursor"])


async def test_history_reads_business_text_and_revocation_blocks_next_page(access_setup):
    k, service = access_setup
    from psycopg.types.json import Jsonb
    async with k.db.tx() as c:
        for channel, role, text in [("api", "pm", "User request"), ("discord", "supervisor", "Advice"),
                                    ("kernel", "pm", "Control"), ("api", "kernel", "Internal")]:
            await c.execute("""INSERT INTO core.messages(project_id,channel,direction,role,body)
                VALUES('project-b',%s,'inbound',%s,%s)""", (channel, role, Jsonb({"text": text})))
    with pytest.raises(PermissionError):
        await service.read_project_history("project-a", "project-b")
    await service.replace("project-a", 0, {"mode": "specific", "targets": ["project-b"], "read_scopes": ["history"]})
    page = await service.read_project_history("project-a", "project-b", limit=1)
    assert len(page["items"]) == 1 and page["next_cursor"]
    second = await service.read_project_history("project-a", "project-b", limit=1, cursor=page["next_cursor"])
    assert {page["items"][0]["text"], second["items"][0]["text"]} == {"User request", "Advice"}
    await service.replace("project-a", 1, {})
    with pytest.raises(PermissionError):
        await service.read_project_history("project-a", "project-b", cursor=page["next_cursor"])


async def test_concurrent_policy_cas_has_one_winner(access_setup):
    import asyncio
    _, service = access_setup
    results = await asyncio.gather(*(service.replace("project-a", 0, {}) for _ in range(2)), return_exceptions=True)
    assert sum(isinstance(x, AccessRevisionConflict) for x in results) == 1
    assert (await service.current("project-a"))["revision"] == 1


@pytest.mark.parametrize("raw", [{"include_future": "false"}, {"schema_version": "future"},
    {"mode": "specific", "targets": ["project-a"]}])
def test_strict_policy_types(raw):
    with pytest.raises(AccessPolicyError):
        normalize_envelope(raw, source_project_id="project-a")


async def test_old_pending_approval_cannot_restore_revoked_policy(access_setup):
    k, service = access_setup
    await k.request_project_access("project-a", request(), idempotency_key="old-card")
    item = (await k.card("project-a"))["items"][0]
    await service.replace("project-a", 0, {})
    result = await k.act("project-a", item["item_id"], "approve")
    assert result["access_status"] == "stale"
    assert (await service.current("project-a"))["revision"] == 1
    with pytest.raises(PermissionError):
        await service.read_project_summary("project-a", "project-b")


async def test_supplements_do_not_cross_multiply(access_setup):
    k, service = access_setup
    await service.replace("project-a", 0, {"mode":"specific", "targets":["project-b"], "read_scopes":["summary"]})
    await k.request_project_access("project-a", request(["tasks"]), idempotency_key="task-scope")
    await k.act("project-a", (await k.card("project-a"))["items"][0]["item_id"], "approve")
    policy = (await service.current("project-a"))["envelope"]
    assert policy_allows(policy, "project-a", "project-b", "tasks")
    assert policy_allows(policy, "project-a", "project-b", "summary")
    assert not policy_allows(policy, "project-a", "project-c", "tasks")


@pytest.mark.skipif(sys.platform == "win32", reason="Windows test runner lacks symlink privilege")
async def test_public_charter_is_bounded_and_symlinks_denied(access_setup):
    k, service = access_setup
    await service.replace("project-a",0,{"mode":"specific", "targets":["project-b"], "read_scopes":["catalog"]})
    target=k.settings.data_root / "vaults" / "project-b" / "knowledge"
    target.mkdir(parents=True)
    (target / "charter.md").write_text("## Purpose\nHelp the user\n\n## Scope\nLearning\n", "utf-8")
    result=await service.read_project_catalog("project-a","project-b")
    assert "Help the user" in result["purpose"]
    (target / "charter.md").unlink()
    secret=k.settings.data_root / "secret.txt"
    secret.write_text("never return this", "utf-8")
    (target / "charter.md").symlink_to(secret)
    with pytest.raises(PermissionError):
        await service.read_project_catalog("project-a","project-b")


async def test_access_tools_real_stdio_mcp(kernel, kernel_settings, project_files, tmp_path):
    import os,sys,json,hashlib
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    from assistant_kernel.core.executors.common import workspace_snapshot
    from .test_scoped_tools_core import private_config,payload
    from assistant_kernel.core import machine
    await kernel.ensure_project("project-b")
    async with kernel.db.tx() as c:
        for pid in ("p1","project-b"):
            await c.execute("INSERT INTO core.project_lifecycle(project_id,lifecycle_state,registration_origin) VALUES(%s,'ready','legacy_import') ON CONFLICT DO NOTHING",(pid,))
        await machine.enqueue(c,"p1","supervisor","consult",{})
    run=await kernel.claim("access-tools")
    settings=kernel_settings.model_copy(update={"project_access_enabled":True})
    service=ProjectAccessService(settings)
    await service.replace("p1",0,{"mode":"specific","targets":["project-b"],"read_scopes":["summary"]})
    tools=("get_authorized_workspace","list_workspace_projects","read_project_summary","read_portfolio_snapshot","request_project_access")
    snapshot=workspace_snapshot(settings,"p1",str(run["run_id"]),"access-mcp",candidates=False)
    env=private_config(settings,run,run["run_id"],snapshot,tmp_path,tools=tools)
    file=tmp_path / "mcp-private.json"
    config=json.loads(file.read_text())
    config["scope_context"]["ASSISTANT_PROJECT_ACCESS_ENABLED"]="1"
    raw=json.dumps(config).encode();file.write_bytes(raw)
    env["KERNEL_MCP_CONFIG_SHA256"]=hashlib.sha256(raw).hexdigest()
    async with stdio_client(StdioServerParameters(command=sys.executable,args=["-m","assistant_kernel.core.tools"],env=env)) as (rd,wr),ClientSession(rd,wr) as session:
        await session.initialize()
        exposed=(await session.list_tools()).tools
        assert {tool.name for tool in exposed}==set(tools)
        assert next(t for t in exposed if t.name=="request_project_access").annotations.read_only_hint is False
        await session.call_tool("get_authorized_workspace",{})
        assert payload(await session.call_tool("read_project_summary",{"target_project_id":"project-b"}))["active_tasks"]==0
        assert payload(await session.call_tool("read_portfolio_snapshot",{}))["policy_revision"] == 1
        result=payload(await session.call_tool("request_project_access",{**request(["tasks"]),"idempotency_key":"mcp"}))
        assert result["status"]=="pending"
        async with kernel.db.tx() as c:
            await c.execute("UPDATE core.runs SET lease_until=now()-interval '1 second' WHERE run_id=%s",(run["run_id"],))
        result=payload(await session.call_tool("read_project_summary",{"target_project_id":"project-b"}))
        assert result["ok"] is False


async def test_simplification_migration_preserves_only_selected_read(kernel):
    async with kernel.db.tx() as c:
        row = await one(c, "SELECT to_regclass('core.project_bootstrap_claims') AS table_name")
        assert row["table_name"] is None
    async with kernel.db.tx() as c:
        constraint = await one(c, """SELECT pg_get_constraintdef(oid) AS definition FROM pg_constraint
            WHERE conrelid='core.project_access_requests'::regclass
              AND conname='project_access_requests_request_kind_check'""")
        assert "selected_read" in constraint["definition"] and "full_policy" not in constraint["definition"]
    from assistant_kernel.core import migrate
    assert migrate.apply(migrate.connection_args(kernel.settings)) == []


async def test_request_binds_task_from_trusted_live_run(access_setup):
    k, _ = access_setup
    from assistant_kernel.core import machine
    async with k.db.tx() as c:
        await machine.enqueue(c, "project-a", "supervisor", "consult", {})
    run = await k.claim("request-binding")
    assert run["task_id"] is None
    with pytest.raises(PermissionError):
        await k.request_project_access("project-a", request(), idempotency_key="wrong-task",
            source_run_id=run["run_id"], claim_token=run["claim_token"], source_task_id=run["run_id"])
    created = await k.request_project_access("project-a", request(), idempotency_key="bound-task",
        source_run_id=run["run_id"], claim_token=run["claim_token"])
    async with k.db.tx() as c:
        row = await one(c, "SELECT source_task_id, source_run_id FROM core.project_access_requests WHERE request_id=%s",
                        (created["request_id"],))
    assert row["source_task_id"] is None and row["source_run_id"] == run["run_id"]


async def test_disabled_registration_is_discoverable_after_enable_without_grants(kernel):
    await kernel.ensure_project("new-disabled")
    service = ProjectAccessService(kernel.settings.model_copy(update={"project_access_enabled": True}))
    assert "new-disabled" in {row["project_id"] for row in await service.discoverable_projects("p1")}
    assert (await service.current("new-disabled"))["revision"] == 0
    with pytest.raises(PermissionError):
        await service.read_project_summary("p1", "new-disabled")


async def test_registration_never_promotes_existing_lifecycle(kernel):
    await kernel.ensure_project("not-finished", lifecycle="pending")
    await kernel.ensure_project("not-finished")
    async with kernel.db.tx() as c:
        row = await one(c, "SELECT lifecycle_state, ready_at FROM core.project_lifecycle WHERE project_id='not-finished'")
        assert row["lifecycle_state"] == "pending" and row["ready_at"] is None
        await c.execute("UPDATE core.project_lifecycle SET lifecycle_state='failed' WHERE project_id='not-finished'")
    await kernel.ensure_project("not-finished")
    async with kernel.db.tx() as c:
        assert (await one(c, "SELECT lifecycle_state FROM core.project_lifecycle WHERE project_id='not-finished'"))["lifecycle_state"] == "failed"


@pytest.mark.parametrize("target_state", ["missing", "pending", "failed", "archived", "nonbusiness"])
async def test_specific_rejects_ineligible_targets_without_replacing_policy(access_setup, target_state):
    k, service = access_setup
    await service.replace("project-a", 0, {})
    if target_state == "missing":
        target = "future-project"
    else:
        target = "project-b"
        async with k.db.tx() as c:
            if target_state == "nonbusiness":
                await c.execute("UPDATE core.projects SET kind='kernel' WHERE project_id=%s", (target,))
            elif target_state == "archived":
                await c.execute("UPDATE core.projects SET status='archived' WHERE project_id=%s", (target,))
            else:
                await c.execute("UPDATE core.project_lifecycle SET lifecycle_state=%s WHERE project_id=%s", (target_state, target))
    for origin in ("user", "creation"):
        with pytest.raises(AccessPolicyError, match="existing active, ready"):
            await service.replace("project-a", 1, {"mode": "specific", "targets": [target]}, origin=origin)
    assert (await service.current("project-a"))["revision"] == 1
    if target_state == "missing":
        await k.ensure_project(target)
        with pytest.raises(PermissionError):
            await service.read_project_summary("project-a", target)


async def test_lifecycle_backfill_only_missing_rows(kernel):
    from assistant_kernel.core import migrate
    async with kernel.db.tx() as c:
        await c.execute("INSERT INTO core.projects(project_id, status) VALUES ('missed-active','active'),('missed-archived','archived')")
        await c.execute("UPDATE core.project_lifecycle SET lifecycle_state='pending', ready_at=NULL WHERE project_id='p1'")
        await c.execute((migrate.SQL_DIR / "0013_project_lifecycle_backfill.sql").read_text())
        await c.execute((migrate.SQL_DIR / "0013_project_lifecycle_backfill.sql").read_text())
        rows = {r["project_id"]: r for r in await all_rows(c, "SELECT project_id,lifecycle_state,ready_at FROM core.project_lifecycle")}
    assert rows["missed-active"]["lifecycle_state"] == "ready"
    assert rows["missed-archived"]["lifecycle_state"] == "archived"
    assert rows["p1"]["lifecycle_state"] == "pending" and rows["p1"]["ready_at"] is None


async def test_portfolio_scope_coverage_paging_revocation_and_no_target_writes(access_setup):
    k, service = access_setup
    await k.ensure_project("project-c")
    await k.ensure_project("project-hidden", lifecycle="pending")
    vault = k.settings.data_root / "vaults" / "project-b" / "knowledge"
    vault.mkdir(parents=True)
    (vault / "charter.md").write_text("## Purpose\nPublic purpose\n## Background\nDO_NOT_EXPORT", "utf-8")
    (vault / "background.md").write_text("PRIVATE_BACKGROUND", "utf-8")
    await service.replace("project-a", 0, {"mode": "specific", "targets": ["project-b", "project-c"],
        "read_scopes": ["catalog", "summary"]})
    first = await service.portfolio_snapshot("project-a", limit=1)
    assert first["items"][0]["project_id"] == "project-b" and first["next_cursor"]
    assert first["coverage"]["authorized_projects"] == 2
    assert first["coverage"]["omitted_unauthorized_projects"] == 2  # p1,p2
    assert first["policy_revision"] == 1 and first["observed_at"]
    second = await service.portfolio_snapshot("project-a", limit=1, cursor=first["next_cursor"])
    assert second["items"][0]["project_id"] == "project-c" and not second["next_cursor"]
    import json
    assert "DO_NOT_EXPORT" not in json.dumps(first) and "PRIVATE_BACKGROUND" not in json.dumps(first)
    assert not (k.settings.data_root / "vaults" / "project-c").exists()
    async with k.db.tx() as c:
        assert (await one(c, "SELECT count(*) AS n FROM core.task_events WHERE project_id='project-b'"))["n"] == 0
    await service.replace("project-a", 1, {})
    with pytest.raises(AccessRevisionConflict):
        await service.portfolio_snapshot("project-a", cursor=first["next_cursor"])
    assert (await service.portfolio_snapshot("project-a"))["items"] == []


async def test_portfolio_expiry_partial_scopes_and_lifecycle(access_setup):
    k, service = access_setup
    await service.replace("project-a", 0, {"mode": "specific", "targets": ["project-b"], "read_scopes": ["summary"],
        "expires_at": (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()})
    page = await service.portfolio_snapshot("project-a")
    assert set(page["items"][0]) == {"project_id", "read_scopes", "omitted_scopes", "summary"}
    assert page["items"][0]["omitted_scopes"] == ["catalog"] and not page["coverage"]["complete"]
    async with k.db.tx() as c:
        await c.execute("UPDATE core.project_lifecycle SET lifecycle_state='failed' WHERE project_id='project-b'")
    assert (await service.portfolio_snapshot("project-a"))["items"] == []
    async with k.db.tx() as c:
        await c.execute("UPDATE core.project_lifecycle SET lifecycle_state='ready' WHERE project_id='project-b'")
    await service.replace("project-a", 1, {"mode": "self"})
    # Trusted migration path can retain an expired envelope; reads must still deny it.
    from assistant_kernel.core.project_access import write_policy_in_transaction
    from assistant_kernel.core.db import lock_project
    async with k.db.tx() as c:
        await lock_project(c, "project-a")
        await write_policy_in_transaction(c, "project-a", 2,
            {"mode": "all", "include_future": True, "expires_at": "2020-01-01T00:00:00Z"},
            origin="legacy", allow_expired=True)
    assert (await service.portfolio_snapshot("project-a"))["items"] == []
    async with k.db.tx() as c:
        await c.execute("UPDATE core.projects SET status='archived' WHERE project_id='project-a'")
    with pytest.raises(PermissionError):
        await service.portfolio_snapshot("project-a")


async def test_portfolio_lease_and_cursor_are_not_authority(access_setup):
    k, service = access_setup
    from assistant_kernel.core import machine
    async with k.db.tx() as c:
        await machine.enqueue(c, "project-a", "supervisor", "consult", {})
    run = await k.claim("portfolio")
    await service.replace("project-a", 0, {"mode": "specific", "targets": ["project-b"], "read_scopes": ["summary"]})
    identity = {"run_id": run["run_id"], "claim_token": run["claim_token"]}
    assert (await service.portfolio_snapshot("project-a", **identity))["items"]
    with pytest.raises(ValueError):
        await service.portfolio_snapshot("project-a", cursor="invalid")
    async with k.db.tx() as c:
        await c.execute("UPDATE core.runs SET lease_until=now()-interval '1 second' WHERE run_id=%s", (run["run_id"],))
    with pytest.raises(PermissionError, match="lease"):
        await service.portfolio_snapshot("project-a", **identity)


async def test_model_catalog_has_no_legacy_fallback(access_setup, monkeypatch):
    """Chief of Staff portfolio content comes only from Project Access (routing projects use the public directory)."""
    k, service = access_setup
    from assistant_kernel.core import machine
    from assistant_kernel.core.executors import common
    async with k.db.tx() as c:
        await c.execute("UPDATE core.projects SET kind='chief_of_staff' WHERE project_id='project-a'")
        await machine.enqueue(c, "project-a", "supervisor", "consult", {})
    run = await k.claim("catalog")
    def forbidden(*args, **kwargs):
        raise AssertionError("legacy catalog scan")
    monkeypatch.setattr(common, "project_catalog", forbidden, raising=False)
    page = await common.project_catalog_for_run(k, k.settings, run)
    assert '"items": []' in page and '"policy_revision": 0' in page
    await service.replace("project-a", 0, {"mode": "specific", "targets": ["project-b"], "read_scopes": ["catalog"]})
    assert '"project_id": "project-b"' in await common.project_catalog_for_run(k, k.settings, run)


def test_cos_inherits_current_operation_templates_and_preserves_custom_copy(tmp_path, monkeypatch):
    import shutil
    from assistant_kernel.core import skills
    templates = tmp_path / "templates"
    shutil.copytree(skills.TEMPLATE_SKILLS, templates)
    monkeypatch.setattr(skills, "TEMPLATE_SKILLS", templates)
    vault = tmp_path / "cos"
    skills.install_templates(vault, frozenset())
    selected = skills.select(vault, tmp_path, role="pm", stage="evolution", task_kind="evolution")
    operation = next(s for s in selected.on_demand if s.name == "assistant-operation")
    assert operation.version == "3.6.0" and "Chief of Staff scope" in operation.body
    assert any(s.name == "assistant-evolution-review" and s.version == "2.5.0" for s in selected.always)
    skills.sync_templates(vault, frozenset(), "same-version")
    before = skills.offered(vault)["template_fingerprint"]
    custom = skills.agent_skill_dir(vault, "pm") / "assistant-evolution-review" / "SKILL.md"
    custom.write_text(custom.read_text("utf-8")+"\nLocal customization\n", "utf-8")
    template = templates / "assistant-operation" / "SKILL.md"
    template.write_text(template.read_text("utf-8").replace('3.6.0', '3.7.0'), "utf-8")
    evolution = templates / "assistant-evolution-review" / "SKILL.md"
    evolution.write_text(evolution.read_text("utf-8").replace('2.5.0', '2.6.0'), "utf-8")
    assert before != skills.template_fingerprint()
    result = skills.sync_templates(vault, frozenset(), "same-version")
    assert "assistant-operation" in result["refreshed"] and "assistant-evolution-review" in result["kept"]
    assert skills.parse(operation.path).version == "3.7.0" and "Local customization" in custom.read_text("utf-8")


def test_template_rollout_preserves_changed_support_files(tmp_path, monkeypatch):
    import shutil
    from assistant_kernel.core import skills
    templates = tmp_path / "templates"
    shutil.copytree(skills.TEMPLATE_SKILLS, templates)
    monkeypatch.setattr(skills, "TEMPLATE_SKILLS", templates)
    support = templates / "assistant-operation" / "references" / "local.md"
    support.parent.mkdir(exist_ok=True)
    support.write_text("upstream", "utf-8")
    vault = tmp_path / "cos"
    skills.install_templates(vault, frozenset())
    home = skills.agent_skill_dir(vault, "pm") / "assistant-operation"
    (home / "references" / "local.md").write_text("custom support", "utf-8")
    template = templates / "assistant-operation" / "SKILL.md"
    template.write_text(template.read_text("utf-8").replace('3.6.0', '3.7.0'), "utf-8")
    result = skills.sync_templates(vault, frozenset(), "same")
    assert "assistant-operation" in result["kept"]
    assert (home / "references" / "local.md").read_text("utf-8") == "custom support"
