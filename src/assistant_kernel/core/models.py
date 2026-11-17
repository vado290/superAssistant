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
    return getattr(settings, "data_root", Path(settings.provider_secrets_path).parent.parent if hasattr(settings, "provider_secrets_path") else Path(".")) / "config" / "models.yaml"


def load(settings) -> dict[str, Any]:
    """The settings file ({} when there is none); a malformed file is an error, never silently ignored."""
    file = path(settings)
    if not file.is_file():
        return {}
    try:
        data = yaml.safe_load(file.read_text("utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"config/models.yaml is not valid YAML: {str(exc)[:200]}") from exc
    if not isinstance(data, dict) or data.get("schema_version") not in (SCHEMA, "kernel-models.v2"):
        raise ValueError(f"config/models.yaml must have schema_version: {SCHEMA}")
    if not isinstance(data.get("profiles") or {}, dict) or not isinstance(data.get("defaults") or {}, dict):
        raise ValueError("config/models.yaml: profiles and defaults must be mappings")
    return data


def resolve(settings, role: str, agent, *, project_id: str | None = None, stage: str | None = None,
            purpose: str | None = None) -> tuple[Any, dict[str, str]]:
    """The agent config with its model settings filled in, and where each value came from."""
    data = load(settings)
    own = getattr(agent, "profile", None)
    selected = route(settings, "agent", project_id=project_id, role=role, stage=stage, purpose=purpose,
                     profile=own, data=data)
    name = selected.get("profile")
    where = selected["source"]
    profile: dict[str, Any] = {}
    if name:
        profile = selected["spec"]
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
    sources["config_version"] = selected["config_version"]
    metadata = {key: value for key, value in {"route_version": selected["config_version"], "route_source": where}.items()
                if hasattr(agent, key)}
    return replace(agent, profile=name, **metadata, **values), sources


def version(data: dict) -> str:
    import hashlib, json
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def profiles(settings, data=None) -> dict:
    from assistant_kernel.core import model_catalog
    data = load(settings) if data is None else data
    # Built-ins are suggestions available to plans; configured profiles are authoritative.
    return {**model_catalog.PROFILES, **(data.get("profiles") or {})}


def route(settings, capability: str, *, project_id=None, purpose=None, role=None, stage=None,
          profile=None, data=None) -> dict:
    """One resolution path for plans, execution and inspection; contains no credentials."""
    from assistant_kernel.core import model_catalog
    data = load(settings) if data is None else data
    v2 = data.get("schema_version") == "kernel-models.v2"
    name, source = profile, "agent config" if profile else "legacy default"
    project = (data.get("projects") or {}).get(project_id, {}) if project_id else {}
    if not name and purpose and (project.get("purposes") or {}).get(purpose):
        name, source = project["purposes"][purpose], "project purpose"
    routes = data.get("routes") or {}
    for key in (f"{role}.{stage}" if stage else None, f"{role}.{purpose}" if purpose else None, role):
        if not name and key and routes.get(key):
            name, source = routes[key], f"route {key}"
    if not name and (data.get("defaults") or {}).get(role):
        name, source = data["defaults"][role], f"models.yaml defaults.{role}"
    if not name and (data.get("capability_defaults") or {}).get(capability):
        name, source = data["capability_defaults"][capability], f"capability {capability}"
    if not name and capability == "agent":
        name, source = data.get("default_profile"), "models.yaml default_profile"
    if not name and (v2 or capability != "agent"):
        name, source = model_catalog.DEFAULT_PROFILE.get(capability), "catalog default"
    spec = dict(profiles(settings, data).get(name) or {}) if name else {}
    if name and not spec:
        raise ValueError(f"model profile {name!r} is not defined in config/models.yaml")
    if spec.get("capability", "agent") != capability:
        raise ValueError(f"profile {name!r} is not for {capability}")
    vendor = spec.get("vendor") or spec.get("provider")
    spec.setdefault("provider", vendor)
    spec.setdefault("vendor", vendor)
    provider = (data.get("providers") or {}).get(vendor, {})
    protocol = spec.get("protocol") or provider.get("protocol")
    credential = None
    if capability != "agent":
        from assistant_kernel.core import model_secrets
        credential = model_secrets.credential(settings, vendor)
        protocol = protocol or {"anthropic_native": "anthropic_messages", "openai_responses": "openai_responses",
                    "gemini_native": "gemini"}.get(credential.provider_type) if credential else protocol
    protocol = protocol or "openai_chat"
    configured_endpoint = (credential.openai_compatible_base_url or credential.native_base_url) if credential else None
    endpoint = spec.get("base_url") or provider.get("base_url") or configured_endpoint or model_catalog.base_url(vendor, protocol)
    return {"profile": name, "spec": spec, "provider": vendor, "model": spec.get("model"),
            "capability": capability, "protocol": protocol, "base_url": endpoint,
            "source": source, "config_version": version(data)}


def update(settings, change):
    """Serialize config writes and atomically replace; caller owns approval and scope checks."""
    import fcntl
    file = path(settings)
    file.parent.mkdir(parents=True, exist_ok=True)
    with file.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            data = load(settings)
            result = change(data)
            temporary = file.with_suffix(".tmp")
            temporary.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), "utf-8")
            temporary.replace(file)
            return result
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def bind(settings, project_id: str, purposes: dict[str, str], *, expected_version=None, receipt=None):
    def apply(data):
        data.setdefault("schema_version", "kernel-models.v2")
        project = data.setdefault("projects", {}).setdefault(project_id, {})
        if receipt and project.get("release_receipt") == receipt:
            return {"config_version": version(data), "purposes": project.get("purposes", {})}
        if expected_version and version(data) != expected_version:
            raise ValueError("model configuration changed; refresh the proposal")
        for purpose, name in purposes.items():
            if name not in profiles(settings, data):
                raise ValueError(f"unknown model profile {name!r}")
        project.setdefault("purposes", {}).update(purposes)
        if receipt:
            project["release_receipt"] = receipt
        return {"config_version": version(data), "purposes": dict(project["purposes"])}
    return update(settings, apply)


def migrate_existing(settings, project_ids: list[str], *, apply: bool = False) -> dict:
    """Explicit installation migration, never run automatically by a model call."""
    import json
    from assistant_kernel.core import model_catalog
    available = set()
    for project_id in project_ids:
        cached = settings.data_root / "vaults" / project_id / "runtime/codex-home/models_cache.json"
        if cached.is_file():
            raw = json.loads(cached.read_text("utf-8"))
            available.update(item.get("slug") for item in raw.get("models", []))
    required = {"gpt-6.1-sol", "gpt-6-astra"}
    if not required <= available:
        raise ValueError("target Agent models are not present in the local account catalog")
    overrides = []
    for project_id in project_ids:
        for file in (settings.data_root / "vaults" / project_id / "agents").glob("*/config.yaml"):
            raw = yaml.safe_load(file.read_text("utf-8")) or {}
            harness = raw.get("harness") or raw.get("harness_defaults") or {}
            if any(raw.get(k) or harness.get(k) for k in (*FIELDS, "profile")):
                overrides.append(f"{project_id}/{file.parent.name}")
    if overrides:
        raise ValueError("explicit Agent exceptions need an explicit migration decision: " + ", ".join(overrides))
    def migrated(data):
        if data.get("migration", {}).get("name") == "model-routing-20261117":
            return {"config_version": version(data), "projects": project_ids, "already_applied": True}
        data["schema_version"] = "kernel-models.v2"
        catalog = {name: dict(spec) for name, spec in model_catalog.PROFILES.items()}
        for spec in catalog.values():
            spec.setdefault("provider", spec.get("vendor"))
        data["profiles"] = {**catalog, **(data.get("profiles") or {})}
        before = {key: data.get(key) for key in ("default_profile", "defaults", "routes", "capability_defaults")}
        data["default_profile"] = "agent.standard"
        data["defaults"] = {}
        data["routes"] = {"triage": "agent.economy", "supervisor": "agent.standard", "pm": "agent.standard",
                          "pm.design": "agent.quality", "pm.review": "agent.quality"}
        data["capability_defaults"] = dict(model_catalog.DEFAULT_PROFILE)
        data["migration"] = {"name": "model-routing-20261117", "projects": sorted(project_ids), "previous_defaults": before}
        for project_id in project_ids:
            data.setdefault("projects", {}).setdefault(project_id, {}).setdefault("purposes", {})
        return {"config_version": version(data), "projects": sorted(project_ids), "routes": data["routes"]}
    return update(settings, migrated) if apply else migrated(load(settings))


if __name__ == "__main__":
    import argparse, json
    from assistant_kernel.config import KernelSettings
    parser = argparse.ArgumentParser(description="Preview or apply the approved model routing migration")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--projects", nargs="+", required=True)
    args = parser.parse_args()
    print(json.dumps(migrate_existing(KernelSettings.from_environment(), args.projects, apply=args.apply), indent=2))
