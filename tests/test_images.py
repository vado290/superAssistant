"""Offline image input checks; synthetic pictures, no Discord or model requests."""
import io
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from assistant_kernel.core import images


def test_deployment_lock_covers_declared_runtime_dependencies():
    """Deployment installs its lock and then the wheel with --no-deps."""
    from pathlib import Path
    import tomli
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    root = Path(__file__).resolve().parents[1]
    manifest = tomli.loads((root / "pyproject.toml").read_text())
    locked = {}
    for line in (root / "requirements/kernel.lock").read_text().splitlines():
        if line.strip() and not line.startswith(("#", "-")):
            requirement = Requirement(line)
            locked[canonicalize_name(requirement.name)] = requirement.specifier
    for raw in manifest["project"]["dependencies"]:
        requirement = Requirement(raw)
        assert locked.get(canonicalize_name(requirement.name)) == requirement.specifier, raw


def picture(format="PNG", color="red"):
    out = io.BytesIO()
    Image.new("RGB", (16, 12), color).save(out, format=format)
    return out.getvalue()


def attachment(data, **overrides):
    return {"filename": "test.png", "size": len(data),
            "url": "https://cdn.discordapp.com/attachments/1/2/test.png?ex=signed", **overrides}


def mock_download(monkeypatch, response):
    original = httpx.AsyncClient
    monkeypatch.setattr(images.httpx, "AsyncClient", lambda **kw: original(
        **kw, transport=httpx.MockTransport(response)))


@pytest.mark.parametrize("format", ["PNG", "JPEG", "WEBP"])
def test_static_formats_and_content_addressed_storage(tmp_path, format):
    settings = SimpleNamespace(data_root=tmp_path)
    data = picture(format)
    ref = images.store_image(settings, "p1", data, "../../wrong.pdf")
    assert ref["media_type"] == images.FORMATS[format][1]
    path, = images.validate_refs(settings, "p1", [ref])
    assert path.read_bytes() == data and ref["filename"] == "wrong.pdf"
    assert images.store_image(settings, "p1", data, "../../wrong.pdf") == ref
    assert len(list(path.parent.iterdir())) == 1
    with pytest.raises(images.ImageInputError):
        images.validate_refs(settings, "p2", [ref])
    path.write_bytes(b"x" * len(data))
    with pytest.raises(images.ImageInputError):
        images.validate_refs(settings, "p1", [ref])


@pytest.mark.parametrize("data", [b"%PDF-1.7", b"\x89PNG\r\n\x1a\nnot-a-picture", b"", picture()[:35]])
def test_invalid_files_are_rejected(data):
    with pytest.raises(images.ImageInputError):
        images.inspect_image(data)


def test_animation_pixel_and_byte_limits(monkeypatch):
    out = io.BytesIO()
    Image.new("RGB", (10, 10), "red").save(out, format="WEBP", save_all=True,
        append_images=[Image.new("RGB", (10, 10), "blue")], duration=100)
    with pytest.raises(images.ImageInputError, match="format"):
        images.inspect_image(out.getvalue())
    monkeypatch.setattr(images, "MAX_PIXELS", 10)
    with pytest.raises(images.ImageInputError, match="pixels"):
        images.inspect_image(picture())
    monkeypatch.setattr(images, "MAX_BYTES", 10)
    with pytest.raises(images.ImageInputError, match="size"):
        images.inspect_image(picture())


def test_reference_cannot_traverse_or_follow_symlink(tmp_path):
    settings = SimpleNamespace(data_root=tmp_path)
    ref = images.store_image(settings, "p1", picture(), "x.png")
    with pytest.raises(images.ImageInputError):
        images.image_path(settings, "p1", {**ref, "sha256": "../../outside"})
    path = images.image_path(settings, "p1", ref)
    outside = tmp_path / "outside.png"
    outside.write_bytes(picture())
    path.unlink()
    path.symlink_to(outside)
    with pytest.raises(images.ImageInputError):
        images.validate_refs(settings, "p1", [ref])
    path.unlink()
    internal = tmp_path / "vaults/p1/knowledge/image.png"
    internal.parent.mkdir(parents=True)
    internal.write_bytes(picture())
    path.symlink_to(internal)
    with pytest.raises(images.ImageInputError):
        images.validate_refs(settings, "p1", [ref])


async def test_download_validates_entire_batch_before_saving(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_root=tmp_path)
    data = picture()
    mock_download(monkeypatch, lambda req: httpx.Response(200, content=(b"%PDF" if "bad" in str(req.url) else data)))
    with pytest.raises(images.ImageInputError):
        await images.download_images(settings, "p1", [attachment(data), attachment(b"%PDF",
            url="https://cdn.discordapp.com/attachments/1/2/bad.pdf")])
    assert not (tmp_path / "vaults").exists()
    refs = await images.download_images(settings, "p1", [attachment(data)])
    assert images.validate_refs(settings, "p1", refs)[0].read_bytes() == data


@pytest.mark.parametrize("url", ["http://cdn.discordapp.com/attachments/1/2/a", "https://example.org/a",
    "https://cdn.discordapp.com.evil/attachments/a", "https://cdn.discordapp.com/not-attachments/a",
    "https://user:pass@cdn.discordapp.com/attachments/a", "https://cdn.discordapp.com:8443/attachments/a"])
async def test_no_arbitrary_url_fetch(tmp_path, monkeypatch, url):
    def forbidden(*args, **kwargs):
        pytest.fail("HTTP must not start")
    monkeypatch.setattr(images.httpx, "AsyncClient", forbidden)
    with pytest.raises(images.ImageInputError):
        await images.download_images(SimpleNamespace(data_root=tmp_path), "p1", [attachment(picture(), url=url)])


async def test_redirect_size_and_count(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_root=tmp_path)
    data = picture()
    mock_download(monkeypatch, lambda req: httpx.Response(302, headers={"location": "https://example.org"}))
    with pytest.raises(images.ImageInputError, match="download"):
        await images.download_images(settings, "p1", [attachment(data)])
    with pytest.raises(images.ImageInputError, match="count"):
        await images.download_images(settings, "p1", [attachment(data)] * 5)
    with pytest.raises(images.ImageInputError, match="size"):
        await images.download_images(settings, "p1", [attachment(data, size=images.MAX_BYTES + 1)])


async def test_actual_stream_limit_and_network_failure(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_root=tmp_path)
    monkeypatch.setattr(images, "MAX_BYTES", 20)
    mock_download(monkeypatch, lambda req: httpx.Response(200, content=b"x" * 21))
    with pytest.raises(images.ImageInputError, match="size"):
        await images.download_images(settings, "p1", [attachment(b"x")])
    assert not (tmp_path / "vaults").exists()


async def test_network_failure_is_a_rejected_upload(tmp_path, monkeypatch):
    def timeout(request):
        raise httpx.ReadTimeout("test timeout", request=request)
    mock_download(monkeypatch, timeout)
    with pytest.raises(images.ImageInputError, match="download"):
        await images.download_images(SimpleNamespace(data_root=tmp_path), "p1", [attachment(picture())])
    assert not (tmp_path / "vaults").exists()


def test_context_order_dedup_resume_and_overflow(tmp_path, monkeypatch):
    settings = SimpleNamespace(data_root=tmp_path)
    a = images.store_image(settings, "p1", picture(), "red.png")
    b = images.store_image(settings, "p1", picture(color="blue"), "blue.png")
    rows = [{"message_id": "m1", "body": {"text": "red", "images": [a]}},
            {"message_id": "m2", "body": {"text": "blue", "images": [b, a]}}]
    refs, note, seen = images.select_images(rows)
    assert refs == (b, a) and '"caption": "blue"' in note
    assert images.select_images(rows, seen) == ((), "", {})
    monkeypatch.setattr(images, "MAX_TURN_IMAGES", 1)
    refs, note, seen = images.select_images(rows)
    assert refs == (a,) and "1 earlier images are not attached" in note and len(seen) == 1


@pytest.mark.parametrize("session", [None, "existing-session"])
async def test_harness_passes_images_in_both_modes(tmp_path, monkeypatch, session):
    import json
    from assistant_kernel.harness.base import AgentTurnRequest
    from assistant_kernel.harness.codex_cli import CodexCliHarness
    from assistant_kernel.storage import ProjectRuntimeManager
    path = tmp_path / "picture with space.png"
    path.write_bytes(picture())
    captured = {}
    async def fake_process(*command, **kwargs):
        captured["command"] = command
        return 0, b'{"type":"thread.started","thread_id":"s"}\n{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n{"type":"turn.completed","usage":{"input_tokens":10}}\n', b"", None
    async def models(_):
        return ({"slug": "vision", "input_modalities": ["text", "image"]},)
    async def configured(*args, **kwargs):
        return {}
    monkeypatch.setattr("assistant_kernel.harness.codex_cli.run_managed_process", fake_process)
    monkeypatch.setattr("assistant_kernel.harness.mcp_config.configured_servers", configured)
    harness = CodexCliHarness(ProjectRuntimeManager(tmp_path), codex_binary="unused")
    monkeypatch.setattr(harness.model_catalog, "list_models", models)
    await harness.run_turn(AgentTurnRequest(project_id="p", prompt="look", cwd=tmp_path,
        model="vision", image_paths=(path,), physical_session_id=session, invocation_id="image-call"))
    cmd = captured["command"]
    assert cmd[cmd.index("--image") + 1] == str(path)
    assert "--" in cmd and cmd.index("--") > cmd.index("--image")
    assert cmd[-1] == "-" and (not session or cmd[-2] == session)
    record = tmp_path / "vaults/p/runs/agent-traces/invocations/image-call/request.json"
    assert json.loads(record.read_text())["images"][0]["sha256"]


async def test_text_only_model_does_not_get_a_silent_fallback(tmp_path, monkeypatch):
    from assistant_kernel.harness.base import AgentTurnRequest
    from assistant_kernel.harness.codex_cli import CodexCliHarness
    from assistant_kernel.storage import ProjectRuntimeManager
    async def models(_):
        return ({"slug": "text", "input_modalities": ["text"]},)
    harness = CodexCliHarness(ProjectRuntimeManager(tmp_path))
    monkeypatch.setattr(harness.model_catalog, "list_models", models)
    with pytest.raises(ValueError, match="does not advertise image"):
        await harness.run_turn(AgentTurnRequest(project_id="p", prompt="look", cwd=tmp_path,
            model="text", image_paths=(tmp_path / "anything.png",)))
