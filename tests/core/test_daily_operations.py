"""Daily operations module (Kernel日常运营模块 实施清单): stage 1 reliability fixes.

Real PostgreSQL (temporary database), real script subprocesses and the real gateway command path. No model is called
in these tests.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from assistant_kernel.core import i18n, jobs, runtime
from assistant_kernel.core.db import all_rows, one
from assistant_kernel.core.dispatch import Dispatcher, ExecutionContext
from assistant_kernel.core.executors.job import execute_job

from .sim import Fail, next_step
from .test_dispatch import settle
from .test_gateway import PM_CH, gateway, say  # noqa: F401  (gateway is a fixture)
from .test_runtime import control, release  # noqa: F401  (control is a fixture)
from .test_scenarios import start_task

COUNTING = """schema_version: runtime-manifest.v1
jobs:
  - id: counter
    type: script
    entry: scripts/count.py
    schedule: {kind: cron, expression: "0 8 * * *"}
    retries: 2
"""


def write_counter(kernel_settings, project_id="p1"):
    """A script with a visible side effect: one line per execution."""
    root = kernel_settings.data_root / "vaults" / project_id
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "count.py").write_text(
        "from pathlib import Path\np = Path('count.txt')\n"
        "p.write_text(p.read_text() + 'x' if p.exists() else 'x')\nprint('counted')\n", "utf-8")
    return root


async def registered(kernel, kernel_settings, manifest=COUNTING, project_id="p1"):
    root = write_counter(kernel_settings, project_id)
    await release(kernel, root, manifest, project_id)
    await jobs.sync_runtime(kernel, kernel_settings)
    return root


async def job_runs(kernel, project_id="p1"):
    async with kernel.db.tx() as c:
        return await all_rows(c, """SELECT run_id, status, input, error FROM core.runs
                                    WHERE project_id=%s AND run_type='job' ORDER BY created_at""", (project_id,))


async def job_row(kernel, job_id="counter", project_id="p1"):
    async with kernel.db.tx() as c:
        return await one(c, "SELECT * FROM core.jobs WHERE project_id=%s AND job_id=%s", (project_id, job_id))


# ------------------------------------------------------------------ 1.1 / 1.4: pause and /jobs run

async def test_pause_withdraws_queued_runs_and_refuses_manual_runs(kernel, kernel_settings, control, gateway):
    await registered(kernel, kernel_settings)
    await jobs.run_now(kernel, "p1", "counter")
    assert "paused" in await gateway.jobs_command("p1", ["pause", "counter"])
    [run] = await job_runs(kernel)
    assert run["status"] == "cancelled"                                    # withdrawn, not left to run later
    reply = await gateway.jobs_command("p1", ["run", "counter"], message_id="m-paused")
    assert reply == i18n.t("cmd.job_run_paused", job="counter")
    assert len(await job_runs(kernel)) == 1                                # nothing new was queued
    async with kernel.db.tx() as c:
        assert await one(c, "SELECT 1 FROM core.task_events WHERE kind='job_run_cancelled' AND payload->>'reason'='paused'")

    assert "resumed" in await gateway.jobs_command("p1", ["resume", "counter"])
    assert "will run now" in await gateway.jobs_command("p1", ["run", "counter"], message_id="m-after")
    dispatcher = Dispatcher(kernel, {"job": execute_job}, services={"settings": kernel_settings})
    await settle(dispatcher)
    assert [r["status"] for r in await job_runs(kernel)] == ["cancelled", "succeeded"]


async def test_a_redelivered_jobs_run_command_queues_one_run(kernel, kernel_settings, control, gateway):
    await registered(kernel, kernel_settings)
    for _ in range(2):   # the same Discord message delivered twice
        await say(gateway, "/jobs run counter", channel=PM_CH, message_id="m-run-1")
    assert len(await job_runs(kernel)) == 1
    await say(gateway, "/jobs run counter", channel=PM_CH, message_id="m-run-2")   # a second, separate request
    runs = await job_runs(kernel)
    assert len(runs) == 2 and {r["input"]["request_key"] for r in runs} == {"msg:m-run-1", "msg:m-run-2"}
    dispatcher = Dispatcher(kernel, {"job": execute_job}, services={"settings": kernel_settings})
    await settle(dispatcher)                                               # one mutex: they run one after the other
    assert (kernel_settings.data_root / "vaults" / "p1" / "count.txt").read_text() == "xx"


# ------------------------------------------------------------------ 1.1 / 1.3: changed or retired jobs

async def test_runs_of_a_changed_or_retired_job_do_not_overwrite_it(kernel, kernel_settings, control):
    root = await registered(kernel, kernel_settings)
    await jobs.run_now(kernel, "p1", "counter")                   # queued under the old spec
    running = await kernel.claim("w-old", run_types=("job",))     # ... and a second one already running
    assert running is None or running["input"]["job_id"] == "counter"
    if running is None:
        pytest.fail("expected the queued run to be claimable")
    await jobs.run_now(kernel, "p1", "counter")                   # queued, still old spec

    changed = COUNTING.replace("retries: 2", "retries: 1")
    await release(kernel, root, changed)
    await jobs.sync_runtime(kernel, kernel_settings)
    statuses = [r["status"] for r in await job_runs(kernel)]
    assert statuses == ["running", "cancelled"]                    # queued old-spec run withdrawn; running one kept

    await kernel.fail(running, {"type": "JobFailed", "message": "exit 1"}, retryable=False)
    job = await job_row(kernel)
    assert job["failures"] == 0 and job["last_status"] is None     # the old run says nothing about the new spec
    async with kernel.db.tx() as c:
        result = await one(c, "SELECT payload FROM core.task_events WHERE kind='job_result' ORDER BY event_id DESC LIMIT 1")
    assert result["payload"]["stale"] is True and result["payload"]["status"] == "failed"

    await jobs.run_now(kernel, "p1", "counter")
    await release(kernel, root, "schema_version: runtime-manifest.v1\njobs: []\n")   # the job is retired
    await jobs.sync_runtime(kernel, kernel_settings)
    assert (await job_runs(kernel))[-1]["status"] == "cancelled"


# ------------------------------------------------------------------ 1.5 / 1.6: lost lease of a job

async def _run_then_lose_lease(kernel, kernel_settings):
    """The job's real script ran (its side effect happened), then contact was lost before the result was recorded."""
    await jobs.run_now(kernel, "p1", "counter")
    run = await kernel.claim("w-lost", run_types=("job",))
    await execute_job(run, ExecutionContext(kernel, {"settings": kernel_settings}))
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.runs SET lease_until=now()-interval '1 second' WHERE run_id=%s", (run["run_id"],))
    assert await kernel.recover_expired() == 1
    return run


async def test_lost_lease_of_a_job_with_side_effects_is_unknown_not_rerun(kernel, kernel_settings, control, sim):
    root = await registered(kernel, kernel_settings)
    await _run_then_lose_lease(kernel, kernel_settings)
    dispatcher = Dispatcher(kernel, {"job": execute_job}, services={"settings": kernel_settings})
    await settle(dispatcher)
    assert (root / "count.txt").read_text() == "x"                          # not run a second time
    [run] = await job_runs(kernel)
    assert run["status"] == "failed" and run["error"]["type"] == "result_unknown"
    assert (await job_row(kernel))["last_status"] == "unknown"
    async with kernel.db.tx() as c:
        observed = await one(c, "SELECT payload FROM core.task_events WHERE kind='job_result'")
    assert observed["payload"]["status"] == "unknown"
    texts = [m["body"]["text"] for m in await sim.outbound("p1")]
    assert i18n.t("mx.job_unknown", job="counter") in texts


async def test_lost_lease_of_an_idempotent_job_is_retried(kernel, kernel_settings, control):
    root = await registered(kernel, kernel_settings, COUNTING.replace("retries: 2", "retries: 2\n    idempotent: true"))
    assert (await job_row(kernel))["spec"]["idempotent"] is True
    await _run_then_lose_lease(kernel, kernel_settings)
    dispatcher = Dispatcher(kernel, {"job": execute_job}, services={"settings": kernel_settings})
    await settle(dispatcher)
    assert (root / "count.txt").read_text() == "xx"                         # retried, as declared safe
    assert [r["status"] for r in await job_runs(kernel)] == ["succeeded"]


def test_idempotent_is_a_strict_boolean_and_absent_when_false(tmp_path):
    (tmp_path / "s.py").write_text("", "utf-8")
    base = "schema_version: runtime-manifest.v1\njobs:\n  - id: j1\n    type: script\n    entry: s.py\n" \
           "    schedule: {kind: cron, expression: \"0 8 * * *\"}\n"
    exists = lambda p: (tmp_path / p).exists()   # noqa: E731
    assert "idempotent" not in runtime.parse_manifest(base, exists)["jobs"][0]           # old specs stay identical
    assert "idempotent" not in runtime.parse_manifest(base + "    idempotent: false\n", exists)["jobs"][0]
    with pytest.raises(runtime.ManifestError):
        runtime.parse_manifest(base + "    idempotent: \"yes\"\n", exists)


# ------------------------------------------------------------------ 1.7 / 1.8: lease while running

async def test_work_stops_itself_when_the_lease_cannot_be_renewed(kernel, kernel_settings, control, monkeypatch):
    await registered(kernel, kernel_settings)
    await jobs.run_now(kernel, "p1", "counter")
    stopped = asyncio.Event()

    async def long_job(run, ctx):
        try:
            await asyncio.sleep(30)
        except asyncio.CancelledError:
            stopped.set()
            raise
        return {"summary": "never"}

    async def unreachable(*args, **kwargs):
        raise OSError("database out of reach")

    lease = 1.5
    dispatcher = Dispatcher(kernel, {"job": long_job}, services={"settings": kernel_settings}, lease_seconds=lease)
    loop = asyncio.get_running_loop()
    run = await dispatcher.claim_and_start()
    started = loop.time()
    monkeypatch.setattr(kernel, "heartbeat", unreachable)
    await asyncio.wait_for(stopped.wait(), timeout=lease * 3)
    assert loop.time() - started < lease                                    # before the lease runs out
    await dispatcher.drain(timeout=5)
    assert run is not None


async def test_tools_need_an_unexpired_lease_but_results_need_only_the_token(kernel, kernel_settings, control):
    from assistant_kernel.core.tools import Scope
    await registered(kernel, kernel_settings)
    await jobs.run_now(kernel, "p1", "counter")
    run = await kernel.claim("w-tool", run_types=("job",))
    scope = Scope(settings=kernel_settings, project_id="p1", run_id=str(run["run_id"]), task_id=str(run["run_id"]),
                  claim_token=str(run["claim_token"]), stage=None, allowed=set(), roots=[])
    await scope.check_lease()
    async with kernel.db.tx() as c:
        await c.execute("UPDATE core.runs SET lease_until=now()-interval '1 second' WHERE run_id=%s", (run["run_id"],))
    with pytest.raises(PermissionError):
        await scope.check_lease()                                           # no new tool effects after expiry
    await kernel.complete(run, {"summary": "done"})                         # not reclaimed yet: the result counts
    assert (await job_runs(kernel))[0]["status"] == "succeeded"


# ------------------------------------------------------------------ 1.10: outbox with an unknown send outcome

async def test_outbox_resends_an_unknown_send_once_and_marked(gateway, kernel):
    from assistant_kernel.core.machine import notify
    async with kernel.db.tx() as c:
        message_id = await notify(c, "p1", "pm", "日报已生成")
    assert await kernel.begin_delivery(message_id) is not None             # the gateway stopped right after sending
    assert await gateway.deliver_outbox() == 0                              # too recent to call it unknown
    async with kernel.db.tx() as c:
        await c.execute("""UPDATE core.messages SET body = body || jsonb_build_object('sending_at', now() - %s)
                           WHERE message_id=%s""", (timedelta(minutes=11), message_id))
    real_send = gateway.transport.send

    async def broken(*args, **kwargs):
        raise ConnectionError("discord down")

    gateway.transport.send = broken                                         # a known failure keeps the marker
    assert await gateway.deliver_outbox() == 0
    gateway.transport.send = real_send
    assert await gateway.deliver_outbox() == 1
    sent = gateway.transport.in_channel(PM_CH)[-1]["text"]
    assert sent.startswith(i18n.t("gw.maybe_duplicate")) and "日报已生成" in sent
    assert await gateway.deliver_outbox() == 0


async def test_a_second_unknown_send_is_marked_failed_not_sent_again(gateway, kernel):
    from assistant_kernel.core.machine import notify
    async with kernel.db.tx() as c:
        message_id = await notify(c, "p1", "pm", "第二条")
    for _ in range(2):
        assert await kernel.begin_delivery(message_id) is not None
        async with kernel.db.tx() as c:
            await c.execute("""UPDATE core.messages SET body = body || jsonb_build_object('sending_at', now() - %s)
                               WHERE message_id=%s""", (timedelta(minutes=11), message_id))
    assert await gateway.deliver_outbox() == 0 and gateway.transport.order == []
    async with kernel.db.tx() as c:
        assert (await one(c, "SELECT delivery_status FROM core.messages WHERE message_id=%s",
                          (message_id,)))["delivery_status"] == "failed"


# ------------------------------------------------------------------ 1.11 / 1.12: waiting tasks

async def test_a_failed_review_of_an_addition_is_reported_and_the_card_stays(kernel, sim):
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    waiting = await sim.item("p1", task_id)
    await kernel.receive("p1", role="pm", text="再加一个图表")
    sim.script("supervisor:review_input", Fail("model unavailable", retryable=False))
    await sim.drain()
    assert (await sim.item("p1", task_id))["item_id"] == waiting["item_id"]
    texts = [m["body"]["text"] for m in await sim.outbound("p1")]
    assert i18n.t("mx.review_input_failed", reason="model unavailable") in texts


async def test_unrelated_chat_does_not_keep_a_waiting_task_alive(kernel, sim):
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    async with kernel.db.tx() as c:
        await c.execute("""UPDATE core.tasks SET updated_at=now()-interval '8 days' WHERE task_id=%s""", (task_id,))
        await c.execute("""UPDATE core.messages SET created_at=now()-interval '8 days' WHERE task_id=%s""", (task_id,))
        await c.execute("""INSERT INTO core.messages(project_id, channel, direction, role, body)
                           VALUES ('p1','discord','inbound','supervisor','{"text":"随便聊聊"}')""")   # not this task's
    assert await kernel.expire_idle_tasks() == [str(task_id)]


async def test_this_tasks_own_message_keeps_it_alive(kernel, sim):
    task_id = await start_task(kernel, sim)
    sim.script("supervisor:evaluate", next_step("requirements"))
    await sim.drain()
    async with kernel.db.tx() as c:
        await c.execute("""UPDATE core.tasks SET updated_at=now()-interval '8 days' WHERE task_id=%s""", (task_id,))
    assert await kernel.expire_idle_tasks() == []                           # its source message is recent


# ------------------------------------------------------------------ 1.13: the weekly review switch

@pytest.mark.parametrize("value, expected", [("1", True), ("true", True), ("ON", True), ("0", False),
                                             ("false", False), ("no", False), ("", False), ("maybe", False)])
def test_weekly_review_switch_is_strict(monkeypatch, value, expected):
    from assistant_kernel.core.dispatch_main import env_flag
    monkeypatch.setenv("CORE_WEEKLY_EVOLUTION", value)
    assert env_flag("CORE_WEEKLY_EVOLUTION", False) is expected
    monkeypatch.setenv("CORE_RUN_JOBS", value)
    assert env_flag("CORE_RUN_JOBS", True) is (expected if value in ("1", "true", "ON", "0", "false", "no") else True)


# ================================================================== stage 2: busy-time triage and the observation loop
# Model outputs below are stand-ins (Sim results or FakeHarness replies); scripts, PostgreSQL and the state machine
# are real. These tests prove Kernel's wiring and guards, not a real model's judgement.

from assistant_kernel.core import machine  # noqa: E402
from assistant_kernel.core.kernel import Kernel  # noqa: E402

from .sim import pm, report  # noqa: E402
from .test_scenarios import execute_next  # noqa: E402

FAILING = """schema_version: runtime-manifest.v1
jobs:
  - id: broken
    type: script
    entry: scripts/fail.py
    schedule: {kind: cron, expression: "0 8 * * *"}
    retries: 0
"""


async def switch(kernel, project="p1", on=True):
    async with kernel.db.tx() as c:
        await c.execute("""UPDATE core.projects SET settings = coalesce(settings, '{}'::jsonb)
                           || jsonb_build_object('daily_operations', %s::boolean) WHERE project_id=%s""", (on, project))


def triaging(kernel_settings):
    """The production entry: PM-channel messages go through triage (the shared fixture turns triage off)."""
    return Kernel(kernel_settings, retry_backoff_seconds=0, triage=True)


async def busy_task(kernel, sim):
    task_id = await start_task(kernel, sim)
    await execute_next(kernel, sim, task_id, "requirements")     # a PM turn is queued: the task is busy
    assert (await sim.task(task_id))["state"] == "pm"
    return task_id


async def message_row(kernel, message_id):
    async with kernel.db.tx() as c:
        return await one(c, "SELECT * FROM core.messages WHERE message_id=%s", (message_id,))


async def events(kernel, kind, project="p1"):
    async with kernel.db.tx() as c:
        return await all_rows(c, "SELECT * FROM core.task_events WHERE project_id=%s AND kind=%s ORDER BY event_id",
                              (project, kind))


async def complete_only(kernel, run_type, purpose, result):
    """Claim one run of this type/purpose and record `result` for it (a stand-in model reply)."""
    run = await kernel.claim("w-x", run_types=(run_type,))
    assert run is not None and run["purpose"] == purpose, run
    await kernel.complete(run, result)
    return run


# ------------------------------------------------------------------ 2.1–2.4 busy-time triage

async def test_switch_off_keeps_the_old_attach_behaviour(kernel, kernel_settings, sim):
    task_id = await busy_task(kernel, sim)
    got = await triaging(kernel_settings).receive("p1", role="pm", text="字体大一点")
    assert got["action"] == "attached" and got["task_id"] == str(task_id)


async def test_a_progress_question_is_answered_and_not_given_to_the_task(kernel, kernel_settings, sim):
    await switch(kernel)
    task_id = await busy_task(kernel, sim)
    got = await triaging(kernel_settings).receive("p1", role="pm", text="现在进展到哪了？")
    assert got["action"] == "triage"
    await complete_only(kernel, "triage", "default", {"decision": "chat", "reply": "正在做需求分析"})
    assert (await message_row(kernel, got["message_id"]))["task_id"] is None      # not PM input
    assert "正在做需求分析" in [m["body"]["text"] for m in await sim.outbound("p1")]
    assert (await sim.task(task_id))["state"] == "pm"                              # the task did not move


async def test_a_supplement_reaches_a_supervisor_run_that_started_before_it_was_attached(kernel, kernel_settings, sim):
    await switch(kernel)
    task_id = await busy_task(kernel, sim)
    sim.script("pm_turn", pm("completed", report=report("task_contract", "r1")))
    assert (await sim.step())["run_type"] == "pm_turn"                            # → supervising
    got = await triaging(kernel_settings).receive("p1", role="pm", text="补充：数据源用 CSV")
    supervisor = await kernel.claim("w-sup", run_types=("supervisor",))            # Supervisor is already thinking
    await complete_only(kernel, "triage", "default", {"decision": "supplement"})
    assert str((await message_row(kernel, got["message_id"]))["task_id"]) == str(task_id)
    await kernel.complete(supervisor, next_step("design"))
    assert await events(kernel, "supervisor_reevaluate")                          # it evaluates again with it
    assert (await sim.task(task_id))["state"] == "supervising"


async def test_separate_work_becomes_a_card_that_queues_behind_the_task(kernel, kernel_settings, sim):
    await switch(kernel)
    task_id = await busy_task(kernel, sim)
    got = await triaging(kernel_settings).receive("p1", role="pm", text="另外帮我做一个周报")
    await complete_only(kernel, "triage", "default",
                        {"decision": "new_work", "propose_new_task": {"goal": "做周报", "message": "这是另一件事"}})
    assert (await message_row(kernel, got["message_id"]))["task_id"] is None
    [item] = [i for i in (await kernel.card("p1"))["items"] if i["content"].get("decision") == "new_task"]
    created = await kernel.act("p1", item["item_id"], "approve")
    assert (await sim.task(created["task_id"]))["state"] == "queued"
    assert (await sim.task(task_id))["state"] == "pm"                              # the current scope is unchanged


async def test_two_messages_are_triaged_one_by_one_and_neither_is_lost(kernel, kernel_settings, sim):
    await switch(kernel)
    task_id = await busy_task(kernel, sim)
    entry = triaging(kernel_settings)
    first = await entry.receive("p1", role="pm", text="进度？")
    second = await entry.receive("p1", role="pm", text="补充：要导出 PDF")
    again = await entry.receive("p1", role="pm", text="补充：要导出 PDF", channel="discord", external_event_id="e1")
    assert (await entry.receive("p1", role="pm", text="补充：要导出 PDF", channel="discord",
                                external_event_id="e1"))["action"] == "duplicate"
    await complete_only(kernel, "triage", "default", {"decision": "chat", "reply": "在做需求"})
    await complete_only(kernel, "triage", "default", {"decision": "supplement"})
    await complete_only(kernel, "triage", "default", {"decision": "supplement"})
    assert (await message_row(kernel, first["message_id"]))["task_id"] is None
    assert str((await message_row(kernel, second["message_id"]))["task_id"]) == str(task_id)
    assert str((await message_row(kernel, again["message_id"]))["task_id"]) == str(task_id)
    assert len(await events(kernel, "input_attached")) == 2


async def test_a_message_whose_task_ended_during_triage_is_routed_again(kernel, kernel_settings, sim):
    await switch(kernel)
    task_id = await busy_task(kernel, sim)
    got = await triaging(kernel_settings).receive("p1", role="pm", text="补充：加一个图表")
    await kernel.cancel_task(task_id)
    await complete_only(kernel, "triage", "default", {"decision": "supplement"})
    assert (await message_row(kernel, got["message_id"]))["task_id"] is None        # not on the cancelled task
    async with kernel.db.tx() as c:
        again = await one(c, """SELECT input FROM core.runs WHERE run_type='triage' AND status='queued'
                                AND input->>'message_id'=%s""", (got["message_id"],))
    assert again and "context_task_id" not in again["input"]                     # the idle triage takes it


async def test_failed_triage_falls_back_to_attaching_and_says_so(kernel, kernel_settings, sim):
    await switch(kernel)
    task_id = await busy_task(kernel, sim)
    got = await triaging(kernel_settings).receive("p1", role="pm", text="字体大一点")
    run = await kernel.claim("w-t", run_types=("triage",))
    await kernel.fail(run, {"message": "model unavailable"}, retryable=False)
    assert str((await message_row(kernel, got["message_id"]))["task_id"]) == str(task_id)
    assert i18n.t("mx.triage_attached", reason="model unavailable") in [m["body"]["text"] for m in await sim.outbound("p1")]


async def test_an_illegal_busy_decision_is_not_applied(kernel, kernel_settings, sim):
    await switch(kernel)
    await busy_task(kernel, sim)
    await triaging(kernel_settings).receive("p1", role="pm", text="帮我新建项目")
    run = await kernel.claim("w-t", run_types=("triage",))
    with pytest.raises(machine.InvalidResult):
        async with kernel.db.tx() as c:
            await machine.on_result(c, run, {"decision": "start_task", "goal": "x"})


async def test_an_idle_triage_that_loses_a_race_proposes_instead_of_merging(kernel, kernel_settings, sim):
    await switch(kernel)
    got = await triaging(kernel_settings).receive("p1", role="pm", text="帮我做周报")
    assert got["action"] == "triage"
    await kernel.create_task("p1", goal="另一件事", origin="user")                    # started meanwhile
    await complete_only(kernel, "triage", "default", {"decision": "start_task", "goal": "做周报", "reply": "好"})
    assert (await message_row(kernel, got["message_id"]))["task_id"] is None
    assert [i for i in (await kernel.card("p1"))["items"] if i["content"].get("decision") == "new_task"]


async def test_triage_prompt_shows_the_busy_task_and_its_contract(kernel_settings, project_files, fake_harness):
    from assistant_kernel.core.executors.triage import execute_triage
    kernel = Kernel(kernel_settings, retry_backoff_seconds=0, triage=False)
    await kernel.ensure_project("p1")
    await switch(kernel)
    task = await kernel.create_task("p1", goal="做日报工具", origin="user")
    async with kernel.db.tx() as c:   # a task being validated (its own Supervisor run is not part of this test)
        await c.execute("UPDATE core.runs SET status='cancelled' WHERE task_id=%s AND status='queued'", (task["task_id"],))
        await c.execute("UPDATE core.tasks SET state='validating' WHERE task_id=%s", (task["task_id"],))
    got = await triaging(kernel_settings).receive("p1", role="pm", text="进度？")
    assert got["action"] == "triage"
    d = Dispatcher(kernel, {"triage": execute_triage}, services={"settings": kernel_settings, "harness": fake_harness})
    fake_harness.script("triage", {"decision": "chat", "reply": "在验证"})
    await settle(d)
    prompt = fake_harness.calls[-1].prompt
    assert "while that task is running" in prompt and "做日报工具" in prompt and "supplement" in prompt
    assert "start_task" not in prompt.split("## The user's new message")[-1]


# ------------------------------------------------------------------ 2.5–2.12 the observation loop

def test_observation_levels_are_computed_by_kernel():
    spec = {"notify": "supervisor"}
    assert machine.observation_level("succeeded", 2, spec) == "info"
    assert machine.observation_level("failed", 0, spec) == "review"
    assert machine.observation_level("failed", 1, spec) == "info"
    assert machine.observation_level("failed", 2, spec) == "urgent"
    assert machine.observation_level("unknown", 5, {"notify": "none"}) == "review"
    assert machine.observation_level("failed", 0, {"notify": "none"}) == "info"
    assert machine.observation_level("failed", 0, spec, stale=True) == "info"


async def failing_job(kernel, kernel_settings, project="p1"):
    root = kernel_settings.data_root / "vaults" / project
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "fail.py").write_text("import sys\nprint('upstream feed missing')\nsys.exit(3)\n", "utf-8")
    await release(kernel, root, FAILING, project)
    await jobs.sync_runtime(kernel, kernel_settings)


async def run_job(kernel, kernel_settings, project="p1", job="broken"):
    await jobs.run_now(kernel, project, job)
    await settle(Dispatcher(kernel, {"job": execute_job}, services={"settings": kernel_settings}))


async def observe_runs(kernel, project="p1"):
    async with kernel.db.tx() as c:
        return await all_rows(c, """SELECT * FROM core.runs WHERE project_id=%s AND run_type='supervisor'
                                    AND purpose='observe' ORDER BY created_at""", (project,))


async def test_a_failure_is_reviewed_proposed_and_becomes_a_task_only_by_the_user(kernel, kernel_settings, control, sim):
    await switch(kernel)
    await failing_job(kernel, kernel_settings)
    await run_job(kernel, kernel_settings)
    [observed] = await events(kernel, "job_result")
    assert observed["payload"]["level"] == "review"
    [review] = await observe_runs(kernel)
    assert review["task_id"] is None and review["input"] == {"event_id": str(observed["event_id"]), "job_id": "broken"}

    await complete_only(kernel, "supervisor", "observe",
                        {"decision": "propose_task", "reply": "上游数据缺失，建议排查",
                         "propose_new_task": {"goal": "排查 broken 作业的上游数据", "message": "要处理吗？"}})
    async with kernel.db.tx() as c:
        assert await one(c, "SELECT 1 FROM core.tasks WHERE project_id='p1'") is None   # nothing ran by itself
    [item] = [i for i in (await kernel.card("p1"))["items"] if i["content"].get("decision") == "new_task"]
    assert item["content"]["data"]["source_ref"] == f"observation:{observed['event_id']}"

    await run_job(kernel, kernel_settings)          # 2nd failure: info
    await run_job(kernel, kernel_settings)          # 3rd failure: urgent, but this job already has an open card
    levels = [e["payload"]["level"] for e in await events(kernel, "job_result")]
    assert levels == ["review", "info", "urgent"]
    assert len(await observe_runs(kernel)) == 1 and len(await events(kernel, "observation_coalesced")) == 1

    created = await kernel.act("p1", item["item_id"], "approve")
    assert (await sim.task(created["task_id"]))["source_ref"] == f"observation:{observed['event_id']}"
    context = await kernel.evolution_context("p1")
    first = next(o for o in context["job_observations"] if o["event_id"] == observed["event_id"])
    assert first["handling"] == "observation_task"


async def test_a_declined_proposal_closes_the_observation(kernel, kernel_settings, control):
    await switch(kernel)
    await failing_job(kernel, kernel_settings)
    await run_job(kernel, kernel_settings)
    await complete_only(kernel, "supervisor", "observe",
                        {"decision": "propose_task", "propose_new_task": {"goal": "排查"}})
    [item] = (await kernel.card("p1"))["items"]
    await kernel.act("p1", item["item_id"], "reject")
    assert len(await events(kernel, "observation_closed")) == 1
    async with kernel.db.tx() as c:
        assert await one(c, "SELECT 1 FROM core.tasks WHERE project_id='p1'") is None


async def test_success_is_only_recorded_and_a_note_ends_the_review(kernel, kernel_settings, control, sim):
    await switch(kernel)
    await registered(kernel, kernel_settings)
    await run_job(kernel, kernel_settings, job="counter")
    assert [e["payload"]["level"] for e in await events(kernel, "job_result")] == ["info"]
    assert await observe_runs(kernel) == []                                     # no model call for a success

    await failing_job(kernel, kernel_settings)
    await run_job(kernel, kernel_settings)
    await complete_only(kernel, "supervisor", "observe", {"decision": "note", "reply": "上游临时故障，已自行恢复"})
    assert len(await events(kernel, "observation_reviewed")) == 1
    assert "上游临时故障，已自行恢复" in [m["body"]["text"] for m in await sim.outbound("p1")]


async def test_turning_daily_operations_off_skips_a_queued_review(kernel, kernel_settings, control):
    await switch(kernel)
    await failing_job(kernel, kernel_settings)
    await run_job(kernel, kernel_settings)
    await switch(kernel, on=False)
    await complete_only(kernel, "supervisor", "observe",
                        {"decision": "propose_task", "propose_new_task": {"goal": "排查"}})
    assert [e["payload"]["reason"] for e in await events(kernel, "observation_skipped")] == ["daily operations off"]
    assert (await kernel.card("p1") or {}).get("items", []) == []


async def test_with_daily_operations_off_a_failure_is_only_recorded(kernel, kernel_settings, control):
    await failing_job(kernel, kernel_settings)
    await run_job(kernel, kernel_settings)
    [observed] = await events(kernel, "job_result")
    assert observed["payload"]["level"] == "review" and await observe_runs(kernel) == []


async def test_another_projects_observation_is_refused(kernel, kernel_settings, control):
    await switch(kernel)
    async with kernel.db.tx() as c:
        foreign = await one(c, """INSERT INTO core.task_events(project_id, kind, payload)
                                  VALUES ('p2', 'job_result', '{"job_id": "secret", "level": "review"}')
                                  RETURNING event_id""")
        await machine.enqueue(c, "p1", "supervisor", "observe", {"event_id": str(foreign["event_id"]), "job_id": "x"})
    assert await kernel.observation("p1", foreign["event_id"]) is None
    await complete_only(kernel, "supervisor", "observe",
                        {"decision": "propose_task", "propose_new_task": {"goal": "读 p2 的数据"}})
    assert [e["payload"]["reason"] for e in await events(kernel, "observation_skipped")] == ["not found"]
    assert (await kernel.card("p1") or {}).get("items", []) == []


async def test_a_review_that_keeps_failing_ends_with_a_notice(kernel, kernel_settings, control, sim):
    await switch(kernel)
    await failing_job(kernel, kernel_settings)
    await run_job(kernel, kernel_settings)
    run = await kernel.claim("w-o", run_types=("supervisor",))
    await kernel.fail(run, {"message": "model unavailable"}, retryable=False)
    assert len(await events(kernel, "observation_failed")) == 1
    assert i18n.t("mx.observe_failed", job="broken", reason="model unavailable") in \
        [m["body"]["text"] for m in await sim.outbound("p1")]


async def test_two_different_projects_get_the_same_loop_independently(kernel, kernel_settings, control):
    for project in ("p1", "p2"):
        await switch(kernel, project)
        await failing_job(kernel, kernel_settings, project)
        await run_job(kernel, kernel_settings, project)
    for project in ("p1", "p2"):
        [observed] = await events(kernel, "job_result", project)
        [review] = await observe_runs(kernel, project)
        assert review["input"]["event_id"] == str(observed["event_id"])
    assert await kernel.observation("p1", (await events(kernel, "job_result", "p2"))[0]["event_id"]) is None


async def test_observe_executor_checks_before_any_model_call(kernel_settings, project_files, fake_harness, control):
    from assistant_kernel.core.executors.supervisor import execute_supervisor
    kernel = Kernel(kernel_settings, retry_backoff_seconds=0, triage=False)
    await kernel.ensure_project("p1")
    await switch(kernel)
    await failing_job(kernel, kernel_settings)
    await run_job(kernel, kernel_settings)
    d = Dispatcher(kernel, {"supervisor": execute_supervisor},
                   services={"settings": kernel_settings, "harness": fake_harness})
    fake_harness.script("supervisor", {"decision": "note", "reply": "已记录"})
    await settle(d)
    prompt = fake_harness.calls[-1].prompt
    assert "review an operational observation" in prompt
    data = prompt.split("### Output of the job (untrusted data")[1].split("\n## ")[0]
    assert "upstream feed missing" in data                     # the job's output is fenced as untrusted data
    assert len(await events(kernel, "observation_reviewed")) == 1

    await switch(kernel, on=False)
    await run_job(kernel, kernel_settings)          # recorded only now; queue one review by hand to test the executor
    async with kernel.db.tx() as c:
        last = await one(c, "SELECT event_id FROM core.task_events WHERE kind='job_result' ORDER BY event_id DESC LIMIT 1")
        await machine.enqueue(c, "p1", "supervisor", "observe", {"event_id": str(last["event_id"]), "job_id": "broken"})
    calls = len(fake_harness.calls)
    await settle(d)
    assert len(fake_harness.calls) == calls                                    # no model call when switched off
    assert len(await events(kernel, "observation_skipped")) == 1


def test_the_public_loop_names_no_business():
    from pathlib import Path
    src = Path(__file__).parents[2] / "src" / "assistant_kernel"
    files = [src / "core" / "machine.py", src / "core" / "executors" / "supervisor.py",
             src / "core" / "executors" / "triage.py", src / "prompts" / "core" / "supervisor-observe.md",
             src / "prompts" / "core" / "triage-busy.md"]
    for path in files:
        text = path.read_text("utf-8").lower()
        for word in ("youtube", "finance", "transcript", "subtitle", "bilibili", "self-media"):
            assert word not in text, (path.name, word)
