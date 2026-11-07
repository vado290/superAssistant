"""Read-only host operations (docs/PC执行端第一版): queue, claim and record queries for a registered PC.

A device is configured by the maintainer in `config/host-devices.json` (token digest, allowed projects, actions).
This module never writes task, run or card state.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid

from jsonschema import Draft202012Validator
from typing import Any

from psycopg.types.json import Jsonb

from assistant_kernel.core.db import Database, all_rows, one

READ_ACTIONS = {
    "obs.status": {"description": "OBS version and recording state", "arguments": {}},
    "obs.scenes": {"description": "OBS scene names", "arguments": {}},
}
for _spec in READ_ACTIONS.values():
    _spec.update(revision="1", effect="observe", input_schema={
        "type": "object", "properties": {}, "additionalProperties": False})
MAX_ARG_BYTES = 4096
TERMINAL = {"succeeded", "failed", "expired", "unknown"}


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def load_devices(settings) -> dict[str, dict[str, Any]]:
    path = settings.data_root / "config" / "host-devices.json"
    if not path.is_file():
        return {}
    data = json.loads(path.read_text("utf-8"))
    return data.get("devices", {}) if isinstance(data, dict) else {}


def authenticate(settings, device_id: str, token: str) -> dict[str, Any]:
    device = load_devices(settings).get(device_id)
    if not token or not device or not hmac.compare_digest(_digest(token or ""), str(device.get("token_sha256", ""))):
        raise PermissionError("unknown device or bad token")
    return device


def allowed_actions(settings, device_id: str, project_id: str) -> dict[str, Any]:
    device = load_devices(settings).get(device_id) or {}
    projects = device.get("projects") or {}
    names = projects.get(project_id) or []
    return {name: READ_ACTIONS[name] for name in names if name in READ_ACTIONS}


async def capabilities(settings, project_id: str) -> dict[str, Any]:
    out = [{"device_id": device_id, "actions": actions} for device_id in load_devices(settings)
           if (actions := allowed_actions(settings, device_id, project_id))]
    return {"devices": out, "note": "queued is not success; poll host_status"}


def validate_arguments(spec, arguments, revision=None):
    if revision is not None and revision != spec["revision"]:
        raise ValueError("unsupported capability_revision")
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    def depth(value, level=0):
        if level > 12:
            raise ValueError("arguments exceed maximum nesting depth")
        for child in (value.values() if isinstance(value, dict) else value if isinstance(value, list) else []):
            depth(child, level + 1)
    depth(arguments)
    raw = json.dumps(arguments, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if len(raw.encode("utf-8")) > MAX_ARG_BYTES:
        raise ValueError("arguments exceed size limit")
    if not Draft202012Validator(spec["input_schema"]).is_valid(arguments):
        raise ValueError("arguments do not match capability input_schema")
    return _digest(raw)


async def authorize(c, project_id, run_id, claim_token, spec):
    row = await one(c, """SELECT r.run_type, r.input, r.task_id, t.stage, t.state,
                                t.state_revision, r.base, r.purpose FROM core.runs r
                         LEFT JOIN core.tasks t ON t.task_id=r.task_id
                         WHERE r.run_id=%s AND r.project_id=%s AND r.claim_token=%s
                           AND r.status='running' AND NOT r.cancel_requested""",
                    (run_id, project_id, claim_token))
    if row is None:
        raise PermissionError("operation requires a live project run lease")
    role = row["run_type"]
    stage = (row["input"] or {}).get("stage")
    if spec["effect"] != "observe":
        if not (role == "pm_turn" and stage == "build" and row["stage"] == "build"
                and row["state"] == "pm" and row["purpose"] == "default"
                and (row["base"] or {}).get("state_revision") == row["state_revision"]):
            raise PermissionError("side effects require the current PM build run")
    return {"run_type": role, "stage": stage, "task_id": str(row["task_id"]) if row["task_id"] else None}


async def submit(settings, *, project_id: str, run_id: str, claim_token: str, owner_key: str, device_id: str,
                 action: str, arguments: dict | None, request_key: str, capability_revision: str | None = None) -> dict[str, Any]:
    arguments = {} if arguments is None else arguments
    if action not in allowed_actions(settings, device_id, project_id):
        raise PermissionError("action is not authorized for this project and device")
    spec = allowed_actions(settings, device_id, project_id)[action]
    args_digest = validate_arguments(spec, arguments, capability_revision)
    if not request_key or len(request_key) > 80:
        raise ValueError("request_key is required (1-80 characters); reuse it when retrying")
    op_id = uuid.uuid4()
    async with Database(settings).tx() as c:
        identity = await authorize(c, project_id, run_id, claim_token, spec)
        row = await one(c, """INSERT INTO core.host_operations(id, project_id, owner_key, run_id, claim_token, device_id,
                                                               action, request_key, arguments, capability_revision, effect, caller_identity, args_digest)
                              VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                              ON CONFLICT (project_id, owner_key, request_key) DO NOTHING RETURNING id""",
                        (op_id, project_id, owner_key, run_id, claim_token, device_id, action, request_key, Jsonb(arguments), spec["revision"], spec["effect"], Jsonb(identity), args_digest))
        if row is None:
            row = await one(c, "SELECT id, device_id, action, state, arguments, capability_revision FROM core.host_operations WHERE project_id=%s AND owner_key=%s "
                               "AND request_key=%s", (project_id, owner_key, request_key))
            if (row["device_id"] != device_id or row["action"] != action
                    or row["arguments"] != arguments or row["capability_revision"] != spec["revision"]):
                raise ValueError("request_key was already used for a different request")
            return {"operation_id": str(row["id"]), "state": row["state"], "reused": True}
    return {"operation_id": str(op_id), "state": "queued"}


async def status(settings, *, project_id: str, operation_id: str) -> dict[str, Any]:
    async with Database(settings).tx() as c:
        await c.execute("UPDATE core.host_operations SET state='unknown' WHERE state='running' AND expires_at < now()")
        await c.execute("UPDATE core.host_operations SET state='expired', ended_at=now() "
                        "WHERE state='queued' AND expires_at < now()")
        row = await one(c, "SELECT id, device_id, action, state, result, created_at, started_at, ended_at "
                           "FROM core.host_operations WHERE id=%s AND project_id=%s", (operation_id, project_id))
    if row is None:
        raise KeyError("unknown operation")
    row["operation_id"] = str(row.pop("id"))
    return {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in row.items()}


async def claim(settings, device_id: str) -> dict[str, Any] | None:
    """Hand one queued operation to the device whose project, run lease and action policy are still valid."""
    async with Database(settings).tx() as c:
        await c.execute("UPDATE core.host_operations SET state='unknown' WHERE state='running' AND expires_at < now()")
        await c.execute("UPDATE core.host_operations SET state='expired', ended_at=now() "
                        "WHERE state='queued' AND expires_at < now()")
        for row in await all_rows(c, """SELECT o.* FROM core.host_operations o JOIN core.runs r ON r.run_id=o.run_id
                                        WHERE o.device_id=%s AND o.state='queued' AND r.status='running'
                                          AND r.claim_token=o.claim_token AND NOT r.cancel_requested
                                        ORDER BY o.created_at LIMIT 5 FOR UPDATE OF o SKIP LOCKED""", (device_id,)):
            if row["action"] not in allowed_actions(settings, device_id, row["project_id"]):
                await c.execute("UPDATE core.host_operations SET state='failed', ended_at=now(), result=%s WHERE id=%s",
                                (Jsonb({"error": {"code": "NOT_AUTHORIZED"}}), row["id"]))
                continue
            spec = allowed_actions(settings, device_id, row["project_id"])[row["action"]]
            try:
                validate_arguments(spec, row["arguments"], row["capability_revision"])
                identity = await authorize(c, row["project_id"], row["run_id"], row["claim_token"], spec)
                if row["effect"] != spec["effect"] or (row["caller_identity"] and row["caller_identity"] != identity):
                    raise PermissionError("capability or caller changed")
            except (ValueError, PermissionError):
                await c.execute("UPDATE core.host_operations SET state='failed', ended_at=now(), result=%s WHERE id=%s",
                                (Jsonb({"error": {"code": "POLICY_CHANGED"}}), row["id"]))
                continue
            token = secrets.token_urlsafe(24)
            await c.execute("UPDATE core.host_operations SET state='running', started_at=now(), dispatch_digest=%s "
                            "WHERE id=%s", (_digest(token), row["id"]))
            return {"operation_id": str(row["id"]), "project_id": row["project_id"], "run_id": str(row["run_id"]), "action": row["action"], "arguments": row["arguments"], "capability_revision": row["capability_revision"],
                    "effect": spec["effect"], "caller_identity": identity, "operation_token": token}
    return None


async def report(settings, device_id: str, operation_id: str, token: str, state: str, result: dict) -> dict[str, Any]:
    if state not in ("succeeded", "failed"):
        raise ValueError("state must be succeeded or failed")
    async with Database(settings).tx() as c:
        row = await one(c, "SELECT state, dispatch_digest FROM core.host_operations WHERE id=%s AND device_id=%s "
                           "FOR UPDATE", (operation_id, device_id))
        if row is None or not hmac.compare_digest(_digest(token or ""), row["dispatch_digest"] or ""):
            raise PermissionError("bad operation token")
        if row["state"] in TERMINAL - {"unknown"}:
            return {"state": row["state"]}
        await c.execute("UPDATE core.host_operations SET state=%s, result=%s, ended_at=now() WHERE id=%s",
                        (state, Jsonb(result), operation_id))
    return {"state": state}


MAX_ARTIFACT_BYTES = 16 * 1024 * 1024


async def import_artifact(settings, device_id, operation_id, token, content, digest):
    """Import bounded bytes into the operation's project; no caller-selected paths."""
    import asyncio
    from pathlib import Path
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_ARTIFACT_BYTES:
        raise ValueError("artifact size out of bounds")
    actual = hashlib.sha256(content).hexdigest()
    if not hmac.compare_digest(actual, digest):
        raise ValueError("artifact checksum mismatch")
    async with Database(settings).tx() as c:
        row = await one(c, "SELECT project_id, dispatch_digest FROM core.host_operations "
                          "WHERE id=%s AND device_id=%s", (operation_id, device_id))
    if row is None or not hmac.compare_digest(_digest(token or ""), row["dispatch_digest"] or ""):
        raise PermissionError("bad operation token")
    project_root = (settings.data_root / "vaults" / row["project_id"]).resolve()
    relative = Path("runs") / "host-artifacts" / str(uuid.UUID(operation_id)) / actual
    target = project_root / relative
    def store():
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.resolve().is_relative_to(project_root):
            raise PermissionError("artifact path escapes project")
        # Exclusive creation makes identical retries idempotent.
        try:
            with target.open("xb") as stream:
                stream.write(content)
        except FileExistsError:
            if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != actual:
                raise ValueError("stored artifact integrity mismatch")
    await asyncio.to_thread(store)
    return {"operation_id": operation_id, "project_id": row["project_id"],
            "path": relative.as_posix(), "sha256": actual, "size": len(content)}
