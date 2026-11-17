"""Vendor keys for model calls (kernel-models.v2, phase 1): one key per vendor for the whole installation.

Stored in the control plane's `config/provider-secrets.json` (settings.provider_secrets_path), in the existing
`ProviderSecrets` format so `QwenProvider` / `CatalogChatProvider` read it unchanged; other top-level entries (such as
`internal`) are kept as they are. This is not the project credential store (`runtime/credentials.json`): project
credentials are readable by the project's models through `get_credential`, vendor keys are offered through no tool.

Only `status()` / `ready()` / `listing()` may reach prompts, cards or logs: they never contain a key.
"""
from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from assistant_kernel.core import model_catalog

PROBE_TIMEOUT_SECONDS = 15


def path(settings) -> Path:
    return Path(settings.provider_secrets_path)


@contextmanager
def _locked(settings):
    target = path(settings)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.with_name(target.name + ".lock").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield target
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _read(target: Path) -> dict[str, Any]:
    if not target.is_file():
        return {}
    data = json.loads(target.read_text("utf-8") or "{}")
    if not isinstance(data, dict):
        raise ValueError("provider-secrets.json must be a JSON object")
    return data


def _write(target: Path, data: dict[str, Any]) -> None:
    temporary = target.with_name(target.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(target)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _volcengine_shared(settings) -> bool:
    """The existing shared Volcengine file counts as configured (it predates this store)."""
    try:
        from assistant_kernel.config.volcengine import SharedVolcengineConfig
        return bool(SharedVolcengineConfig.load(settings.shared_volcengine_path).ark.api_key.get_secret_value())
    except Exception:   # a missing or broken shared file only means "not configured here"
        return False


def status(settings) -> dict[str, dict[str, Any]]:
    """Every catalog vendor and every stored one: configured (bool), validated_at, failing — never a key."""
    with _locked(settings) as target:
        providers = _read(target).get("providers") or {}
    result = {}
    for vendor in sorted(set(model_catalog.VENDORS) | set(providers)):
        spec = model_catalog.VENDORS.get(vendor) or {}
        stored = providers.get(vendor) if isinstance(providers.get(vendor), dict) else {}
        if spec.get("auth") == "codex_login":
            configured, how = True, "codex login"
        else:
            configured = bool(stored.get("enabled", True) and stored.get("api_key"))
            how = "key"
            if not configured and vendor == "volcengine" and _volcengine_shared(settings):
                configured, how = True, "shared volcengine config"
        result[vendor] = {"configured": configured, "via": how if configured else None,
                          "validated_at": stored.get("validated_at"), "failing": stored.get("failing")}
    return result


def ready(settings, vendor: str, current: dict[str, dict[str, Any]] | None = None) -> bool:
    entry = (current if current is not None else status(settings)).get(vendor) or {}
    return bool(entry.get("configured")) and not entry.get("failing")


def put(settings, vendor: str, api_key: str, *, validated: bool, base_url: str = "", source: str = "user_card",
        region: str = "") -> dict[str, Any]:
    """Store or replace a vendor's key; returns the vendor's status entry (no key)."""
    vendor = vendor.strip().lower()
    if not api_key.strip():
        raise ValueError("an empty key cannot be stored")
    spec = model_catalog.VENDORS.get(vendor)
    if spec is None and not base_url:
        raise ValueError(f"unknown vendor {vendor!r}; a vendor outside the catalog needs a base_url")
    if spec and spec.get("auth") == "codex_login":
        raise ValueError(f"{vendor} uses the Codex login, not a stored key")
    with _locked(settings) as target:
        data = _read(target)
        providers = data.setdefault("providers", {})
        entry = dict(providers.get(vendor) or {})
        entry.update(enabled=True, api_key=api_key.strip(), provider_type="openai_compatible",
                     validated_at=_now() if validated else None, failing=None, updated_at=_now(), source=source)
        if base_url:
            entry["openai_compatible_base_url"] = base_url.rstrip("/")
        if region:
            entry["region"] = region
        providers[vendor] = entry
        data.setdefault("schema_version", "1.0")
        _write(target, data)
    return {"vendor": vendor, "configured": True, "validated_at": entry["validated_at"], "failing": None}


def mark_failing(settings, vendor: str, reason: str) -> None:
    """Run-time fallback hook: a key the vendor now refuses (401/403). Cleared by the next put()."""
    with _locked(settings) as target:
        data = _read(target)
        entry = (data.get("providers") or {}).get(vendor)
        if isinstance(entry, dict):
            entry["failing"] = f"{_now()} {reason}"[:300]
            _write(target, data)


def delete(settings, vendor: str) -> bool:
    with _locked(settings) as target:
        data = _read(target)
        providers = data.get("providers") or {}
        if vendor not in providers:
            return False
        del providers[vendor]
        _write(target, data)
        return True


def credential(settings, vendor: str):
    """The stored entry as a ProviderCredential, for adapters only — never for prompts, tools or logs."""
    from assistant_kernel.config.settings import ProviderCredential
    with _locked(settings) as target:
        entry = (_read(target).get("providers") or {}).get(vendor)
    return ProviderCredential.model_validate(entry) if isinstance(entry, dict) else None


def probe(vendor: str, api_key: str, *, base_url: str = "", timeout: float = PROBE_TIMEOUT_SECONDS) -> str:
    """Check a key with one cheap real call: "valid", "rejected" (401/403) or "unverified" (anything else, e.g. an
    endpoint without a model list or no network). Only "rejected" stops a key from being stored."""
    import httpx
    url = (base_url or model_catalog.base_url(vendor) or "").rstrip("/")
    if not url:
        return "unverified"
    try:
        response = httpx.get(f"{url}/models", headers={"Authorization": f"Bearer {api_key.strip()}"}, timeout=timeout)
    except httpx.HTTPError:
        return "unverified"
    if response.status_code in (401, 403):
        return "rejected"
    return "valid" if response.is_success else "unverified"


SKIP_WORDS = {"skip", "skipped", "no", "none", "跳过", "不配", "不用", "不需要"}
DEFER_WORDS = {"later", "defer", "deferred", "暂缓", "以后", "稍后", "发布前", "发布前再说", "先不配，发布前再说"}


def apply_setup_answer(settings, spec: dict[str, Any], answer: str) -> str:
    """Turn the user's answer to a `model_setup` question into the note the PM receives, storing the key on the way.

    The answer is a key for the recommended vendor, `<vendor>: <key>` for another catalog vendor,
    `custom_openai: <base_url> <key>` for any OpenAI-compatible endpoint, or a word meaning skip / defer."""
    import re
    text = answer.strip()
    word = text.lower().rstrip(".。!！")
    if word in SKIP_WORDS:
        return "(model setup: skipped by the user)"
    if word in DEFER_WORDS:
        return "(model setup: deferred until release by the user; add defer_until: release to the rows of this vendor)"
    vendor = str(spec.get("recommended_vendor") or "dashscope").strip().lower()
    base_url = ""
    named = re.match(r"^([a-z][a-z0-9_]*)\s*[:：]\s*(\S.*)$", text)
    if named and named.group(1).lower() in model_catalog.VENDORS:
        vendor, text = named.group(1).lower(), named.group(2).strip()
    if (model_catalog.VENDORS.get(vendor) or {}).get("needs_base_url"):
        parts = text.split()
        if len(parts) != 2 or not parts[0].startswith("https://"):
            return f"(model setup: not saved — {vendor} needs '<https base_url> <key>')"
        base_url, text = parts
    if (model_catalog.VENDORS.get(vendor) or {}).get("auth") == "codex_login":
        return f"(model setup: {vendor} uses the Codex login; nothing to store)"
    if vendor not in model_catalog.VENDORS:
        return f"(model setup: not saved — unknown vendor {vendor})"
    result = probe(vendor, text, base_url=base_url)
    if result == "rejected":
        return f"(model setup: rejected by {vendor}; the key was not saved)"
    put(settings, vendor, text, validated=result == "valid", base_url=base_url)
    return f"(model setup: configured {vendor}" + ("" if result == "valid" else "; the key could not be verified yet") + ")"
