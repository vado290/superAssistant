"""P0: independent CoS identity, bounded review and existing Discord bootstrap flow."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest
import yaml

from assistant_kernel.core import rules
from assistant_kernel.core.db import one
from assistant_kernel.core.executors.bootstrap import DiscordChannels
from assistant_kernel.core.executors.pm import render_turn

from .sim import next_step
from .test_bootstrap import CAPS, CHARTER, charter_turn, dispatcher, management
from .test_dispatch import settle


@pytest.mark.parametrize("project_kind,expected", [
    ("business", "project_review"), ("kernel", "kernel_maintenance"),
    ("chief_of_staff", "chief_of_staff"),
])
def test_review_profile_is_identity_based(project_kind, expected):
    assert rules.review_profile(project_kind) == expected


def test_cos_creation_contract_and_legacy_defaults():
    ordinary = rules.charter_spec(CHARTER)
    assert ordinary["project_kind"] == "business"
    cos = rules.project_spec({**ordinary, "project_kind": "chief_of_staff"})
    assert cos["capabilities"] == [] and cos["project_access"]["mode"] == "self"
    for patch in ({"project_kind": "kernel"}, {"capabilities": ["routing"]},
                  {"capabilities": ["shared_context"]}, {"capabilities": ["kernel_maintenance"]},
                  {"delivery": "git-repo"},
                  {"directories": [{"path": "/outside", "access": "write"}]}):
        with pytest.raises(ValueError):
            rules.project_spec({**cos, **patch})
    assert rules.project_spec({**cos, "capabilities": ["project_management"]})["capabilities"] == ["project_management"]


def test_profile_is_visible_to_pm():
    assert "chief_of_staff" in render_turn(
        {"kind": "evolution", "goal": "Review goals", "review_profile": "chief_of_staff"}, {"stage": "operate"}, [])


async def create_cos(kernel, sim, dispatcher, kernel_settings):
    await kernel.ensure_project("kernel", kind="kernel")
    # The shared fixtures predate project identity and use IDs too short for access policies.
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.projects SET kind='kernel' WHERE project_id='kernel'")
        await c.execute("UPDATE core.projects SET status='archived' WHERE project_id IN ('p1','p2')")
    for project_id in ("business-one", "business-two"):
        await kernel.ensure_project(project_id)
    task = await kernel.create_task("kernel", goal="Create Chief of Staff", origin="user", kind="bootstrap")
    dispatcher.scripts.add("supervisor:evaluate", next_step("requirements"))
    await settle(dispatcher)
    await kernel.act("kernel", (await sim.item("kernel", task["task_id"]))["item_id"], "execute")
    markdown = CHARTER.replace("project_id: sales-report", "project_id: chief-of-staff\nproject_kind: chief_of_staff")
    markdown = markdown.replace("capabilities: []", "capabilities: [project_management]\nproject_access:\n"
                                "  mode: all\n  include_future: false\n  read_scopes: [catalog, summary]")
    dispatcher.scripts.add("pm_turn", charter_turn(kernel_settings, task["task_id"], markdown))
    dispatcher.scripts.add("supervisor:evaluate", {"decision": "propose_project", **CAPS})
    await settle(dispatcher)
    await kernel.act("kernel", (await sim.item("kernel", task["task_id"]))["item_id"], "approve")
    # A real dispatcher maintains before claiming: pending vaults must remain untouched.
    await dispatcher.maintain()
    assert not (kernel_settings.data_root / "vaults" / "chief-of-staff").exists()
    await settle(dispatcher)
    return task


async def test_cos_bootstrap_identity_channels_and_manual_review(kernel, sim, dispatcher, kernel_settings, management):
    task = await create_cos(kernel, sim, dispatcher, kernel_settings)
    assert (await sim.task(task["task_id"]))["state"] == "completed"
    assert await kernel.project_kind("chief-of-staff") == "chief_of_staff"
    async with kernel.db.tx() as c:
        project = await one(c, "SELECT settings FROM core.projects WHERE project_id='chief-of-staff'")
        policy = await one(c, "SELECT envelope_json FROM core.project_access_policies WHERE source_project_id='chief-of-staff' AND is_current")
    assert project["settings"]["git_push"] is False
    assert project["settings"]["daily_operations"] is True
    assert await kernel.daily_operations("chief-of-staff")
    assert not await kernel.daily_operations("p1")
    assert policy["envelope_json"]["mode"] == "all"
    assert policy["envelope_json"]["targets"] == ["business-one", "business-two"]
    assert policy["envelope_json"]["read_scopes"] == ["catalog", "summary"]
    vault = kernel_settings.data_root / "vaults" / "chief-of-staff"
    assert yaml.safe_load((vault / "project.yaml").read_text("utf-8"))["project_kind"] == "chief_of_staff"
    routing = yaml.safe_load(management.read_text("utf-8"))["projects"]["chief-of-staff"]
    assert routing == {"pm_channel_id": "pm1", "supervisor_channel_id": "sup1"}
    notices = [m["body"]["text"] for m in await sim.outbound("kernel")]
    assert any("/g1/pm1" in n and "/g1/sup1" in n for n in notices)
    review = await kernel.start_evolution("chief-of-staff")
    assert review["review_profile"] == "chief_of_staff" and review["stage"] == "operate"
    async with kernel.db.tx() as c:
        run = await one(c, "SELECT input FROM core.runs WHERE task_id=%s AND run_type='pm_turn'", (review["task_id"],))
    assert "read_portfolio_snapshot" in run["input"]["instruction"]
    # An edited descriptive file does not alter platform identity.
    (vault / "project.yaml").write_text("project_kind: kernel\ncapabilities: [kernel_maintenance]\n", "utf-8")
    assert await kernel.project_kind("chief-of-staff") == "chief_of_staff"


async def test_weekly_schedule_skips_cos_and_incomplete_projects(kernel):
    await kernel.ensure_project("chief-of-staff", kind="chief_of_staff")
    await kernel.ensure_project("not-ready", lifecycle="pending")
    async with kernel.db.tx() as c:
        for project in ("p1", "chief-of-staff", "not-ready"):
            await c.execute("INSERT INTO core.messages(project_id,channel,direction,body) VALUES (%s,'test','inbound','{}')", (project,))
    # Use a due Sunday within a week of the inserted activity, independent of wall-clock weekday.
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    due = now + timedelta(days=(6 - now.weekday()) % 7)
    due = due.replace(hour=5, minute=5, second=0, microsecond=0)
    queued = await kernel.schedule_evolution(due)
    assert "p1" in queued and "chief-of-staff" not in queued and "not-ready" not in queued
    assert await kernel.schedule_evolution(due) == []


async def test_failed_bootstrap_retry_restores_own_reservation(kernel, sim, dispatcher, kernel_settings):
    original = dispatcher.channels.ensure
    async def fail(*args):
        raise RuntimeError("simulated channel failure")
    dispatcher.channels.ensure = fail
    task = await create_cos(kernel, sim, dispatcher, kernel_settings)
    async with kernel.db.tx() as c:
        failed = await one(c, "SELECT lifecycle_state FROM core.project_lifecycle WHERE project_id='chief-of-staff'")
    assert failed["lifecycle_state"] == "failed"
    dispatcher.channels.ensure = original
    item = await sim.item("kernel", task["task_id"])
    assert item["content"]["decision"] == "run_failed"
    await kernel.act("kernel", item["item_id"], "retry")
    await settle(dispatcher)
    async with kernel.db.tx() as c:
        ready = await one(c, "SELECT lifecycle_state FROM core.project_lifecycle WHERE project_id='chief-of-staff'")
    assert ready["lifecycle_state"] == "ready"
    assert await kernel.project_kind("chief-of-staff") == "chief_of_staff"


async def test_discord_channels_are_created_once_and_found_on_retry(monkeypatch):
    channels, posts = [], []
    def respond(request):
        if request.method == "GET":
            return httpx.Response(200, json=channels)
        payload = json.loads(request.content)
        posts.append(payload)
        created = {**payload, "id": str(len(channels) + 1)}
        channels.append(created)
        return httpx.Response(200, json=created)
    client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: client(**kw, transport=httpx.MockTransport(respond)))
    creator = DiscordChannels("test-token", "guild")
    first = await creator.ensure("chief-of-staff", "Chief of Staff", "Help prioritize goals")
    second = await creator.ensure("chief-of-staff", "Chief of Staff", "Help prioritize goals")
    assert first == second and len(posts) == 3
    assert posts[0]["type"] == 4
    assert {p["name"] for p in posts[1:]} == {"pm", "supervisor"}
    assert all(p["parent_id"] == first["category_id"] for p in posts[1:])


async def test_review_profile_is_frozen_and_migration_can_repeat(kernel):
    from pathlib import Path
    first = await kernel.start_evolution("p1")
    assert first["review_profile"] == "project_review"
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.projects SET kind='kernel' WHERE project_id='p1'")
    second = await kernel.start_evolution("p1")
    assert second["review_profile"] == "kernel_maintenance"
    assert (await kernel.get_task(first["task_id"]))["review_profile"] == "project_review"
    migration = Path(__file__).parents[2] / "src/assistant_kernel/core/sql/0014_chief_of_staff.sql"
    async with kernel.db.tx() as c:
        await c.execute(migration.read_text("utf-8"))
        await c.execute(migration.read_text("utf-8"))
    assert (await kernel.get_task(first["task_id"]))["review_profile"] == "project_review"
    assert (await kernel.get_task(second["task_id"]))["review_profile"] == "kernel_maintenance"


async def test_cos_does_not_read_legacy_catalog_or_route(kernel, monkeypatch):
    from assistant_kernel.core import machine
    from assistant_kernel.core.executors import common
    await kernel.ensure_project("chief-of-staff", kind="chief_of_staff")
    def forbidden(*args, **kwargs):
        raise AssertionError("CoS must not scan legacy project files")
    monkeypatch.setattr(common, "project_catalog", forbidden, raising=False)
    note = await common.project_catalog_for_run(kernel, kernel.settings, {"project_id": "chief-of-staff"})
    assert "disabled" in note and "unknown" in note
    async with kernel.db.tx() as c:
        with pytest.raises(machine.InvalidResult, match="not legacy routing"):
            await machine._route(c, "chief-of-staff", {"body": {"text": "consider priorities"}},
                                 {"project_id": "p1", "handoff": "a request"})


async def test_cos_contract_reaches_model(kernel, sim, dispatcher, kernel_settings, management, fake_harness):
    import asyncio
    from types import SimpleNamespace
    from assistant_kernel.core.executors.common import call_for_json, load_agent
    await create_cos(kernel, sim, dispatcher, kernel_settings)
    review = await kernel.start_evolution("chief-of-staff")
    run = await kernel.claim("cos-test")
    assert run["task_id"] == review["task_id"]
    config = load_agent(kernel_settings, "chief-of-staff", "pm", stage="evolution", task_kind="evolution",
                        project_kind=await kernel.project_kind("chief-of-staff"))
    fake_harness.script("pm", {"outcome": "completed", "summary": "Bounded review"})
    ctx = SimpleNamespace(kernel=kernel, services={"settings": kernel_settings, "harness": fake_harness},
                          cancel=asyncio.Event())
    await call_for_json(ctx, run, config, "# Project chief-of-staff · PM turn (weekly review)", task_id=str(review["task_id"]),
                       stage="operate", session_id=None, tools=frozenset(), sandbox="read-only")
    prompt = fake_harness.calls[-1].prompt
    assert "Platform-assigned Chief of Staff" in prompt
    assert "Automatic Chief of Staff reviews are not scheduled" in prompt
    assert "Skill assistant-chief-of-staff" in config.skills_text


async def test_routing_directory_is_public_registration_only(kernel):
    """Routing does not depend on Project Access: name, purpose and charter routing; never background or profile."""
    from assistant_kernel.core.executors import common
    await kernel.ensure_project("chief-of-staff", kind="chief_of_staff")
    await kernel.ensure_project("kernel", kind="kernel")
    await kernel.ensure_project("pending-project", lifecycle="pending")
    await kernel.ensure_project("failed-project", lifecycle="failed")
    async with kernel.db.tx() as c:
        await c.execute("""UPDATE core.projects SET settings=settings || '{"display_name":"Chief of Staff",
            "purpose":"Connect goals and next steps"}'::jsonb WHERE project_id='chief-of-staff'""")
    knowledge = kernel.settings.data_root / "vaults" / "chief-of-staff" / "knowledge"
    knowledge.mkdir(parents=True)
    (knowledge / "charter.md").write_text("# C\n\n## 7 Routing\n- Aliases: 幕僚长\n\n## 8 Background archive\n- secret fact\n",
                                          "utf-8")
    (knowledge / "user-profile.md").write_text("private profile", "utf-8")
    for enabled in (False, True):
        settings = kernel.settings.model_copy(update={"project_access_enabled": enabled})
        text = await common.project_catalog_for_run(kernel, settings, {"project_id": "p1"})
        assert "p2" in text and "chief-of-staff" in text and "Connect goals and next steps" in text and "幕僚长" in text
        assert "secret fact" not in text and "private profile" not in text
        assert "`kernel`" not in text and "pending-project" not in text and "failed-project" not in text
    assert not (kernel.settings.data_root / "vaults" / "pending-project").exists()
    assert "pending-project" not in await kernel.active_projects()
    assert "failed-project" not in await kernel.active_projects()


async def test_cos_uses_current_public_operation_and_daily_roles(kernel, sim, dispatcher, kernel_settings, management):
    from assistant_kernel.core import skills
    await create_cos(kernel, sim, dispatcher, kernel_settings)
    vault = kernel_settings.data_root / "vaults" / "chief-of-staff"
    installed = {s.name: s for s in skills.project_skills(vault, "pm")}
    shipped = {s.name: s for s in skills.template_skills()}
    assert installed["assistant-operation"].version == shipped["assistant-operation"].version
    assert "Chief of Staff scope" in installed["assistant-operation"].body
    for role in ("pm", "supervisor"):
        assert "Ongoing operation" in (vault / "agents" / role / "role.md").read_text("utf-8")


def test_operational_cos_charter_is_valid_and_explicit_about_rollout():
    from pathlib import Path
    text = (Path(__file__).parents[2] / "docs" / "CHIEF_OF_STAFF_CHARTER.md").read_text("utf-8")
    spec = rules.charter_spec(text)
    assert spec["project_kind"] == "chief_of_staff"
    assert spec["project_access"]["read_scopes"] == ["catalog", "summary"]
    assert spec["project_access"]["include_future"] is False
    assert "never claim it was sent" in text and "P2" not in text and "P3" not in text
    assert spec["pm_role"] and spec["supervisor_role"]


async def test_kernel_identity_migration_excludes_platform_from_portfolio(kernel):
    from pathlib import Path
    await kernel.ensure_project("kernel")
    migration = Path(__file__).parents[2] / "src/assistant_kernel/core/sql/0015_kernel_identity.sql"
    async with kernel.db.tx() as c:
        await c.execute(migration.read_text("utf-8"))
        await c.execute(migration.read_text("utf-8"))
    assert await kernel.project_kind("kernel") == "kernel"
    assert "kernel" not in await kernel.routing_project_ids("p1")


def test_cos_skill_is_installed_only_in_cos_projects(tmp_path):
    from assistant_kernel.core import skills
    ordinary, cos = tmp_path / "ordinary", tmp_path / "cos"
    skills.install_templates(ordinary, frozenset())
    skills.install_templates(cos, frozenset({"project_management"}), "chief_of_staff")
    for agent in ("pm", "supervisor"):
        assert not (skills.agent_skill_dir(ordinary, agent) / "assistant-chief-of-staff").exists()
        assert (skills.agent_skill_dir(cos, agent) / "assistant-chief-of-staff" / "SKILL.md").is_file()
    assert skills.sync_templates(ordinary, frozenset(), "next")["installed"] == []
    synced = tmp_path / "synced-cos"
    skills.install_templates(synced, frozenset())
    assert "pm/assistant-chief-of-staff" in skills.sync_templates(synced, frozenset(), "next", "chief_of_staff")["installed"]


async def test_cos_daily_operations_opt_out_survives_ensure(kernel, sim, dispatcher, kernel_settings):
    await create_cos(kernel, sim, dispatcher, kernel_settings)
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.projects SET settings=settings || '{\"daily_operations\":false}'::jsonb "
                        "WHERE project_id='chief-of-staff'")
    await kernel.ensure_project("chief-of-staff", kind="chief_of_staff")
    assert not await kernel.daily_operations("chief-of-staff")


async def test_cos_external_grant_is_rejected_at_write_boundary(kernel):
    from assistant_kernel.core import machine
    await kernel.ensure_project("chief-of-staff", kind="chief_of_staff")
    async with kernel.db.tx() as c:
        with pytest.raises(machine.InvalidResult, match="cannot receive external directory grants"):
            await machine.grant_directory(c, "chief-of-staff", {"key": "external", "path": "/external",
                "access": "read", "purpose": "background"}, reason="test")
        assert await one(c, "SELECT 1 FROM core.project_grants WHERE project_id='chief-of-staff'") is None


@pytest.mark.parametrize("purpose", ["consult", "evaluate"])
async def test_cos_supervisor_refuses_grant_before_card(kernel, kernel_settings, project_files, fake_harness, purpose):
    import asyncio
    from types import SimpleNamespace
    from assistant_kernel.core import skills
    from assistant_kernel.core.executors.supervisor import execute_supervisor
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.projects SET kind='chief_of_staff' WHERE project_id='p1'")
    skills.install_templates(project_files, frozenset(), "chief_of_staff")
    task = await kernel.create_task("p1", goal="Review priorities", origin="user") if purpose == "evaluate" else None
    message = await kernel.receive("p1", role="supervisor", text="Read an external directory") if not task else None
    run = await kernel.claim("cos-grant-test")
    assert run["purpose"] == purpose
    fake_harness.script("supervisor", {"decision": "propose_grant", "reply": "Requested access",
        "directory": {"path": "/external", "access": "read", "purpose": "background"}})
    ctx = SimpleNamespace(kernel=kernel, services={"settings": kernel_settings, "harness": fake_harness},
                          cancel=asyncio.Event())
    result = await execute_supervisor(run, ctx)
    assert result["_grant"] is None
    assert "cannot receive external directory grants" in result["_grant_error"]
    assert "assistant-chief-of-staff@1.0.0" in result["skills"]


def test_runtime_skill_selection_checks_identity_and_effective_capabilities(kernel_settings, project_files):
    from assistant_kernel.core import skills
    from assistant_kernel.core.executors.common import load_agent
    caps = frozenset({"project_management", "shared_context", "kernel_maintenance", "routing"})
    skills.install_templates(project_files, caps, "chief_of_staff")
    (project_files / "project.yaml").write_text(
        "project_id: p1\nlayout_version: 2\ncapabilities: [project_management, shared_context, kernel_maintenance, routing]\n", "utf-8")
    for role, purpose in (("pm", "triage"), ("supervisor", "consult")):
        ordinary = load_agent(kernel_settings, "p1", role, purpose=purpose, project_kind="business")
        cos = load_agent(kernel_settings, "p1", role, purpose=purpose, project_kind="chief_of_staff")
        assert not any(label.startswith("assistant-chief-of-staff@") for label in ordinary.skill_labels)
        assert any(label.startswith("assistant-chief-of-staff@") for label in cos.skill_labels)
        for skill in skills.project_skills(project_files, role):
            if set(skill.load.get("capabilities") or ()) - {"project_management"}:
                assert skill.label not in cos.skill_labels
