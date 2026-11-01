"""Images through the real gateway, database, executors and task state machine.

Discord downloads and the model are faked; only per-test databases are written.
"""
import asyncio
import io
from types import SimpleNamespace

import pytest
from PIL import Image

from assistant_kernel.core import images
from assistant_kernel.core.dispatch import Dispatcher
from assistant_kernel.core.executors import build_executors
from assistant_kernel.core.gateway.config import GatewayConfig, ProjectChannels
from assistant_kernel.core.gateway.discord_app import CoreDiscordClient
from assistant_kernel.core.gateway.service import GatewayService
from .test_dispatch import settle


class FakeTransport:
    def __init__(self):
        self.messages = []

    async def send(self, channel_id, text, buttons=(), attachment=None):
        self.messages.append({"channel": channel_id, "text": text})
        return str(len(self.messages))

    def in_channel(self, channel_id):
        return [m for m in self.messages if m["channel"] == channel_id]


def propose(target, instruction="Continue"):
    return {"decision": "propose_next", "target_stage": target, "instruction": instruction, "message": "Next step"}


def stored(settings, color="red", project="p1"):
    out = io.BytesIO()
    Image.new("RGB", (10, 10), color).save(out, "PNG")
    return images.store_image(settings, project, out.getvalue(), color + ".png")


@pytest.fixture
def gateway(kernel):
    return GatewayService(kernel, GatewayConfig("g", frozenset({"u"}),
        {"p1": ProjectChannels("p1", "pm", "sup")}), FakeTransport())


async def say(gateway, text="", *, channel="pm", ident="1", user="u", attachments=()):
    return await gateway.on_message(channel_id=channel, user_id=user, message_id=ident,
                                    text=text, attachments=attachments)


@pytest.fixture
def download(monkeypatch, kernel_settings):
    calls = []
    async def fake(settings, project, attachments):
        calls.append(attachments)
        return [stored(settings, a["color"], project) for a in attachments]
    monkeypatch.setattr(images, "download_images", fake)
    return calls


@pytest.fixture
def dispatcher(kernel, kernel_settings, fake_harness, project_files):
    return Dispatcher(kernel, build_executors(), services={"settings": kernel_settings, "harness": fake_harness})


async def test_gateway_pure_image_duplicate_permissions_and_text(gateway, kernel, download):
    assert await say(gateway, attachments=[{"color": "red"}], user="other") is None
    assert await say(gateway, attachments=[{"color": "red"}], channel="unknown") is None
    assert download == []
    result = await say(gateway, attachments=[{"color": "red"}])
    message = await kernel.get_message(result["message_id"])
    assert message["body"]["images"][0]["filename"] == "red.png" and message["body"]["text"]
    assert await say(gateway, attachments=[{"color": "red"}]) == {"action": "duplicate"}
    assert len(download) == 1
    result = await say(gateway, "ordinary text", ident="2")
    assert (await kernel.get_message(result["message_id"]))["body"] == {"text": "ordinary text"}
    assert (await say(gateway, "/status", ident="3", attachments=[{"color": "blue"}]))["action"] == "command"
    assert len(download) == 1  # command semantics do not change


async def test_download_failure_never_creates_partial_task(gateway, kernel, monkeypatch):
    async def broken(*args):
        raise images.ImageInputError("download")
    monkeypatch.setattr(images, "download_images", broken)
    result = await say(gateway, "read the attached chart", attachments=[{}])
    assert result == {"action": "image_rejected"}
    assert await kernel.active_task("p1") is None
    assert not await kernel.received_event("p1", "discord", "msg:1")
    assert "not submitted" in gateway.transport.in_channel("pm")[-1]["text"]


async def test_later_text_cannot_overtake_image_download(gateway, kernel, kernel_settings, monkeypatch):
    entered, release = asyncio.Event(), asyncio.Event()
    async def slow(*args):
        entered.set()
        await release.wait()
        return [stored(kernel_settings)]
    monkeypatch.setattr(images, "download_images", slow)
    first = asyncio.create_task(say(gateway, "first", attachments=[{}]))
    await entered.wait()
    second = asyncio.create_task(say(gateway, "second", ident="2"))
    await asyncio.sleep(0)
    assert not second.done()
    release.set()
    await asyncio.gather(first, second)
    assert [(m["body"]["text"]) for m in await kernel.recent_messages("p1")][:2] == ["first", "second"]


async def test_discord_callback_carries_attachment_metadata():
    calls = []
    async def receive(**kwargs):
        calls.append(kwargs)
    client = SimpleNamespace(config=SimpleNamespace(guild_id="1"), service=SimpleNamespace(on_message=receive))
    msg = SimpleNamespace(author=SimpleNamespace(id=4, bot=False), guild=SimpleNamespace(id=1),
        channel=SimpleNamespace(id=2), id=3, content="", attachments=[SimpleNamespace(
            filename="x.png", size=123, url="https://cdn.discordapp.com/attachments/1/2/x.png")])
    await CoreDiscordClient.on_message(client, msg)
    assert calls[0]["attachments"][0]["size"] == 123 and calls[0]["text"] == ""


async def test_picture_survives_triage_task_resume_and_fresh_stage(
        gateway, kernel, kernel_settings, sim, download, dispatcher, fake_harness):
    kernel.triage = True
    initial = await say(gateway, "Build the screen in this picture", attachments=[{"color": "red"}])
    fake_harness.script("triage", {"decision": "start_task", "goal": "Build the screen", "reply": "starting"})
    fake_harness.script("supervisor", propose("requirements", "Read the screenshot"))
    await settle(dispatcher)
    assert [len(c.image_paths) for c in fake_harness.calls] == [1, 1]
    task_id = (await kernel.active_task("p1"))["task_id"]
    assert (await kernel.get_message(initial["message_id"]))["task_id"] == task_id
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "execute")
    question = {"outcome": "needs_input", "summary": "Need audience", "document": None,
                "questions": [{"id": "q1", "text": "Who?"}]}
    fake_harness.script("pm", question)
    fake_harness.script("supervisor", {"decision": "relay_pm_questions", "message": "Who?",
        "questions": [{"id": "c1", "text": "Who?", "covers": ["q1"]}]})
    await settle(dispatcher)
    first_pm = next(c for c in fake_harness.calls if "· PM turn" in c.prompt)
    assert len(first_pm.image_paths) == 1 and first_pm.physical_session_id is None
    await say(gateway, "Also use this chart", ident="2", attachments=[{"color": "blue"}])
    fake_harness.script("supervisor", {"decision": "keep"})
    await settle(dispatcher)
    assert len(fake_harness.calls[-1].image_paths) == 2
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "submit", {"answers": {"c1": "team"}})
    fake_harness.script("pm", {"outcome": "completed", "summary": "requirements ready", "questions": [],
        "document": {"kind": "task_contract", "markdown": "# Requirements\nUse the screen and chart."}})
    fake_harness.script("supervisor", propose("design"))
    await settle(dispatcher)
    second_pm = [c for c in fake_harness.calls if "· PM turn" in c.prompt][-1]
    assert second_pm.physical_session_id and len(second_pm.image_paths) == 1
    assert second_pm.image_paths != first_pm.image_paths
    ledger = await kernel.task_ledger(task_id)
    assert any("User image references" in text for turn in ledger["turns"] for text in turn["supplements"])
    item = await sim.item("p1", task_id)
    assert item["content"]["approve"] == ["requirements"]
    await kernel.act("p1", item["item_id"], "execute")
    fake_harness.script("pm", {"outcome": "needs_input", "summary": "Question", "document": None,
        "questions": [{"id": "q2", "text": "Which layout?"}]})
    fake_harness.script("supervisor", {"decision": "relay_pm_questions", "message": "Which?",
        "questions": [{"id": "c2", "text": "Which?", "covers": ["q2"]}]})
    await settle(dispatcher)
    fresh = [c for c in fake_harness.calls if "· PM turn" in c.prompt][-1]
    assert fresh.physical_session_id is None and len(fresh.image_paths) == 2
    await sim.assert_consistent()


async def test_supervisor_consultation_is_read_only_and_user_scoped(
        gateway, kernel, kernel_settings, sim, download, dispatcher, fake_harness):
    task = (await kernel.receive("p1", role="pm", text="existing task"))["task_id"]
    fake_harness.script("supervisor", propose("requirements"))
    await settle(dispatcher)
    before = await kernel.get_task(task)
    # Another user's picture in the same channel must never attach to this user's prompt.
    await kernel.receive("p1", role="supervisor", text="private", channel="discord", chat_id="sup", user_id="other",
        images=[stored(kernel_settings, "green")])
    fake_harness.script("supervisor", {"reply": "other user's reply"})
    await settle(dispatcher)
    await say(gateway, "What is here?", channel="sup", attachments=[{"color": "blue"}])
    fake_harness.script("supervisor", {"reply": "A chart"})
    await settle(dispatcher)
    assert len(fake_harness.calls[-1].image_paths) == 1
    assert (await kernel.get_task(task))["state_revision"] == before["state_revision"]
    await say(gateway, "What about the color?", channel="sup", ident="2")
    fake_harness.script("supervisor", {"reply": "Blue"})
    await settle(dispatcher)
    assert len(fake_harness.calls[-1].image_paths) == 1  # fresh Supervisor remembers recent same-user picture


async def test_claim_boundary_and_project_isolation(kernel, kernel_settings, sim):
    ref = stored(kernel_settings)
    with pytest.raises(images.ImageInputError):
        await kernel.receive("p2", role="pm", text="wrong project", images=[ref])
    task_id = (await kernel.receive("p1", role="pm", text="first", images=[ref]))["task_id"]
    run = await kernel.claim("worker")
    await kernel.complete(run, propose("requirements"))
    await kernel.act("p1", (await sim.item("p1", task_id))["item_id"], "execute")
    run = await kernel.claim("worker")
    await kernel.receive("p1", role="pm", text="later", images=[stored(kernel_settings, "blue")])
    rows = await kernel.image_inputs(run)
    assert len(rows) == 1 and rows[0]["body"]["text"] == "first"


@pytest.mark.parametrize("role", ["pm", "supervisor"])
async def test_image_route_has_notice_and_no_destination_message(kernel, kernel_settings, role):
    kernel.triage = True
    await kernel.receive("p1", role=role, text="send this to p2", images=[stored(kernel_settings)])
    run = await kernel.claim("worker")
    result = {"decision": "route", "reply": "forwarded", "route": {"project_id": "p2", "handoff": "read image"},
              "_capabilities": ["routing"]}
    await kernel.complete(run, result)
    assert await kernel.recent_messages("p2") == []
    outbound = [m for m in await kernel.recent_messages("p1") if m["direction"] == "outbound"]
    assert "not forwarded" in outbound[-1]["body"]["text"]


async def test_json_repair_does_not_attach_images_twice(kernel, kernel_settings, project_files, fake_harness, dispatcher):
    kernel.triage = True
    await kernel.receive("p1", role="pm", text="what is this", images=[stored(kernel_settings)])
    fake_harness.script("triage", "not json", {"decision": "chat", "reply": "a red square"})
    await settle(dispatcher)
    assert len(fake_harness.calls[0].image_paths) == 1 and fake_harness.calls[1].image_paths == ()


@pytest.mark.parametrize("channel", ["pm", "sup"])
async def test_picture_then_text_task_keeps_context(
        channel, gateway, kernel, sim, download, dispatcher, fake_harness):
    kernel.triage = True
    await say(gateway, "What do you see?", channel=channel, attachments=[{"color": "red"}])
    role = "triage" if channel == "pm" else "supervisor"
    fake_harness.script(role, {"decision": "chat", "reply": "A screenshot"})
    await settle(dispatcher)
    followup = await say(gateway, "Build that screen", channel=channel, ident="2")
    if channel == "pm":
        fake_harness.script("triage", {"decision": "start_task", "goal": "Build the screen", "reply": "starting"})
        fake_harness.script("supervisor", propose("requirements"))
        await settle(dispatcher)
    else:
        fake_harness.script("supervisor", {"reply": "Please confirm", "propose_new_task": {"goal": "Build the screen"}})
        await settle(dispatcher)
        card = await kernel.card("p1")
        item = next(i for i in card["items"] if i["content"].get("decision") == "new_task")
        await kernel.act("p1", item["item_id"], "approve")
        fake_harness.script("supervisor", propose("requirements"))
        await settle(dispatcher)
    source = await kernel.get_message(followup["message_id"])
    assert source["body"].get("image_context") and "images" not in source["body"]
    assert len(fake_harness.calls[-1].image_paths) == 1
    task = await kernel.active_task("p1")
    await kernel.act("p1", (await sim.item("p1", task["task_id"]))["item_id"], "execute")
    run = await kernel.claim("worker")
    assert len(await kernel.image_inputs(run)) >= 1


@pytest.mark.parametrize("requires_images", [False, True])
async def test_text_routing_after_a_picture_is_not_globally_disabled(kernel, kernel_settings, requires_images):
    kernel.triage = True
    ref = stored(kernel_settings)
    await kernel.receive("p1", role="pm", text="text follow-up")
    run = await kernel.claim("worker")
    await kernel.complete(run, {"decision": "route", "reply": "handoff", "_capabilities": ["routing"],
        "_input_images": [ref], "route": {"project_id": "p2", "handoff": "a text request",
                                          "requires_images": requires_images}})
    assert bool(await kernel.recent_messages("p2")) is not requires_images


async def test_old_chat_images_age_out_by_message_window(kernel, kernel_settings):
    kernel.triage = True
    await kernel.receive("p1", role="pm", text="old image", images=[stored(kernel_settings)])
    latest = None
    for n in range(20):
        latest = await kernel.receive("p1", role="pm", text=f"later text {n}")
    async with kernel.db.tx() as c:
        run = (await c.execute("SELECT * FROM core.runs WHERE run_id=%s", (latest["run_id"],))).fetchone
        run = await run()
    assert await kernel.image_inputs(run) == []
