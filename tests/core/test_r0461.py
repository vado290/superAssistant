"""0.46.1: one place for model settings (config/models.yaml), merged into each agent at load time; /models shows it."""
from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from assistant_kernel.core import models
from assistant_kernel.core.executors.common import load_agent

from .test_gateway import gateway  # noqa: F401  (a fixture)
from .test_r0453 import TEMPLATE

STRONG = ("schema_version: kernel-models.v1\nprofiles:\n"
          "  strong: {engine: codex, provider: openai, model: gpt-6-astra, reasoning_effort: high}\n"
          "  cheap: {engine: codex, model: gpt-6-luna, reasoning_effort: low}\n"
          "default_profile: strong\n")


def settings_file(kernel_settings, text: str) -> None:
    path = models.path(kernel_settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, "utf-8")


@dataclass(frozen=True)
class Agent:   # the model fields of AgentConfig
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    profile: str | None = None


def agent(**fields):
    return Agent(**fields)


def test_without_a_settings_file_codex_chooses(kernel_settings):
    config, source = models.resolve(kernel_settings, "pm", agent())
    assert config.model is None and config.reasoning_effort is None and source["model"] == "Codex default"


def test_profiles_defaults_and_agent_exceptions_in_order(kernel_settings):
    settings_file(kernel_settings, STRONG + "defaults: {triage: cheap}\n")
    config, source = models.resolve(kernel_settings, "pm", agent())
    assert (config.model, config.reasoning_effort, config.provider) == ("gpt-6-astra", "high", "openai")
    assert source["profile"] == "strong (models.yaml default_profile)"
    assert models.resolve(kernel_settings, "triage", agent())[0].model == "gpt-6-luna"         # defaults[role]
    assert models.resolve(kernel_settings, "pm", agent(profile="cheap"))[0].model == "gpt-6-luna"  # harness.profile
    config, source = models.resolve(kernel_settings, "pm", agent(reasoning_effort="xhigh"))     # one field overridden
    assert (config.model, config.reasoning_effort, source["reasoning_effort"]) == ("gpt-6-astra", "xhigh", "agent config")


@pytest.mark.parametrize("text, error", [
    (STRONG + "defaults: {pm: missing}\n", "not defined"),
    (STRONG.replace("engine: codex, provider", "engine: claude_code, provider"), "not supported yet"),
    ("profiles: {}\n", "schema_version"),
    ("profiles: [\n", "not valid YAML"),
])
def test_bad_settings_say_what_is_wrong(kernel_settings, text, error):
    settings_file(kernel_settings, text)
    with pytest.raises(ValueError, match=error):
        models.resolve(kernel_settings, "pm", agent())


def test_loaded_agents_follow_the_settings(kernel_settings, project_files):
    settings_file(kernel_settings, STRONG + "defaults: {triage: cheap}\n")
    pm = load_agent(kernel_settings, "p1", "pm")             # the test configs pin `model: test-model`
    assert (pm.model, pm.reasoning_effort, pm.profile) == ("test-model", "high", "strong")
    config = project_files / "agents" / "pm" / "config.yaml"
    config.write_text(config.read_text("utf-8").replace("  model: test-model\n", ""), "utf-8")
    assert load_agent(kernel_settings, "p1", "pm").model == "gpt-6-astra"
    assert load_agent(kernel_settings, "p1", "pm", purpose="triage").model == "gpt-6-luna"


def test_templates_leave_models_to_the_settings():
    for name in ("pm", "supervisor"):
        harness = yaml.safe_load((TEMPLATE / name / "config.yaml").read_text("utf-8"))["harness"]
        assert not {"model", "provider", "reasoning_effort"} & set(harness), name


async def test_models_command_shows_each_role_and_its_source(gateway, kernel, kernel_settings, project_files,  # noqa: F811
                                                            monkeypatch):
    settings_file(kernel_settings, STRONG)
    import assistant_kernel.harness as harness_module

    class Catalog:
        def __init__(self, *_):
            pass

        async def resolve_model_profile(self, *, model, reasoning_effort, **_):
            if model == "nope":
                raise ValueError("model is not available in the active Codex catalog: nope")
            return SimpleNamespace(model=model, reasoning_effort=reasoning_effort)

    monkeypatch.setattr(harness_module, "CodexCliHarness", Catalog)
    text = await gateway.models_text()
    assert ("p1 · pm: test-model · effort high — profile strong (models.yaml default_profile); "
            "model from agent config, effort from profile strong") in text
    for path in (project_files / "agents").glob("*/config.yaml"):
        path.write_text(path.read_text("utf-8").replace("  model: test-model\n", ""), "utf-8")
    text = await gateway.models_text()
    assert "p1 · triage: gpt-6-astra" in text and "p1 · supervisor: gpt-6-astra" in text
    settings_file(kernel_settings, STRONG.replace("model: gpt-6-astra", "model: nope"))
    assert "⚠ model is not available in the active Codex catalog: nope" in await gateway.models_text()


def test_migration_removes_only_the_old_defaults():
    path = Path(__file__).parents[2] / "scripts" / "r0461_models_migrate.py"
    spec = importlib.util.spec_from_file_location("migrate", path)
    migrate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migrate)
    old = {"agent_id": "pm", "harness": {"provider": "openai", "model": "gpt-5.6-sol", "reasoning_effort": "medium",
                                          "context_policy": "max", "timeout_seconds": 1800}}
    new, notes = migrate.clean(old)
    assert new["harness"] == {"context_policy": "max", "timeout_seconds": 1800} and len(notes) == 3
    kept, notes = migrate.clean({"harness": {"model": "gpt-6-sol", "reasoning_effort": "medium"}})
    assert kept["harness"] == {"model": "gpt-6-sol"} and any("保留 model" in n for n in notes)
    assert migrate.clean(new)[1] == []                                                       # a second run: nothing
    assert yaml.safe_load(migrate.MODELS_YAML)["profiles"]["strong"]["model"] == "gpt-6-astra"
