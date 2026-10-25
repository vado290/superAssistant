"""Kernel-wide model settings (0.46.1): one place for which engine, provider, model and reasoning effort the agents use.

`assistant-data/config/models.yaml` (the design is a subset of docs/PUBLIC_RELEASE_ASSESSMENT.md §2.2):

    schema_version: kernel-models.v1
    profiles:                      # named settings
      strong: {engine: codex, provider: openai, model: gpt-6-astra, reasoning_effort: high}
    default_profile: strong        # used by every role without its own choice
    defaults: {triage: strong}     # optional: role → profile (pm, supervisor, triage, or a product agent's name)

Each field is taken from the first place that sets it: the agent's own config.yaml (model / provider / reasoning_effort,
kept only for a real exception), the profile it names (`harness.profile`), `defaults[role]`, `default_profile`, then the
environment / Codex's own default. The file is read on every run, so a change applies from the next turn without a
restart. Only the codex engine exists today; the field is there so another engine can be added without a new format.
"""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

SCHEMA = "kernel-models.v1"
ENGINES = ("codex",)
FIELDS = ("provider", "model", "reasoning_effort")


def path(settings) -> Path:
    return settings.data_root / "config" / "models.yaml"


def load(settings) -> dict[str, Any]:
    """The settings file ({} when there is none); a malformed file is an error, never silently ignored."""
    file = path(settings)
    if not file.is_file():
        return {}
    try:
        data = yaml.safe_load(file.read_text("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"config/models.yaml is not valid YAML: {str(exc)[:200]}") from exc
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA:
        raise ValueError(f"config/models.yaml must have schema_version: {SCHEMA}")
    if not isinstance(data.get("profiles") or {}, dict) or not isinstance(data.get("defaults") or {}, dict):
        raise ValueError("config/models.yaml: profiles and defaults must be mappings")
    return data


def resolve(settings, role: str, agent) -> tuple[Any, dict[str, str]]:
    """The agent config with its model settings filled in, and where each value came from."""
    data = load(settings)
    own = getattr(agent, "profile", None)
    name = own or (data.get("defaults") or {}).get(role) or data.get("default_profile")
    where = "agent config" if own else f"models.yaml defaults.{role}" if (data.get("defaults") or {}).get(role) \
        else "models.yaml default_profile"
    profile: dict[str, Any] = {}
    if name:
        profile = (data.get("profiles") or {}).get(name)
        if not isinstance(profile, dict):
            raise ValueError(f"model profile {name!r} is not defined in config/models.yaml")
        engine = str(profile.get("engine") or "codex")
        if engine not in ENGINES:
            raise ValueError(f"model profile {name!r}: engine {engine!r} is not supported yet (only: {', '.join(ENGINES)})")
    fallback = {"provider": settings.default_codex_provider, "model": settings.default_codex_model,
                "reasoning_effort": settings.default_codex_reasoning_effort}
    values, sources = {}, {"profile": f"{name} ({where})" if name else "none"}
    for field in FIELDS:
        if getattr(agent, field, None):
            values[field], sources[field] = getattr(agent, field), "agent config"
        elif profile.get(field):
            values[field], sources[field] = str(profile[field]), f"profile {name}"
        else:
            values[field] = fallback[field]
            sources[field] = "environment" if fallback[field] else "Codex default"
    return replace(agent, profile=name, **values), sources
