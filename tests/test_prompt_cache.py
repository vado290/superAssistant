from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import SecretStr

from assistant_kernel.config.settings import ProviderCredential
from assistant_kernel.core.prompt_cache import PromptParts, from_prompt
from assistant_kernel.harness.base import AgentTurnRequest
from assistant_kernel.harness.codex_cli import CodexCliHarness
from assistant_kernel.model_providers.chat import CatalogChatProvider, PromptCachePolicy
from assistant_kernel.storage import ProjectRuntimeManager


def test_prompt_parts_keep_stable_prefix_first_and_hash_only_metadata():
    parts = PromptParts("ROLE\nSKILL", "CURRENT TIME", "USER MESSAGE", "OUTPUT")
    assert parts.render().startswith("ROLE\nSKILL\n\nCURRENT TIME")
    meta = from_prompt(parts.render(), "ROLE\nSKILL", provider="openai", model="test")
    assert meta["prompt_cache"]["stable_prefix_match"] is True
    assert meta["prompt_cache"]["stable_prefix_sha256"]
    assert meta["prompt_cache"]["stable_prefix_chars"] == len("ROLE\nSKILL")
    assert "ROLE" not in json.dumps(meta)


def test_prompt_cache_rejects_unmatched_prefix():
    meta = from_prompt("MUTABLE\n\nROLE", "ROLE")
    assert meta["prompt_cache"]["stable_prefix_match"] is False
    assert meta["prompt_cache"]["stable_prefix_chars"] == 0


def test_normalize_usage_supports_common_protocols():
    result = CatalogChatProvider.normalize_usage({
        "prompt_tokens": 100, "completion_tokens": 20,
        "prompt_tokens_details": {"cached_tokens": 60},
    })
    assert result["input_tokens"] == 100
    assert result["cached_input_tokens"] == 60
    assert result["output_tokens"] == 20
    assert result["cache_hit_ratio"] == 0.6


@pytest.mark.asyncio
async def test_codex_archives_prompt_cache_metadata(tmp_path, monkeypatch):
    async def no_configured_servers(*args, **kwargs):
        return []

    monkeypatch.setattr("assistant_kernel.harness.mcp_config.configured_servers", no_configured_servers)
    async def no_cleanup(*args, **kwargs):
        return {"status": "verified", "terminated": []}
    monkeypatch.setattr("assistant_kernel.harness.codex_cli.cleanup_mcp_processes", no_cleanup)

    async def fake_process(*command, **kwargs):
        return 0, (
            b'{"type":"thread.started","thread_id":"cache-thread"}\n'
            b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}\n'
            b'{"type":"turn.completed","usage":{"input_tokens":100,"cached_input_tokens":70,"output_tokens":4}}\n'
        ), b"", None

    monkeypatch.setattr("assistant_kernel.harness.codex_cli.run_managed_process", fake_process)
    result = await CodexCliHarness(ProjectRuntimeManager(tmp_path), codex_binary="codex").run_turn(
        AgentTurnRequest(
            project_id="p", prompt="ROLE\n\nCURRENT", cwd=Path(tmp_path),
            prompt_cache={"prompt_cache": {"stable_prefix_sha256": "abc", "stable_prefix_chars": 4}},
        )
    )
    assert result.metadata["prompt_cache"]["cache_hit"] is True
    assert result.metadata["prompt_cache"]["cached_input_tokens"] == 70
    assert result.metadata["prompt_cache"]["cache_hit_ratio"] == 0.7


def _provider(provider_type="openai_responses", **model_config):
    return CatalogChatProvider("test", ProviderCredential(
        enabled=True, api_key=SecretStr("secret"), provider_type=provider_type,
        native_base_url="https://example.invalid", chat_models={"m": model_config},
    ))


@pytest.mark.asyncio
async def test_openai_responses_cache_key_is_explicit(monkeypatch):
    captured = {}
    async def fake_post(self, path, payload, model, *, responses):
        captured.update(payload=payload)
        return None
    monkeypatch.setattr(CatalogChatProvider, "_post_openai", fake_post)
    await _provider().chat("turn", "m", system_prompt="stable", cache_policy=PromptCachePolicy(True, "k"))
    assert captured["payload"]["prompt_cache_key"] == "k"


@pytest.mark.asyncio
async def test_unsupported_openai_compatible_provider_gets_no_cache_field(monkeypatch):
    captured = {}
    async def fake_post(self, path, payload, model, *, responses):
        captured.update(payload=payload)
        return None
    monkeypatch.setattr(CatalogChatProvider, "_post_openai", fake_post)
    provider = _provider("openai_compatible")
    await provider.chat("turn", "m", cache_policy=PromptCachePolicy(True, "k"))
    assert "prompt_cache_key" not in captured["payload"]
