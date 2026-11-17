"""Project-level cross-project access facts and read-only policy rules.

It deliberately keeps this module independent from model tools and gateway cards.
A policy grants only bounded business reads; it never grants filesystem access,
message writes, routing, task dispatch, publishing, or directory grants.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Iterable
from contextlib import asynccontextmanager

SCHEMA_VERSION = "project-access.v1"
POLICY_KEYS = frozenset({
    "schema_version", "mode", "targets", "excluded", "include_future", "read_scopes", "expires_at",
    "supplemental",
})
SUPPLEMENT_KEYS = frozenset({"request_id", "targets", "read_scopes", "expires_at"})
MODES = frozenset({"self", "specific", "all", "all_except"})
READ_SCOPES = ("catalog", "summary", "tasks", "history", "documents")
READ_SCOPE_SET = frozenset(READ_SCOPES)
DOCUMENT_SCOPE_KINDS = frozenset({
    "task_contract", "solution_design", "implementation_report", "review_report",
    "improvement_proposal", "domain_brief",
})
PROJECT_ID = re.compile(r"[a-z][a-z0-9-]{2,39}")


class AccessPolicyError(ValueError):
    """An invalid or unsafe project access policy."""


class AccessRevisionConflict(RuntimeError):
    """The policy changed since the caller read its expected revision."""


def _utc(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise AccessPolicyError("expires_at must be an ISO-8601 string or null")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        result = datetime.fromisoformat(text)
    except ValueError as exc:
        raise AccessPolicyError("expires_at must be a valid ISO-8601 timestamp") from exc
    if result.tzinfo is None:
        raise AccessPolicyError("expires_at must include a timezone")
    return result.astimezone(timezone.utc).replace(microsecond=0)


def _ids(value: Any, field: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple, set)):
        raise AccessPolicyError(f"{field} must be a list")
    result = sorted({str(item).strip() for item in value if str(item).strip()})
    invalid = [item for item in result if not PROJECT_ID.fullmatch(item)]
    if invalid:
        raise AccessPolicyError(f"{field} contains invalid project id: {invalid[0]}")
    return result


def _scopes(value: Any) -> list[str]:
    if value is None:
        # Callers can narrow the bounded business read views.
        return sorted(READ_SCOPES)
    if not isinstance(value, (list, tuple, set)):
        raise AccessPolicyError("read_scopes must be a list")
    result = sorted({str(item).strip() for item in value if str(item).strip()})
    unknown = sorted(set(result) - READ_SCOPE_SET)
    if unknown:
        raise AccessPolicyError(f"unknown read scope: {unknown[0]}")
    return result


def _supplemental(value: Any, source_project_id: str | None) -> list[dict[str, Any]]:
    if value in (None, []):
        return []
    if not isinstance(value, list):
        raise AccessPolicyError("supplemental must be a list")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - SUPPLEMENT_KEYS:
            raise AccessPolicyError("supplemental item has unknown fields")
        request_id = str(item.get("request_id") or "").strip()
        if not request_id or request_id in seen:
            raise AccessPolicyError("supplemental request_id must be unique and non-empty")
        seen.add(request_id)
        targets = _ids(item.get("targets"), "supplemental.targets")
        if source_project_id in targets:
            raise AccessPolicyError("supplemental cannot include its source project")
        if not targets:
            raise AccessPolicyError("supplemental needs at least one target")
        expires = _utc(item.get("expires_at"))
        if expires is None:
            raise AccessPolicyError("supplemental authorization must expire")
        result.append({"request_id": request_id, "targets": targets,
                       "read_scopes": _scopes(item.get("read_scopes")),
                       "expires_at": expires.isoformat().replace("+00:00", "Z")})
    return sorted(result, key=lambda item: item["request_id"])


def normalize_envelope(raw: dict[str, Any] | None, *, source_project_id: str | None = None,
                       frozen_targets: Iterable[str] = (), allow_expired: bool = False) -> tuple[dict[str, Any], str]:
    """Validate and canonicalize a project-level read-only envelope.

    ``targets`` is the frozen snapshot for all/all_except policies. Future projects
    are considered only when include_future is true and the caller supplies them to
    ``policy_allows``. No write capability can be represented by this schema.
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise AccessPolicyError("access policy must be an object")
    unknown = sorted(set(raw) - POLICY_KEYS)
    if unknown:
        raise AccessPolicyError(f"unknown access policy key: {unknown[0]}")
    if raw.get("schema_version", SCHEMA_VERSION) != SCHEMA_VERSION:
        raise AccessPolicyError("unsupported access policy schema")
    if "include_future" in raw and not isinstance(raw["include_future"], bool):
        raise AccessPolicyError("include_future must be a boolean")
    mode = str(raw.get("mode") or "self").strip()
    if mode not in MODES:
        raise AccessPolicyError(f"mode must be one of {', '.join(sorted(MODES))}")
    source = str(source_project_id or "").strip() or None
    targets = _ids(raw.get("targets"), "targets")
    excluded = _ids(raw.get("excluded"), "excluded")
    snapshot = _ids(frozen_targets, "frozen_targets") if mode in {"all", "all_except"} else []
    include_future = bool(raw.get("include_future", False))
    if mode == "self":
        if targets or excluded or include_future:
            raise AccessPolicyError("self policy cannot contain targets, exclusions or future projects")
        snapshot = []
    elif mode == "specific":
        if not targets:
            raise AccessPolicyError("specific policy needs at least one target")
        if excluded or include_future:
            raise AccessPolicyError("specific policy cannot contain exclusions or future projects")
        snapshot = targets
    else:
        # For all modes, targets are the approved/frozen current set. Empty is valid.
        if mode == "all" and excluded:
            raise AccessPolicyError("all policy cannot contain excluded projects; use all_except")
        if mode == "all_except" and not excluded:
            raise AccessPolicyError("all_except policy needs at least one excluded project")
        if not snapshot:
            snapshot = targets
        if source and source in snapshot:
            snapshot.remove(source)
        if source and source in excluded:
            excluded.remove(source)
    if source and source in targets:
        targets = [item for item in targets if item != source]
    if mode == "specific" and not targets:
        raise AccessPolicyError("specific policy needs another project")
    expires = _utc(raw.get("expires_at"))
    if expires is not None and expires <= datetime.now(timezone.utc) and not allow_expired:
        raise AccessPolicyError("expires_at must be in the future")
    canonical = {
        "schema_version": SCHEMA_VERSION,
        "mode": mode,
        "targets": snapshot if mode in {"all", "all_except"} else targets,
        "excluded": excluded if mode == "all_except" else [],
        "include_future": include_future if mode in {"all", "all_except"} else False,
        "read_scopes": _scopes(raw.get("read_scopes")),
        "expires_at": expires.isoformat().replace("+00:00", "Z") if expires else None,
        "supplemental": _supplemental(raw.get("supplemental"), source),
    }
    return canonical, policy_sha256(canonical)


def policy_sha256(policy: dict[str, Any]) -> str:
    encoded = json.dumps(policy, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def policy_allows(policy: dict[str, Any], source_project_id: str, target_project_id: str,
                  read_scope: str, *, eligible_targets: Iterable[str] = (), now: datetime | None = None) -> bool:
    """Return whether one bounded read is allowed; never follows target references transitively."""
    if read_scope not in READ_SCOPE_SET:
        return False
    if source_project_id == target_project_id:
        return True
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    excluded = set(policy.get("excluded") or ())
    if target_project_id in excluded:
        return False

    mode = policy.get("mode")
    targets = set(policy.get("targets") or ())
    target_match = (target_project_id in targets or
                    (mode in {"all", "all_except"} and bool(policy.get("include_future"))
                     and target_project_id in set(eligible_targets)))
    base_expires = _utc(policy.get("expires_at"))
    base_allowed = (target_match and read_scope in set(policy.get("read_scopes") or ())
                    and (base_expires is None or current < base_expires))
    if base_allowed:
        return True

    for supplement in policy.get("supplemental") or ():
        if target_project_id in set(supplement.get("targets") or ()):
            expires = _utc(supplement.get("expires_at"))
            if expires and current < expires and read_scope in set(supplement.get("read_scopes") or ()):
                return True
    return False


def revision_from_row(row: dict[str, Any] | None) -> int:
    return int(row["revision"]) if row else 0


def request_semantic_sha(*, request_kind: str, targets: Iterable[str], read_scopes: Iterable[str],
                         expires_at: str, reason: str) -> str:
    payload = {"request_kind": request_kind, "targets": sorted(set(targets)),
               "read_scopes": sorted(set(read_scopes)), "expires_at": expires_at,
               "reason": reason.strip()[:1000]}
    return policy_sha256(payload)


def _request_values(raw: dict[str, Any], *, allow_expired: bool = False) -> tuple[str, list[str], list[str], str, str, str]:
    if not isinstance(raw, dict):
        raise AccessPolicyError("access request must be an object")
    kind = str(raw.get("request_kind") or "selected_read")
    if kind != "selected_read":
        raise AccessPolicyError("only selected_read requests are enabled for agent calls")
    targets = _ids(raw.get("targets"), "targets")
    if not targets:
        raise AccessPolicyError("an access request needs at least one target")
    scopes = _scopes(raw.get("read_scopes"))
    if not scopes:
        raise AccessPolicyError("an access request needs a read scope")
    expires = _utc(raw.get("expires_at"))
    if expires is None:
        raise AccessPolicyError("an access request must expire")
    if expires <= datetime.now(timezone.utc) and not allow_expired:
        raise AccessPolicyError("an access request must expire in the future")
    reason = str(raw.get("reason") or "").strip()[:1000]
    if not reason:
        raise AccessPolicyError("an access request needs a reason")
    return kind, targets, scopes, expires.isoformat().replace("+00:00", "Z"), reason, request_semantic_sha(
        request_kind=kind, targets=targets, read_scopes=scopes,
        expires_at=expires.isoformat().replace("+00:00", "Z"), reason=reason)


async def validate_specific_targets(c, envelope: dict[str, Any]) -> None:
    """Validate explicit grants at approval/write time, not during pure parsing."""
    if envelope["mode"] != "specific":
        return
    from assistant_kernel.core.db import all_rows
    targets = set(envelope["targets"])
    rows = await all_rows(c, """SELECT p.project_id FROM core.projects p
        JOIN core.project_lifecycle l USING (project_id)
        WHERE p.project_id = ANY(%s) AND p.status='active' AND p.kind='business'
          AND l.lifecycle_state='ready'""", (sorted(targets),))
    if {row["project_id"] for row in rows} != targets:
        raise AccessPolicyError("specific targets must be existing active, ready business projects")


async def write_policy_in_transaction(c, source_project_id: str, expected_revision: int,
                                      raw_envelope: dict[str, Any] | None, *, origin: str,
                                      approval_ref: str | None = None, provenance: dict[str, Any] | None = None,
                                      frozen_targets: Iterable[str] = (), allow_expired: bool = False) -> dict[str, Any]:
    """Write one policy revision; the caller owns the transaction and source project lock."""
    from psycopg.types.json import Jsonb
    from assistant_kernel.core.db import one
    if origin not in {"user", "creation", "request", "legacy"}:
        raise AccessPolicyError("invalid policy origin")
    envelope, digest = normalize_envelope(raw_envelope, source_project_id=source_project_id,
                                          frozen_targets=frozen_targets, allow_expired=allow_expired)
    current = await one(c, """SELECT revision FROM core.project_access_policies
        WHERE source_project_id=%s AND is_current FOR UPDATE""", (source_project_id,))
    actual = revision_from_row(current)
    if actual != expected_revision:
        raise AccessRevisionConflict(f"expected revision {expected_revision}, current is {actual}")
    if origin in {"user", "creation"}:
        await validate_specific_targets(c, envelope)
    await c.execute("""UPDATE core.project_access_policies SET is_current=false, superseded_at=now()
        WHERE source_project_id=%s AND is_current""", (source_project_id,))
    row = await one(c, """INSERT INTO core.project_access_policies
        (source_project_id, revision, is_current, envelope_json, envelope_sha256, expires_at,
         origin, approval_ref, provenance)
        VALUES (%s, %s, true, %s, %s, %s, %s, %s, %s)
        RETURNING source_project_id, revision, envelope_json, envelope_sha256, expires_at,
                  origin, approval_ref, provenance, created_at""",
        (source_project_id, actual + 1, Jsonb(envelope), digest, _utc(envelope["expires_at"]),
         origin, approval_ref, Jsonb(provenance or {})))
    return {**row, "revision": int(row["revision"]), "envelope": dict(row["envelope_json"])}


async def create_request_in_transaction(c, source_project_id: str, raw: dict[str, Any],
                                        *, idempotency_key: str, source_run_id=None,
                                        source_task_id=None) -> tuple[dict[str, Any], bool]:
    """Create or replay one pending request; caller owns the source project lock."""
    from psycopg.types.json import Jsonb
    from assistant_kernel.core.db import one
    if not idempotency_key or len(idempotency_key) > 200:
        raise AccessPolicyError("idempotency_key is required and must be short")
    kind, targets, scopes, expires, reason, semantic = _request_values(raw, allow_expired=True)
    if source_project_id in targets:
        raise AccessPolicyError("an access request cannot include its source project")
    if not targets:
        raise AccessPolicyError("a project cannot request access to itself")
    existing = await one(c, """SELECT * FROM core.project_access_requests
                              WHERE source_project_id=%s AND idempotency_key=%s FOR UPDATE""",
                         (source_project_id, idempotency_key))
    if existing:
        if existing["request_semantic_sha256"] != semantic:
            raise AccessPolicyError("idempotency key was already used for a different request")
        return existing, True
    # Terminalize old requests before applying the pending quota / semantic uniqueness.
    await c.execute("""UPDATE core.project_access_requests SET status='expired', resolved_at=now(), terminal_reason='expired'
        WHERE source_project_id=%s AND status='pending' AND candidate_expires_at <= clock_timestamp()""", (source_project_id,))
    duplicate = await one(c, """SELECT * FROM core.project_access_requests WHERE source_project_id=%s
        AND request_semantic_sha256=%s AND status='pending'""", (source_project_id, semantic))
    if duplicate:
        return duplicate, True
    pending = await one(c, "SELECT count(*) AS n FROM core.project_access_requests WHERE source_project_id=%s AND status='pending'", (source_project_id,))
    if pending["n"] >= 20:
        raise AccessPolicyError("too many pending access requests")
    if _utc(expires) <= datetime.now(timezone.utc):
        raise AccessPolicyError("an access request must expire in the future")
    if source_run_id is not None:
        run = await one(c, "SELECT project_id FROM core.runs WHERE run_id=%s AND project_id=%s",
                        (source_run_id, source_project_id))
        if run is None:
            raise AccessPolicyError("source_run_id does not belong to the source project")
    if source_task_id is not None:
        task = await one(c, "SELECT project_id FROM core.tasks WHERE task_id=%s AND project_id=%s",
                         (source_task_id, source_project_id))
        if task is None:
            raise AccessPolicyError("source_task_id does not belong to the source project")
    policy = await one(c, "SELECT revision FROM core.project_access_policies WHERE source_project_id=%s AND is_current", (source_project_id,))
    row = await one(c, """INSERT INTO core.project_access_requests
        (source_project_id, source_run_id, source_task_id, request_kind, target_projects, read_scopes,
         reason, idempotency_key, request_semantic_sha256, candidate_expires_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING *""", (source_project_id, source_run_id, source_task_id, kind, Jsonb(targets),
                          Jsonb(scopes), reason, idempotency_key, semantic, _utc(expires)))
    await c.execute("UPDATE core.project_access_requests SET notice_json=%s WHERE request_id=%s",
                    (Jsonb({"base_revision": revision_from_row(policy)}), row["request_id"]))
    return row, False


async def resolve_request_in_transaction(c, request_id: str, decision: str, *, approval_ref: str | None = None,
                                         reason: str | None = None) -> dict[str, Any]:
    """Approve by appending one expiring supplement, or terminally reject/expire it."""
    from assistant_kernel.core.db import lock_project, one
    identity = await one(c, "SELECT source_project_id FROM core.project_access_requests WHERE request_id=%s", (request_id,))
    if identity is None:
        raise KeyError(f"unknown access request {request_id}")
    source = str(identity["source_project_id"])
    # All request creation and policy replacement paths lock source_project first.
    await lock_project(c, source)
    request = await one(c, "SELECT * FROM core.project_access_requests WHERE request_id=%s FOR UPDATE", (request_id,))
    if request is None:
        raise KeyError(f"unknown access request {request_id}")
    if request["status"] != "pending":
        raise AccessRevisionConflict(f"access request is already {request['status']}")
    if decision in {"reject", "withdraw", "expire"}:
        status = {"reject": "rejected", "withdraw": "withdrawn", "expire": "expired"}[decision]
        row = await one(c, """UPDATE core.project_access_requests
                              SET status=%s, terminal_reason=%s, resolved_at=now()
                            WHERE request_id=%s RETURNING *""", (status, reason or status, request_id))
        return row
    if decision != "approve":
        raise AccessPolicyError("decision must be approve, reject, withdraw or expire")
    eligible = await one(c, """SELECT count(*) AS n FROM core.projects p
        JOIN core.project_lifecycle l USING(project_id)
        WHERE p.project_id = ANY(%s) AND p.status='active' AND p.kind='business'
          AND l.lifecycle_state='ready'""", (list(request["target_projects"]),))
    expired = await one(c, "SELECT %s::timestamptz <= clock_timestamp() AS expired", (request["candidate_expires_at"],))
    if expired["expired"] or int(eligible["n"]) != len(request["target_projects"]):
        status = "expired" if expired["expired"] else "stale"
        return await one(c, """UPDATE core.project_access_requests SET status=%s,
            terminal_reason=%s, resolved_at=now() WHERE request_id=%s RETURNING *""",
            (status, status, request_id))
    current = await one(c, """SELECT * FROM core.project_access_policies
                            WHERE source_project_id=%s AND is_current FOR UPDATE""", (source,))
    if revision_from_row(current) != int((request["notice_json"] or {}).get("base_revision", -1)):
        return await one(c, """UPDATE core.project_access_requests SET status='stale', terminal_reason='policy changed',
            resolved_at=now() WHERE request_id=%s RETURNING *""", (request_id,))
    if current:
        if policy_sha256(current["envelope_json"]) != current["envelope_sha256"]:
            raise AccessPolicyError("current policy hash is invalid")
        envelope = dict(current["envelope_json"] or {})
        expected_revision = int(current["revision"])
    else:
        envelope, _ = normalize_envelope({}, source_project_id=source)
        expected_revision = 0
    supplement = {"request_id": str(request["request_id"]),
                  "targets": sorted(set(str(x) for x in request["target_projects"])),
                  "read_scopes": sorted(set(str(x) for x in request["read_scopes"])),
                  "expires_at": request["candidate_expires_at"].astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")}
    envelope["supplemental"] = [item for item in envelope.get("supplemental") or []
                                 if item.get("request_id") != supplement["request_id"]] + [supplement]
    policy = await write_policy_in_transaction(c, source, expected_revision, envelope, origin="request",
        approval_ref=approval_ref, provenance={"request_id": str(request["request_id"]),
                                               "base_revision": expected_revision}, allow_expired=True)
    revision = policy["revision"]
    row = await one(c, """UPDATE core.project_access_requests
                          SET status='approved', approval_ref=%s, approved_revision=%s, resolved_at=now()
                        WHERE request_id=%s RETURNING *""", (approval_ref, revision, request_id))
    return row


class ProjectAccessService:
    """Database access for policy facts; callers still decide when to expose tools."""

    def __init__(self, settings):
        from assistant_kernel.core.db import Database
        self.settings = settings
        self.db = Database(settings)

    def require_enabled(self) -> None:
        if not self.settings.project_access_enabled:
            raise PermissionError("cross-project access rollout is disabled")

    async def current(self, source_project_id: str) -> dict[str, Any]:
        self.require_enabled()
        from assistant_kernel.core.db import one
        async with self.db.tx() as c:
            row = await one(c, """SELECT source_project_id, revision, envelope_json, envelope_sha256,
                                      expires_at, origin, approval_ref, provenance, created_at, superseded_at
                                 FROM core.project_access_policies
                                WHERE source_project_id=%s AND is_current""", (source_project_id,))
        if row is None:
            return {"source_project_id": source_project_id, "revision": 0, "envelope": None, "envelope_sha256": None}
        envelope = dict(row["envelope_json"] or {})
        if policy_sha256(envelope) != row["envelope_sha256"]:
            raise AccessPolicyError(f"access policy hash differs at revision {row['revision']}")
        return {**row, "revision": int(row["revision"]), "envelope": envelope}

    async def replace(self, source_project_id: str, expected_revision: int, raw_envelope: dict[str, Any] | None,
                      *, origin: str = "user", approval_ref: str | None = None,
                      provenance: dict[str, Any] | None = None,
                      frozen_targets: Iterable[str] = ()) -> dict[str, Any]:
        self.require_enabled()
        from assistant_kernel.core.db import lock_project
        async with self.db.tx() as c:
            await lock_project(c, source_project_id)
            return await write_policy_in_transaction(c, source_project_id, expected_revision, raw_envelope,
                origin=origin, approval_ref=approval_ref, provenance=provenance, frozen_targets=frozen_targets)

    async def discoverable_projects(self, source_project_id: str) -> list[dict[str, Any]]:
        """Minimal directory only: no charter, messages, tasks, files or settings."""
        self.require_enabled()
        from assistant_kernel.core.db import all_rows
        async with self.db.tx() as c:
            return await all_rows(c, """SELECT p.project_id, p.settings->>'display_name' AS display_name,
                                                p.kind, l.lifecycle_state
                                           FROM core.projects p
                                           JOIN core.project_lifecycle l USING (project_id)
                                          WHERE p.status='active' AND p.kind='business'
                                            AND l.lifecycle_state='ready'
                                          ORDER BY p.project_id""", ())

    async def accessible_project_ids(self, source_project_id: str, read_scope: str) -> list[str]:
        current = await self.current(source_project_id)
        if read_scope not in READ_SCOPE_SET:
            raise AccessPolicyError("unknown read category")
        projects = await self.discoverable_projects(source_project_id)
        eligible = {str(row["project_id"]) for row in projects}
        policy = current.get("envelope")
        if not policy:
            return [source_project_id] if read_scope in READ_SCOPE_SET else []
        return sorted({source_project_id} | {
            project_id for project_id in eligible
            if policy_allows(policy, source_project_id, project_id, read_scope, eligible_targets=eligible)
        })

    async def _authorize(self, c, source: str, target: str, scope: str) -> int:
        from assistant_kernel.core.db import one
        if scope not in READ_SCOPE_SET:
            raise PermissionError("unknown read category")
        eligible = await one(c, """SELECT 1 FROM core.projects p JOIN core.project_lifecycle l USING(project_id)
            WHERE p.project_id=%s AND p.status='active' AND p.kind='business' AND l.lifecycle_state='ready'""", (target,))
        if eligible is None:
            raise PermissionError("target is not an active, ready business project")
        row = await one(c, "SELECT revision, envelope_json, envelope_sha256 FROM core.project_access_policies WHERE source_project_id=%s AND is_current", (source,))
        if source == target:
            return int(row["revision"]) if row else 0
        if row is None or policy_sha256(row["envelope_json"]) != row["envelope_sha256"]:
            raise PermissionError("no valid project policy")
        clock = await one(c, "SELECT clock_timestamp() AS at")
        if not policy_allows(row["envelope_json"], source, target, scope, eligible_targets={target}, now=clock["at"]):
            raise PermissionError("project read is not authorized")
        return int(row["revision"])

    @asynccontextmanager
    async def _read(self, source: str, target: str, scope: str, *, run_id=None, claim_token=None):
        self.require_enabled()
        from assistant_kernel.core.db import all_rows, one, event
        async with self.db.tx() as c:
            # Ordered shared locks serialize policy replacement and target archival with this short read.
            projects = await all_rows(c, "SELECT project_id FROM core.projects WHERE project_id = ANY(%s) ORDER BY project_id FOR SHARE", (sorted({source, target}),))
            if len(projects) != len({source, target}):
                raise PermissionError("unknown source or target")
            async def lease():
                if run_id is None and claim_token is None:
                    return  # trusted service API; model tools always supply both
                held = await one(c, """SELECT 1 FROM core.runs WHERE run_id=%s AND project_id=%s
                    AND claim_token=%s AND status='running' AND NOT cancel_requested
                    AND lease_until > clock_timestamp()""", (run_id, source, claim_token))
                if held is None:
                    raise PermissionError("access invocation lease lost")
            await lease()
            revision = await self._authorize(c, source, target, scope)
            yield c
            await lease()
            await self._authorize(c, source, target, scope)
            await event(c, source, "project_access_read", target_project_id=target,
                        read_scope=scope, policy_revision=revision, source_run_id=str(run_id) if run_id else None)

    async def portfolio_snapshot(self, source_project_id: str, *, limit: int = 20,
                                 cursor: str | None = None, run_id=None, claim_token=None) -> dict[str, Any]:
        """Current authorized catalog/summary page, not an atomic historical or live-health snapshot.

        A cursor locates the next id and binds the policy revision; every page rechecks
        expiry, readiness and the invocation lease. No background, runtime or error text.
        """
        import base64
        from assistant_kernel.core.db import all_rows, one, event
        self.require_enabled()
        limit = min(max(int(limit), 1), 20)
        after = ""
        continuation = None
        if cursor:
            try:
                if len(cursor) > 2048:
                    raise ValueError
                continuation = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if (set(continuation) != {"source", "revision", "after"}
                        or continuation["source"] != source_project_id
                        or type(continuation["revision"]) is not int
                        or not isinstance(continuation["after"], str)
                        or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", continuation["after"])):
                    raise ValueError
                after = continuation["after"]
            except (ValueError, TypeError, KeyError, AttributeError) as exc:
                raise ValueError("invalid portfolio cursor; query the first page again") from exc
        async with self.db.tx() as c:
            # Shared project/lifecycle locks also serialize archival and policy replacement.
            projects = await all_rows(c, """SELECT p.project_id, p.kind, p.status, l.lifecycle_state
                FROM core.projects p JOIN core.project_lifecycle l USING(project_id)
                WHERE p.project_id=%s OR (p.status='active' AND p.kind='business' AND l.lifecycle_state='ready')
                ORDER BY p.project_id FOR SHARE OF p,l""", (source_project_id,))
            source = next((p for p in projects if p["project_id"] == source_project_id), None)
            if source is None or source["status"] != "active" or source["lifecycle_state"] != "ready":
                raise PermissionError("source is not an active, ready project")
            async def lease():
                if run_id is None and claim_token is None:
                    return
                held = await one(c, """SELECT 1 FROM core.runs WHERE run_id=%s AND project_id=%s
                    AND claim_token=%s AND status='running' AND NOT cancel_requested
                    AND lease_until > clock_timestamp()""", (run_id, source_project_id, claim_token))
                if held is None:
                    raise PermissionError("access invocation lease lost")
            await lease()
            row = await one(c, """SELECT revision,envelope_json,envelope_sha256 FROM core.project_access_policies
                WHERE source_project_id=%s AND is_current""", (source_project_id,))
            revision = int(row["revision"]) if row else 0
            policy = row["envelope_json"] if row else {}
            if row and policy_sha256(policy) != row["envelope_sha256"]:
                raise AccessPolicyError("access policy hash differs")
            if continuation and continuation["revision"] != revision:
                raise AccessRevisionConflict("portfolio policy changed; query the first page again")
            clock = (await one(c, "SELECT clock_timestamp() AS at"))["at"]
            eligible = [p["project_id"] for p in projects if p["project_id"] != source_project_id
                        and p["kind"] == "business" and p["status"] == "active" and p["lifecycle_state"] == "ready"]
            allowed = {target: [scope for scope in ("catalog", "summary") if policy_allows(
                policy, source_project_id, target, scope, eligible_targets=eligible, now=clock)] for target in eligible}
            authorized = [target for target in eligible if allowed[target]]
            remaining = [target for target in authorized if target > after]
            page = remaining[:limit]
            items = []
            for target in page:
                entry = {"project_id": target, "read_scopes": allowed[target],
                         "omitted_scopes": [s for s in ("catalog", "summary") if s not in allowed[target]]}
                if "catalog" in allowed[target]:
                    entry["catalog"] = self._catalog(target)
                if "summary" in allowed[target]:
                    entry["summary"] = await self._summary(c, target)
                items.append(entry)
            # Time-based expiry can change without a new policy revision. Discard the page then.
            end = (await one(c, "SELECT clock_timestamp() AS at"))["at"]
            for target in page:
                for scope in allowed[target]:
                    if not policy_allows(policy, source_project_id, target, scope, eligible_targets=eligible, now=end):
                        raise PermissionError("portfolio authorization expired during read; query again")
            await lease()
            await event(c, source_project_id, "project_access_portfolio", policy_revision=revision,
                        targets=page, source_run_id=str(run_id) if run_id else None)
        truncated = len(remaining) > limit
        next_cursor = base64.urlsafe_b64encode(json.dumps({"source": source_project_id,
            "revision": revision, "after": page[-1]}).encode()).decode() if truncated else None
        return {"observed_at": clock.isoformat(), "policy_revision": revision, "items": items,
                "next_cursor": next_cursor, "truncated": truncated,
                "coverage": {"eligible_projects": len(eligible), "authorized_projects": len(authorized),
                    "returned_projects": len(items), "remaining_projects": max(len(remaining)-limit, 0),
                    "omitted_unauthorized_projects": len(eligible)-len(authorized),
                    "scope": "active ready business projects; catalog and summary only",
                    "complete": not after and not truncated and len(authorized) == len(eligible)
                        and all(len(allowed[t]) == 2 for t in eligible)},
                "limitations": ["Each page is a current read; earlier pages may have changed.",
                    "Kernel, CoS, archived and non-ready projects are outside this portfolio.",
                    "No background, runtime files, credentials, live health or business-effect verification."]}

    def _catalog(self, target_project_id: str) -> dict[str, Any]:
        """Call only while the target catalog read is authorized and locked."""
        from assistant_kernel.core import skills
        vaults = (self.settings.data_root / "vaults").resolve()
        charter = vaults / target_project_id / "knowledge" / "charter.md"
        if not charter.resolve().is_relative_to(vaults / target_project_id) or (vaults / target_project_id).is_symlink():
            raise PermissionError("charter escapes the target project")
        text = ""
        if charter.is_file():
            with charter.open("rb") as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                raise ValueError("public charter exceeds 64 KiB")
            text = raw.decode("utf-8")
        return {"project_id": target_project_id,
                **{key: skills.section_text(text, key)[:8000] for key in ("purpose", "scope", "routing")}}

    async def _summary(self, c, target_project_id: str) -> dict[str, Any]:
        from assistant_kernel.core.db import one
        row = await one(c, """SELECT count(*) FILTER (WHERE state NOT IN ('completed', 'cancelled', 'failed')) AS active_tasks,
            count(*) FILTER (WHERE state='completed') AS completed_tasks FROM core.tasks WHERE project_id=%s""", (target_project_id,))
        return {"project_id": target_project_id, **{key: int(value) for key, value in row.items()}}

    async def read_project_catalog(self, source_project_id: str, target_project_id: str, **identity) -> dict[str, Any]:
        """Bounded public charter sections; no arbitrary file paths."""
        async with self._read(source_project_id, target_project_id, "catalog", **identity):
            return self._catalog(target_project_id)

    async def read_project_summary(self, source_project_id: str, target_project_id: str, **identity) -> dict[str, Any]:
        async with self._read(source_project_id, target_project_id, "summary", **identity) as c:
            return await self._summary(c, target_project_id)

    async def read_project_history(self, source_project_id: str, target_project_id: str, *, lookback_days: int = 365,
                                   limit: int = 50, cursor: str | None = None, **identity) -> dict[str, Any]:
        """Read bounded business conversation history without exposing control or secret data."""
        from assistant_kernel.core.db import all_rows
        from assistant_kernel.core.kernel import _page_context, _page_result
        days = min(max(int(lookback_days), 1), 365)
        limit = min(max(int(limit), 1), 100)
        filters = {"target": target_project_id, "days": days}
        context = _page_context(cursor, source_project_id, "project_access_history", filters)
        boundary = "AND (created_at, message_id) < (%s::timestamptz, %s::uuid)" if context["after"] else ""
        async with self._read(source_project_id, target_project_id, "history", **identity) as c:
            rows = await all_rows(c, f"""SELECT message_id, created_at, channel, direction, role,
                        body->>'text' AS text, task_id
                    FROM core.messages
                   WHERE project_id=%s
                     AND channel IN ('discord', 'api')
                     AND role IN ('pm', 'supervisor')
                     AND body ? 'text'
                     AND jsonb_typeof(body->'text') = 'string'
                     AND created_at <= %s::timestamptz
                     AND created_at > %s::timestamptz - make_interval(days => %s)
                     {boundary}
                   ORDER BY created_at DESC, message_id DESC
                   LIMIT %s""",
                (target_project_id, context["as_of"], context["as_of"], days,
                 *(context["after"] or []), limit + 1))
        page = _page_result(rows, limit, context, "message_id")
        page["items"] = [{**row, "message_id": str(row["message_id"]),
                           "task_id": str(row["task_id"]) if row.get("task_id") else None,
                           "created_at": row["created_at"].isoformat()}
                          for row in reversed(page["items"])]
        return {"project_id": target_project_id, **page}

    async def list_project_documents(self, source_project_id: str, target_project_id: str, *,
                                      limit: int = 50, cursor: str | None = None, **identity) -> dict[str, Any]:
        """List bounded, user-facing document references recorded on target tasks."""
        from assistant_kernel.core.db import all_rows
        from assistant_kernel.core.kernel import _page_context, _page_result
        limit = min(max(int(limit), 1), 50)
        context = _page_context(cursor, source_project_id, "project_access_documents", {"target": target_project_id})
        boundary = "AND (created_at, task_id) < (%s::timestamptz, %s::uuid)" if context["after"] else ""
        async with self._read(source_project_id, target_project_id, "documents", **identity) as c:
            rows = await all_rows(c, f"""SELECT task_id, goal, created_at, documents
                FROM core.tasks WHERE project_id=%s AND created_at <= %s::timestamptz
                {boundary} ORDER BY created_at DESC, task_id DESC LIMIT %s""",
                (target_project_id, context["as_of"], *(context["after"] or []), limit + 1))
        flattened = []
        for row in rows:
            for kind, ref in (row.get("documents") or {}).items():
                if kind not in DOCUMENT_SCOPE_KINDS or not isinstance(ref, dict):
                    continue
                flattened.append({"task_id": str(row["task_id"]), "goal": str(row["goal"])[:300],
                                  "created_at": row["created_at"], "kind": kind, "ref": ref})
        # The page boundary is task-based; return only documents from the selected task page.
        page = _page_result(rows, limit, context, "task_id")
        task_ids = {str(row["task_id"]) for row in page["items"]}
        page["items"] = [{**item, "created_at": item["created_at"].isoformat()}
                          for item in flattened if item["task_id"] in task_ids]
        return {"project_id": target_project_id, **page}

    async def read_project_document(self, source_project_id: str, target_project_id: str, *,
                                    task_id: str, kind: str, **identity) -> dict[str, Any]:
        """Read one stored task document by its target task and approved kind, never by arbitrary path."""
        if kind not in DOCUMENT_SCOPE_KINDS:
            raise PermissionError("document kind is not exposed through cross-project access")
        from assistant_kernel.core.db import one
        from assistant_kernel.services.document_runtime import TaskDocumentService
        async with self._read(source_project_id, target_project_id, "documents", **identity) as c:
            row = await one(c, "SELECT documents FROM core.tasks WHERE task_id=%s AND project_id=%s",
                            (task_id, target_project_id))
            if row is None or not isinstance((row.get("documents") or {}).get(kind), dict):
                raise FileNotFoundError("document reference is not registered on the target task")
            ref = dict(row["documents"][kind])
            # The database reference is the allow-list. Recheck its identity,
            # project prefix and immutable hash before reading the target file.
            uri = str(ref.get("uri") or "")
            if not uri.startswith(f"vault://{target_project_id}/runs/"):
                raise PermissionError("document reference is outside the target task runtime")
            document = TaskDocumentService(self.settings).read(target_project_id, ref)
        payload = document.get("payload")
        return {"project_id": target_project_id, "task_id": str(task_id), "kind": kind,
                "ref": ref, "status": document.get("status"), "created_at": document.get("created_at"),
                "payload": payload}

    async def read_project_tasks(self, source_project_id: str, target_project_id: str, *, limit: int = 30,
                                 cursor: str | None = None, **identity) -> dict[str, Any]:
        from assistant_kernel.core.db import all_rows
        from assistant_kernel.core.kernel import _page_context, _page_result
        context = _page_context(cursor, source_project_id, "project_access_tasks", {"target": target_project_id})
        limit = min(max(int(limit), 1), 30)
        boundary = "AND (created_at, task_id) < (%s::timestamptz, %s::uuid)" if context["after"] else ""
        async with self._read(source_project_id, target_project_id, "tasks", **identity) as c:
            rows = await all_rows(c, f"""SELECT task_id, kind, state, stage, left(goal,300) AS goal, created_at
                FROM core.tasks WHERE project_id=%s AND created_at <= %s::timestamptz {boundary}
                ORDER BY created_at DESC, task_id DESC LIMIT %s""",
                (target_project_id, context["as_of"], *(context["after"] or []), limit + 1))
        page = _page_result(rows, limit, context, "task_id")
        page["items"] = [{**row, "task_id": str(row["task_id"]), "created_at": row["created_at"].isoformat()} for row in page["items"]]
        return {"project_id": target_project_id, **page}
