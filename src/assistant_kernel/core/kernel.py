"""Public entry points of the rebuilt core. Each method is one transaction."""
from __future__ import annotations

import base64
import asyncio
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from assistant_kernel.config import KernelSettings
from assistant_kernel.core import cards, i18n, machine, rules
from assistant_kernel.core.db import Database, all_rows, event, lock_project, one

EVOLUTION_GOAL = "kernel.evolution_goal"   # interface text key: the weekly review's goal in the project's language
EVOLUTION_TZ = ZoneInfo("Asia/Shanghai")
IDLE_DAYS = 7   # a task waiting this long for the user ends on its own

log = logging.getLogger(__name__)
CONTROL_RUNS = ("pm_turn", "supervisor", "validation", "publish", "bootstrap", "triage", "job")


def _page_context(cursor: str | None, project_id: str, kind: str, filters: dict) -> dict:
    """Read-only keyset navigation, bound to the project and query; not an authorization token."""
    context = {"project": project_id, "kind": kind, "filters": filters}
    if not cursor:
        return {**context, "as_of": datetime.now(timezone.utc).isoformat(), "after": None}
    try:
        if len(cursor) > 8192:
            raise ValueError
        data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if any(data.get(k) != v for k, v in context.items()):
            raise ValueError
        cutoff = datetime.fromisoformat(data["as_of"])
        at, ident = data["after"]
        timestamp = datetime.fromisoformat(at)
        uuid.UUID(ident)
        if cutoff.tzinfo is None or timestamp.tzinfo is None or timestamp > cutoff:
            raise ValueError
        return {**context, "as_of": cutoff.isoformat(), "after": [timestamp.isoformat(), ident]}
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise ValueError("the paging cursor is invalid or does not match the project/filters; query the first page again.") from exc


def _page_result(rows: list[dict], limit: int, context: dict, id_key: str) -> dict:
    truncated = len(rows) > limit
    items = rows[:limit]
    cursor = None
    if truncated:
        last = items[-1]
        body = {**context, "after": [last["created_at"].isoformat(), str(last[id_key])]}
        cursor = base64.urlsafe_b64encode(json.dumps(body).encode()).decode()
    return {"items": items, "count": len(items), "truncated": truncated, "next_cursor": cursor,
            "as_of": context["as_of"]}


class LeaseLost(Exception):
    """The run is no longer held by this claim (cancelled, expired or claimed elsewhere)."""


class Kernel:
    def __init__(self, settings: KernelSettings, *, retry_backoff_seconds: float = 15.0, triage: bool = True):
        self.db = Database(settings)
        self.settings = settings
        self.retry_backoff_seconds = retry_backoff_seconds
        self.triage = triage   # idle PM-channel messages go to triage first (R2.1); tests may start tasks directly

    # ------------------------------------------------------------ projects and messages

    async def ensure_project(self, project_id: str, *, kind: str = "business", lifecycle: str | None = None) -> None:
        """Register a project; incomplete bootstrap projects stay out of discovery."""
        async with self.db.tx() as c:
            await c.execute("INSERT INTO core.projects(project_id, kind) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                            (project_id, kind))
            state = lifecycle or "ready"
            await c.execute("""INSERT INTO core.project_lifecycle
                (project_id, lifecycle_state, registration_origin, ready_at)
                VALUES (%s, %s, %s, CASE WHEN %s='ready' THEN now() ELSE NULL END)
                ON CONFLICT (project_id) DO NOTHING""",
                (project_id, state, "bootstrap" if lifecycle == "pending" else "legacy_import", state))

    async def project_kind(self, project_id: str) -> str:
        """Authoritative identity: project.yaml is only a descriptive mirror."""
        async with self.db.tx() as c:
            project = await one(c, "SELECT kind FROM core.projects WHERE project_id=%s", (project_id,))
            if project is None:
                raise ValueError(f"unknown project {project_id}")
            return project["kind"]

    async def project_directory(self, source_project_id: str) -> list[dict[str, Any]]:
        from assistant_kernel.core.project_access import ProjectAccessService
        return await ProjectAccessService(self.settings).discoverable_projects(source_project_id)

    async def project_access(self, source_project_id: str) -> dict[str, Any]:
        from assistant_kernel.core.project_access import ProjectAccessService
        return await ProjectAccessService(self.settings).current(source_project_id)

    async def request_project_access(self, source_project_id: str, request: dict[str, Any], *,
                                     idempotency_key: str, source_run_id=None,
                                     source_task_id=None, claim_token=None) -> dict[str, Any]:
        """Create an expiring read request; approval remains a separate source-project card action."""
        from assistant_kernel.core.project_access import AccessPolicyError, ProjectAccessService, create_request_in_transaction
        ProjectAccessService(self.settings).require_enabled()
        async with self.db.tx() as c:
            await lock_project(c, source_project_id)
            if source_run_id is not None:
                live = await one(c, """SELECT task_id FROM core.runs WHERE run_id=%s AND project_id=%s
                    AND claim_token=%s AND status='running' AND NOT cancel_requested
                    AND lease_until > clock_timestamp()""", (source_run_id, source_project_id, claim_token))
                if live is None:
                    raise PermissionError("access request run is no longer active")
                if source_task_id is not None and str(source_task_id) != str(live["task_id"]):
                    raise PermissionError("access request task does not belong to the leased run")
                source_task_id = live["task_id"]
            targets = {str(value).strip() for value in request.get("targets") or [] if str(value).strip()}
            targets.discard(source_project_id)
            if not targets:
                raise AccessPolicyError("an access request needs another project")
            eligible = await all_rows(c, """SELECT p.project_id FROM core.projects p
                JOIN core.project_lifecycle l USING (project_id)
                WHERE p.status='active' AND p.kind='business' AND l.lifecycle_state='ready'
                  AND p.project_id = ANY(%s)""", (list(targets),))
            if {str(row["project_id"]) for row in eligible} != targets:
                raise AccessPolicyError("every target must be an active, ready business project")
            created, replayed = await create_request_in_transaction(
                c, source_project_id, {**request, "targets": sorted(targets)},
                idempotency_key=idempotency_key, source_run_id=source_run_id, source_task_id=source_task_id)
            if replayed:
                return {"request_id": str(created["request_id"]), "status": created["status"], "replayed": True}
            item = cards.new_item(
                target_kind="project_access_request", target_id=str(created["request_id"]), kind="decision",
                origin="supervisor", deliver_to="supervisor",
                content={"decision": "project_access_request",
                         "message": "A project-level read-only access request is waiting for your approval.",
                         "actions": ["approve", "reject"],
                         "data": {"request_id": str(created["request_id"]),
                                  "targets": sorted(targets), "read_scopes": list(created["read_scopes"]),
                                  "expires_at": created["candidate_expires_at"].isoformat(),
                                  "reason": created["reason"]}})
            await cards.add_item(c, source_project_id, item)
            await c.execute("UPDATE core.project_access_requests SET card_item_id=%s WHERE request_id=%s",
                            (item["item_id"], created["request_id"]))
            return {"request_id": str(created["request_id"]), "status": "pending", "replayed": False}

    async def receive(self, project_id: str, *, role: str, text: str, channel: str = "api",
                      chat_id: str | None = None, user_id: str | None = None,
                      external_event_id: str | None = None, images: list[dict] = ()) -> dict[str, Any]:
        """Store one inbound user message and route it: PM channel → task, Supervisor channel → consult.

        A repeated external_event_id is recognised and not processed again.
        """
        if images:
            from assistant_kernel.core.images import MAX_IMAGES, validate_refs, ImageInputError
            if len(images) > MAX_IMAGES:
                raise ImageInputError("count")
            await asyncio.to_thread(validate_refs, self.settings, project_id, images)
        async with self.db.tx() as c:
            await lock_project(c, project_id)
            message = await one(
                c, """INSERT INTO core.messages(project_id, channel, chat_id, user_id, direction, role, body,
                                                external_event_id)
                      VALUES (%s, %s, %s, %s, 'inbound', %s, %s, %s)
                      ON CONFLICT (channel, external_event_id) WHERE external_event_id IS NOT NULL DO NOTHING
                      RETURNING *""",
                (project_id, channel, chat_id, user_id, role,
                 Jsonb({"text": text, **({"images": images} if images else {})}), external_event_id))
            if message is None:
                return {"action": "duplicate"}
            routed = (await machine.user_input(c, project_id, message, triage=self.triage) if role == "pm"
                      else await machine.consult(c, project_id, message))
            return {"message_id": str(message["message_id"]), **routed}

    async def received_event(self, project_id: str, channel: str, external_event_id: str) -> bool:
        async with self.db.tx() as c:
            return bool(await one(c, """SELECT 1 FROM core.messages WHERE project_id=%s AND channel=%s
                AND external_event_id=%s""", (project_id, channel, external_event_id)))

    async def image_inputs(self, run: dict) -> list[dict]:
        """Image history bound to this run, never another project's or later PM turn's inputs."""
        task_id = run.get("task_id") or run["input"].get("context_task_id")
        message_id = run["input"].get("message_id")
        cutoff = run.get("started_at") or run["created_at"]
        async with self.db.tx() as c:
            if task_id and run["purpose"] != "consult":
                # PM claiming already pinned its batch. Supervisor evaluations include pending inputs.
                pinned = ("AND (m.consumed_by_run_id IS NOT NULL OR m.message_id=t.source_message_id)"
                          if run["run_type"] == "pm_turn" else "")
                return await all_rows(c, f"""SELECT m.* FROM core.messages m
                    JOIN core.tasks t ON t.task_id=%s AND t.project_id=m.project_id
                    WHERE m.project_id=%s AND (m.task_id=t.task_id OR m.message_id=t.source_message_id)
                    AND m.direction='inbound' AND (m.body ? 'images' OR m.body ? 'image_context')
                    AND m.created_at<=%s {pinned}
                    ORDER BY m.created_at, m.message_id""", (task_id, run["project_id"], cutoff))
            if not message_id:
                return []
            # Bound the conversation window by messages, not by images from arbitrarily old chats.
            return list(reversed(await all_rows(c, """SELECT * FROM (SELECT m.* FROM core.messages m
                JOIN core.messages source ON source.message_id=%s AND source.project_id=m.project_id
                WHERE m.project_id=%s AND m.direction='inbound'
                AND m.user_id IS NOT DISTINCT FROM source.user_id
                AND m.chat_id IS NOT DISTINCT FROM source.chat_id AND m.role=source.role
                AND m.created_at<=source.created_at ORDER BY m.created_at DESC, m.message_id DESC LIMIT 20) recent
                WHERE body ? 'images' OR body ? 'image_context' ORDER BY created_at DESC, message_id DESC""",
                (message_id, run["project_id"]))))

    async def create_task(self, project_id: str, **fields: Any) -> dict[str, Any]:
        async with self.db.tx() as c:
            await lock_project(c, project_id)
            return await machine.create_task(c, project_id, **fields)

    async def cancel_task(self, task_id) -> dict[str, Any]:
        async with self.db.tx() as c:
            task = await one(c, "SELECT project_id FROM core.tasks WHERE task_id=%s", (task_id,))
            await lock_project(c, task["project_id"])
            task = await machine.load_task(c, task_id)
            if task["state"] in rules.TERMINAL_STATES:
                return task
            if task["state"] == "publishing":   # recheck under the project lock; the channel's earlier read may race
                return task
            from assistant_kernel.core.executors.delivery import require_publication_stopped
            await require_publication_stopped(c, self.settings, task)
            return await machine.close(c, task, "cancelled", "cancelled_by_user")

    async def get_task(self, task_id) -> dict[str, Any] | None:
        async with self.db.tx() as c:
            return await one(c, "SELECT * FROM core.tasks WHERE task_id=%s", (task_id,))

    async def get_message(self, message_id) -> dict[str, Any] | None:
        async with self.db.tx() as c:
            return await one(c, "SELECT * FROM core.messages WHERE message_id=%s", (message_id,))

    async def recent_messages(self, project_id: str, limit: int = 20) -> list[dict[str, Any]]:
        async with self.db.tx() as c:
            rows = await (await c.execute(
                """SELECT * FROM core.messages WHERE project_id=%s AND body ? 'text'
                   ORDER BY created_at DESC LIMIT %s""", (project_id, limit))).fetchall()
            return list(reversed(rows))

    # ------------------------------------------------------------ cards

    async def card(self, project_id: str) -> dict[str, Any] | None:
        async with self.db.tx() as c:
            return await one(c, "SELECT * FROM core.interactions WHERE project_id=%s AND status='open'", (project_id,))

    async def act(self, project_id: str, item_id: str, action: str, values: dict[str, Any] | None = None,
                  *, user_id: str | None = None, external_event_id: str | None = None) -> dict[str, Any]:
        """Apply a card action. Raises cards.StaleItem for replaced/handled items (UI: 'card updated')."""
        if action == "submit" and (values or {}).get("answers"):
            values = {**values, "answers": await self._store_credentials(project_id, item_id, values["answers"])}
        async with self.db.tx() as c:
            await lock_project(c, project_id)
            record = await one(
                c, """INSERT INTO core.messages(project_id, channel, user_id, direction, role, body,
                                                external_event_id)
                      VALUES (%s, 'card', %s, 'inbound', 'supervisor', %s, %s)
                      ON CONFLICT (channel, external_event_id) WHERE external_event_id IS NOT NULL DO NOTHING
                      RETURNING message_id""",
                (project_id, user_id, Jsonb({"card_action": {"item_id": item_id, "action": action,
                                                              "values": values or {}}}), external_event_id))
            if record is None:
                return {"action": "duplicate"}
            result = await machine.on_action(c, project_id, item_id, action, values or {},
                                             source_message_id=record["message_id"], settings=self.settings)
            return {"action": action, "task_id": str(result["task_id"]) if result.get("task_id") else None,
                    **({"access_status": result["access_status"]} if "access_status" in result else {})}

    SECRET_NOTES = ("(saved as project credential ", "(model setup: ")

    async def store_secret_answers(self, project_id: str, item_id: str, answers: dict[str, Any]) -> dict[str, Any]:
        """Take secret answers out before anything is recorded (submits and saved drafts of multi-page cards alike):
        `credential` answers go to the project's credential file (ROUND5 D8), `model_setup` answers to the vendor key
        store (assistant-model-routing). What remains is a note; a note is never stored again as a value."""
        card = await self.card(project_id)
        item = next((i for i in (card or {}).get("items", []) if i["item_id"] == item_id), None)
        questions = ((item or {}).get("content") or {}).get("questions") or []
        marked = {q["id"]: q["credential"] for q in questions
                  if isinstance(q.get("credential"), dict) and q["credential"].get("key")}
        setups = {q["id"]: q["model_setup"] for q in questions if isinstance(q.get("model_setup"), dict)}
        if not marked and not setups:
            return answers
        stored = dict(answers)
        if marked:
            from assistant_kernel.core import credentials
            from assistant_kernel.storage import ProjectRuntimeManager
            vault = ProjectRuntimeManager(self.settings.data_root).ensure(project_id).vault_root
            for question_id, spec in marked.items():
                value = str(answers.get(question_id) or "").strip()
                if value and not value.startswith(self.SECRET_NOTES):
                    credentials.put(vault, str(spec["key"]), value, service=str(spec.get("service") or ""),
                                    purpose=str(spec.get("purpose") or ""), source="user_card")
                    stored[question_id] = f"(saved as project credential {spec['key']}; read it with get_credential)"
        for question_id, spec in setups.items():
            value = str(answers.get(question_id) or "").strip()
            if value and not value.startswith(self.SECRET_NOTES):
                from assistant_kernel.core import model_secrets
                stored[question_id] = await asyncio.to_thread(model_secrets.apply_setup_answer, self.settings, spec,
                                                              value)
        return stored

    _store_credentials = store_secret_answers   # the name used before model setup existed

    async def displayed_cards(self, project_id: str) -> list[dict[str, Any]]:
        """The open card plus closed cards whose channel messages still need tidying up."""
        async with self.db.tx() as c:
            return list(await (await c.execute(
                """SELECT * FROM core.interactions WHERE project_id=%s
                   AND (status='open' OR display->'messages' <> '{}'::jsonb) ORDER BY created_at""",
                (project_id,))).fetchall())

    async def card_item_status(self, *, pid: str, item_id: str) -> str:
        """Resolve a retired card from recorded facts, without guessing its outcome."""
        from assistant_kernel.core.i18n import t
        async with self.db.tx() as c:
            action = await one(c, """SELECT payload FROM core.task_events WHERE project_id=%s
                AND kind='card_action' AND payload->>'item_id'=%s ORDER BY created_at DESC LIMIT 1""", (pid, item_id))
            if action:
                return t("card.history_status", status=t("done." + action["payload"]["action"]))
            replacement = await one(c, """SELECT 1 FROM core.task_events WHERE project_id=%s
                AND kind='card_item_shown' AND payload->>'replaces'=%s LIMIT 1""", (pid, item_id))
            return t("card.history_replaced" if replacement else "card.history_closed")

    async def set_card_display(self, interaction_id, display: dict[str, Any]) -> None:
        async with self.db.tx() as c:
            await cards.set_display(c, interaction_id, display)

    SEND_UNKNOWN_AFTER = timedelta(minutes=10)

    async def pending_outbound(self, limit: int = 20) -> list[dict[str, Any]]:
        """Messages to send: never tried, or tried so long ago that the attempt's outcome is unknown (the gateway
        stopped between sending and recording it)."""
        async with self.db.tx() as c:
            return list(await (await c.execute(
                """SELECT * FROM core.messages WHERE direction='outbound' AND delivery_status='pending'
                     AND (NOT body ? 'sending_at' OR (body->>'sending_at')::timestamptz < now() - %s)
                   ORDER BY created_at LIMIT %s""", (self.SEND_UNKNOWN_AFTER, limit))).fetchall())

    async def begin_delivery(self, message_id) -> dict[str, Any] | None:
        """Record that sending starts; returns the body (with `maybe_duplicate` once an earlier attempt's outcome was
        unknown, so the text gets a repeat marker). None: already handled, or a second unknown-outcome attempt is
        due — then it is marked failed instead of risking a third copy."""
        async with self.db.tx() as c:
            row = await one(c, """UPDATE core.messages SET body = body || jsonb_build_object('sending_at', now(),
                                     'send_attempts', coalesce((body->>'send_attempts')::int, 0) + 1)
                                     || CASE WHEN body ? 'sending_at' THEN '{"maybe_duplicate": true}'::jsonb
                                             ELSE '{}'::jsonb END
                                  WHERE message_id=%s AND delivery_status='pending' RETURNING body""", (message_id,))
            if row is None:
                return None
            if int(row["body"]["send_attempts"]) > 2:
                await c.execute("UPDATE core.messages SET delivery_status='failed' WHERE message_id=%s", (message_id,))
                return None
            return row["body"]

    async def delivery_failed_known(self, message_id) -> None:
        """The send raised, so this attempt did not go out: it may be tried again (an earlier unknown attempt keeps
        its `maybe_duplicate` marker)."""
        async with self.db.tx() as c:
            await c.execute("""UPDATE core.messages SET body = (body - 'sending_at') || jsonb_build_object(
                                   'send_attempts', greatest(coalesce((body->>'send_attempts')::int, 1) - 1, 0))
                               WHERE message_id=%s AND delivery_status='pending'""", (message_id,))

    async def mark_delivered(self, message_id, *, external_id: str | None, ok: bool = True) -> None:
        async with self.db.tx() as c:
            await c.execute("""UPDATE core.messages SET delivery_status=%s, external_event_id=COALESCE(%s, external_event_id)
                               WHERE message_id=%s""", ("sent" if ok else "failed", external_id, message_id))

    async def start_evolution(self, project_id: str, *, origin: str = "user",
                              idempotency_key: str | None = None) -> dict[str, Any]:
        """Queue a self-evolution review: the PM reviews the last week, the user decides on its proposals."""
        profile = rules.review_profile(await self.project_kind(project_id))
        goal = i18n.t("kernel.cos_review_goal" if profile == "chief_of_staff" else EVOLUTION_GOAL)
        return await self.create_task(project_id, goal=goal, origin=origin,
                                      kind="evolution",
                                      idempotency_key=idempotency_key)

    async def schedule_evolution(self, now: datetime | None = None) -> list[str]:
        """Sunday 13:00 plus 24h catch-up; project lock and existing weekly key prevent duplicate scheduling."""
        local = (now or datetime.now(timezone.utc)).astimezone(EVOLUTION_TZ)
        due_at = (local - timedelta(days=local.isoweekday() % 7)).replace(hour=13, minute=0, second=0, microsecond=0)
        if not timedelta(0) <= local - due_at < timedelta(hours=24):
            return []
        year, week, _ = due_at.isocalendar()
        week_key = f"{year}-W{week:02d}"
        async with self.db.tx() as c:
            projects = await all_rows(c, "SELECT project_id FROM core.projects WHERE status='active' ORDER BY project_id")
        queued = []
        for row in projects:
            project_id = row["project_id"]
            async with self.db.tx() as c:
                await lock_project(c, project_id)
                due = await one(c, """
                SELECT p.project_id FROM core.projects p WHERE p.project_id=%s
                  AND p.status='active' AND p.kind<>'chief_of_staff'
                  AND EXISTS (SELECT 1 FROM core.project_lifecycle l
                              WHERE l.project_id=p.project_id AND l.lifecycle_state='ready')
                  AND coalesce((p.settings->>'weekly_evolution')::boolean, true)
                  AND NOT EXISTS (SELECT 1 FROM core.tasks t WHERE t.idempotency_key=%s)
                  AND NOT EXISTS (SELECT 1 FROM core.tasks t WHERE t.project_id=p.project_id AND t.kind='evolution'
                                    AND NOT (t.state = ANY(%s)))
                  AND (EXISTS (SELECT 1 FROM core.messages m WHERE m.project_id=p.project_id AND m.direction='inbound'
                                 AND m.created_at > %s)
                       OR EXISTS (SELECT 1 FROM core.tasks t WHERE t.project_id=p.project_id AND t.kind <> 'evolution'
                                    AND t.updated_at > %s)
                       OR EXISTS (SELECT 1 FROM core.jobs j WHERE j.project_id=p.project_id
                                    AND (j.status='enabled' OR (j.kind='service' AND j.status <> 'retired')
                                         OR j.last_at > %s))
                       OR EXISTS (SELECT 1 FROM core.runs r WHERE r.project_id=p.project_id
                                    AND r.run_type='job' AND r.created_at > %s)
                       OR EXISTS (SELECT 1 FROM core.iterations i WHERE i.project_id=p.project_id
                                    AND coalesce(i.expected_effect,'') <> ''
                                    AND (i.effect_status IS NULL OR i.effect_status='pending')))""",
                                (project_id, f"evolution:{project_id}:{week_key}", list(rules.TERMINAL_STATES),
                                 *([local - timedelta(days=7)] * 4)))
                if due:
                    await machine.create_task(c, project_id, goal=i18n.t(EVOLUTION_GOAL),
                                              origin="schedule", kind="evolution",
                                              idempotency_key=f"evolution:{project_id}:{week_key}")
                    queued.append(project_id)
        return queued

    async def expire_idle_tasks(self, days: int = IDLE_DAYS) -> list[str]:
        """Tasks waiting for the user with no word or click from the user for `days` end on their own, so the
        project's next task can start (ROUND5 D23). Returns their ids."""
        async with self.db.tx() as c:
            idle = await all_rows(c, """
                SELECT t.task_id, t.project_id FROM core.tasks t
                WHERE t.state = 'awaiting_user' AND t.updated_at < now() - make_interval(days => %s)
                  AND NOT EXISTS (SELECT 1 FROM core.messages m WHERE m.task_id = t.task_id
                                  AND m.direction = 'inbound' AND m.created_at > now() - make_interval(days => %s))""",
                                  (days, days))
        expired = []
        for row in idle:
            async with self.db.tx() as c:
                await lock_project(c, row["project_id"])
                task = await machine.load_task(c, row["task_id"])
                if task["state"] != "awaiting_user":
                    continue
                from assistant_kernel.core.executors.delivery import require_publication_stopped
                try:
                    await require_publication_stopped(c, self.settings, task)
                except ValueError:
                    continue   # idle expiry is not evidence that an uncertain publisher has stopped
                await machine.close(c, task, "cancelled", "expired_no_response")
                await machine.notify(c, task["project_id"], "supervisor",
                                     i18n.t("kernel.expired", days=days,
                                            goal=task["goal"][:100]), task_id=task["task_id"])
                expired.append(str(task["task_id"]))
        return expired

    async def grants(self, project_id: str) -> list[dict[str, Any]]:
        async with self.db.tx() as c:
            return await all_rows(c, """SELECT resource_key, policy_json FROM core.project_grants WHERE project_id=%s
                                        AND grant_kind='file' AND status='active' ORDER BY resource_key""", (project_id,))

    async def revoke_grant(self, project_id: str, key: str) -> bool:
        async with self.db.tx() as c:
            await lock_project(c, project_id)
            return await machine.revoke_grant(c, project_id, key,
                                              reason=i18n.t("kernel.revoked_command"))

    async def iterations(self, project_id: str, *, days: int = 28, artifact: str | None = None,
                         major_only: bool = False, query: str | None = None, limit: int = 30,
                         pending_only: bool = False, needs_measure: bool = False, oldest_first: bool = False,
                         as_of: str | None = None, after: list[str] | None = None) -> list[dict[str, Any]]:
        """The project's change stream, newest first (ROUND4 R4.1). `query` matches words for now; the same parameter
        can later be served by vector search over search_text without changing callers."""
        sql = ["SELECT * FROM core.iterations WHERE project_id=%s"]
        params: list[Any] = [project_id]
        if pending_only or needs_measure:
            sql.append("AND coalesce(expected_effect, '') <> '' AND (effect_status IS NULL OR effect_status='pending')")
            sql.append("AND coalesce(measure, '') = ''" if needs_measure else "AND coalesce(measure, '') <> ''")
        else:
            sql.append("AND created_at > COALESCE(%s::timestamptz, now()) - %s::interval")
            params += [as_of, f"{max(1, int(days))} days"]
        if as_of:
            sql.append("AND created_at <= %s::timestamptz")
            params.append(as_of)
        if after:
            sql.append(f"AND (created_at, iteration_id) {'>' if oldest_first else '<'} (%s::timestamptz, %s::uuid)")
            params += after
        if artifact:
            sql.append("AND (%s = ANY(artifacts) OR EXISTS (SELECT 1 FROM unnest(artifacts) a WHERE a LIKE %s))")
            params += [artifact, artifact.rstrip("*") + "%"]
        if major_only:
            sql.append("AND significance='major'")
        for word in (query or "").split()[:5]:
            sql.append("AND search_text ILIKE %s")
            params.append(f"%{word}%")
        order = "ASC" if oldest_first else "DESC"
        sql.append(f"ORDER BY created_at {order}, iteration_id {order} LIMIT %s")
        params.append(min(max(int(limit), 1), 100))
        async with self.db.tx() as c:
            return await all_rows(c, " ".join(sql), tuple(params))

    async def iteration_page(self, project_id: str, *, days: int = 28, artifact: str | None = None,
                             major_only: bool = False, query: str | None = None, limit: int = 20,
                             pending_only: bool = False, needs_measure: bool = False,
                             oldest_first: bool = False, cursor: str | None = None) -> dict:
        filters = dict(days=days, artifact=artifact, major_only=major_only, query=query,
                       pending_only=pending_only, needs_measure=needs_measure, oldest_first=oldest_first)
        context = _page_context(cursor, project_id, "iterations", filters)
        limit = min(max(limit, 1), 50)
        rows = await self.iterations(project_id, **filters, limit=limit + 1,
                                     as_of=context["as_of"], after=context["after"])
        return _page_result(rows, limit, context, "iteration_id")

    async def history_page(self, project_id: str, *, lookback_days: int = 365, limit: int = 50,
                           cursor: str | None = None) -> dict:
        days, limit = min(max(lookback_days, 1), 365), min(max(limit, 1), 200)
        context = _page_context(cursor, project_id, "messages", {"days": days})
        boundary = "AND (created_at, message_id) < (%s::timestamptz, %s::uuid)" if context["after"] else ""
        async with self.db.tx() as c:
            rows = await all_rows(c, f"""SELECT message_id, created_at, direction, role, body->>'text' AS text
                FROM core.messages WHERE project_id=%s AND body ? 'text'
                  AND created_at <= %s::timestamptz AND created_at > %s::timestamptz - make_interval(days => %s)
                  {boundary} ORDER BY created_at DESC, message_id DESC LIMIT %s""",
                                  (project_id, context["as_of"], context["as_of"], days,
                                   *(context["after"] or []), limit + 1))
        page = _page_result(rows, limit, context, "message_id")
        page["items"] = [{**r, "message_id": str(r["message_id"]), "created_at": r["created_at"].isoformat()}
                         for r in reversed(page["items"])]  # retain oldest-first display within each page
        return page

    async def evolution_context(self, project_id: str, limit: int = 30) -> dict:
        """Bounded operational observations and follow-up pointers; no service probes or state changes."""
        limit = min(max(limit, 1), 50)
        async with self.db.tx() as c:
            runtime = await all_rows(c, """SELECT job_id, kind, status, last_status, last_at, next_at, failures
                FROM core.jobs WHERE project_id=%s ORDER BY (status='retired'), updated_at DESC, job_id LIMIT %s""",
                                     (project_id, limit + 1))
            tasks = await all_rows(c, """SELECT task_id, kind, state, left(goal,240) AS goal, left(reason,800) AS reason,
                parent_task_id, source_ref, updated_at,
                documents->'improvement_proposal'->'ref' AS proposal_ref FROM core.tasks
                WHERE project_id=%s AND (NOT (state = ANY(%s)) OR kind='evolution' OR origin='evolution')
                ORDER BY (NOT (state = ANY(%s))) DESC, updated_at DESC, task_id DESC LIMIT %s""",
                                   (project_id, list(rules.TERMINAL_STATES), list(rules.TERMINAL_STATES), limit + 1))
            prior = await all_rows(c, """SELECT task_id, state, documents->'improvement_proposal'->'ref' AS proposal_ref
                FROM core.tasks WHERE project_id=%s AND kind='evolution'
                  AND documents->'improvement_proposal'->'ref' IS NOT NULL
                ORDER BY updated_at DESC, task_id DESC LIMIT 3""", (project_id,))
            agent_runs = await all_rows(c, """SELECT run_id, task_id, run_type, purpose, created_at,
                input->>'stage' AS stage, result->'skills' AS skills, result->>'config_sha256' AS config_sha256
                FROM core.runs WHERE project_id=%s AND status='succeeded'
                  AND (result ? 'skills' OR result ? 'config_sha256')
                ORDER BY created_at DESC, run_id DESC LIMIT 12""", (project_id,))
            # Each observation with how it ended so far (daily operations): reviewed, proposed, task, closed, ...
            observations = await all_rows(c, """SELECT e.event_id, e.run_id, e.created_at, e.payload,
                (SELECT h.kind FROM core.task_events h WHERE h.project_id=e.project_id AND h.kind = ANY(%s)
                   AND h.payload->>'event_id' = e.event_id::text ORDER BY h.event_id DESC LIMIT 1) AS handling
                FROM core.task_events e WHERE e.project_id=%s AND e.kind='job_result'
                ORDER BY e.created_at DESC, e.event_id DESC LIMIT %s""",
                                          (list(machine.OBSERVATION_ENDS), project_id, limit + 1))
        return {"observed_at": datetime.now(timezone.utc).isoformat(),
                "runtime": runtime[:limit], "runtime_truncated": len(runtime) > limit,
                "related_tasks": tasks[:limit], "tasks_truncated": len(tasks) > limit, "recent_proposals": prior,
                "recent_agent_runs": agent_runs, "job_observations": observations[:limit],
                "job_observations_truncated": len(observations) > limit}

    async def daily_operations(self, project_id: str) -> bool:
        async with self.db.tx() as c:
            return await machine.project_switch(c, project_id, machine.DAILY_OPERATIONS)

    async def observation(self, project_id: str, event_id) -> dict[str, Any] | None:
        """One job observation of this project (never another project's), with the job's current state."""
        if not str(event_id or "").isdigit():
            return None
        async with self.db.tx() as c:
            row = await one(c, """SELECT event_id, created_at, payload FROM core.task_events
                                  WHERE event_id=%s::bigint AND project_id=%s AND kind='job_result'""",
                            (str(event_id), project_id))
            if row:
                row["job"] = await one(c, """SELECT job_id, kind, status, last_status, last_at, next_at, failures
                    FROM core.jobs WHERE project_id=%s AND job_id=%s""", (project_id, row["payload"].get("job_id")))
            return row

    async def recent_tasks(self, project_id: str, *, exclude=None, limit: int = 5,
                           include_basis: bool = False) -> list[dict[str, Any]]:
        """The project's other tasks a stateless Supervisor should know about: queued ones, then the latest ended."""
        extra = ", reason, parent_task_id, source_ref" if include_basis else ""
        query = f"""
            (SELECT task_id, kind, state, close_reason, left(goal, 120) AS goal, updated_at{extra} FROM core.tasks
             WHERE project_id=%s AND state='queued' AND task_id IS DISTINCT FROM %s ORDER BY created_at)
            UNION ALL
            (SELECT task_id, kind, state, close_reason, left(goal, 120) AS goal, updated_at{extra} FROM core.tasks
             WHERE project_id=%s AND state = ANY(%s) AND task_id IS DISTINCT FROM %s
             ORDER BY updated_at DESC LIMIT %s)"""
        params = (project_id, exclude, project_id, list(rules.TERMINAL_STATES), exclude, limit)
        if include_basis:
            # Queued tasks may have no state.md yet. Read their authoritative basis and same-project parent ref.
            query = f"""SELECT r.*, p.documents->'improvement_proposal'->'ref' AS source_proposal_ref
                FROM ({query}) r LEFT JOIN core.tasks p ON p.task_id=r.parent_task_id AND p.project_id=%s
                ORDER BY (r.state='queued') DESC, r.updated_at DESC, r.task_id"""
            params += (project_id,)
        async with self.db.tx() as c:
            return await all_rows(c, query, params)

    async def task_ledger(self, task_id) -> dict[str, Any]:
        """The recorded facts of one task: its finished PM turns (input and output), the user messages each turn
        consumed, and the card actions. Read-only; rendered by executors/state.py."""
        async with self.db.tx() as c:
            turns = await all_rows(c, """SELECT run_id, input, result, created_at FROM core.runs
                                         WHERE task_id=%s AND run_type='pm_turn' AND status='succeeded'
                                         ORDER BY created_at""", (task_id,))
            messages = await all_rows(c, """SELECT consumed_by_run_id, body->>'text' AS text,
                                            body->'images' AS images FROM core.messages
                                            WHERE task_id=%s AND direction='inbound' AND consumed_by_run_id IS NOT NULL
                                            ORDER BY created_at""", (task_id,))
            actions = await all_rows(c, """SELECT payload, created_at FROM core.task_events
                                           WHERE task_id=%s AND kind='card_action' ORDER BY event_id""", (task_id,))
            shown = await all_rows(c, """SELECT payload, created_at FROM core.task_events
                                         WHERE task_id=%s AND kind='card_item_shown' ORDER BY event_id""", (task_id,))
            source = await one(c, """SELECT p.documents->'improvement_proposal'->'ref' AS proposal
                                     FROM core.tasks t JOIN core.tasks p ON p.task_id=t.parent_task_id
                                     AND p.project_id=t.project_id WHERE t.task_id=%s AND t.origin='evolution'""", (task_id,))
        supplements: dict[str, list[str]] = {}
        for m in messages:
            if m.get("images"):
                m["text"] = (m["text"] or "") + "\nUser image references (not image contents): " + json.dumps(
                    [{"filename": ref["filename"], "sha256": ref["sha256"]} for ref in m["images"]], ensure_ascii=False)
            if m["text"]:
                supplements.setdefault(str(m["consumed_by_run_id"]), []).append(m["text"])
        return {"turns": [{**t, "supplements": supplements.get(str(t["run_id"]), [])} for t in turns],
                "actions": [{**a["payload"], "at": a["created_at"]} for a in actions],
                "cards": [{**s["payload"], "at": s["created_at"]} for s in shown],
                "source_proposal": (source or {}).get("proposal")}

    async def project_digest(self, project_id: str, days: int = 7) -> dict[str, Any]:
        """What happened in a project recently: the evidence a weekly evolution review works from."""
        since = f"{int(days)} days"
        async with self.db.tx() as c:
            tasks = list(await (await c.execute(
                """SELECT task_id, kind, state, stage, turn_count, close_reason, left(goal, 160) AS goal FROM core.tasks
                   WHERE project_id=%s AND updated_at > now() - %s::interval AND kind <> 'evolution'
                   ORDER BY created_at""", (project_id, since))).fetchall())
            actions = list(await (await c.execute(
                """SELECT payload FROM core.task_events WHERE project_id=%s AND kind='card_action'
                   AND created_at > now() - %s::interval ORDER BY event_id""", (project_id, since))).fetchall())
            failures = list(await (await c.execute(
                """SELECT run_type, purpose, count(*) AS n, left(max(error->>'message'), 200) AS example
                   FROM core.runs WHERE project_id=%s AND created_at > now() - %s::interval
                   AND (status='failed' OR attempt > 1) GROUP BY run_type, purpose""", (project_id, since))).fetchall())
            counts = dict(await (await c.execute(
                """SELECT kind, count(*) FROM core.task_events WHERE project_id=%s AND created_at > now() - %s::interval
                   AND kind IN ('card_upgraded','stale_result','supervisor_reevaluate') GROUP BY kind""",
                (project_id, since))).fetchall())
            inbound = (await one(c, """SELECT count(*) AS n FROM core.messages WHERE project_id=%s AND direction='inbound'
                                       AND body ? 'text' AND created_at > now() - %s::interval""", (project_id, since)))["n"]
            delegated = await all_rows(c, """
                SELECT d->>'name' AS name, count(*) AS n FROM core.runs r,
                       jsonb_array_elements(CASE WHEN jsonb_typeof(r.result->'delegations') = 'array'
                                                 THEN r.result->'delegations' ELSE '[]'::jsonb END) d
                WHERE r.project_id=%s AND r.run_type='pm_turn' AND r.created_at > now() - %s::interval
                GROUP BY 1 ORDER BY 2 DESC""", (project_id, since))
            job_runs = await all_rows(c, """
                SELECT input->>'job_id' AS job_id, count(*) FILTER (WHERE status='succeeded') AS ok,
                       count(*) FILTER (WHERE status='failed') AS failed, max(error->>'message') AS example
                FROM core.runs WHERE project_id=%s AND run_type='job' AND created_at > now() - %s::interval
                GROUP BY 1 ORDER BY 1""", (project_id, since))
            stage_turns = await all_rows(c, """
                SELECT left(t.goal, 60) AS goal, r.input->>'stage' AS stage, count(*) AS n
                FROM core.runs r JOIN core.tasks t ON t.task_id = r.task_id
                WHERE r.project_id=%s AND r.run_type='pm_turn' AND r.status='succeeded'
                  AND r.created_at > now() - %s::interval
                GROUP BY t.task_id, t.goal, r.input->>'stage' ORDER BY min(r.created_at)""", (project_id, since))
        return {"days": days, "tasks": tasks, "card_actions": [a["payload"] for a in actions], "stage_turns": stage_turns,
                "failures": failures, "event_counts": counts, "user_messages": inbound,
                "delegations": [(d["name"] or "(unnamed)", d["n"]) for d in delegated], "jobs": job_runs}

    async def platform_maintenance_metrics(self, days: int = 7) -> dict[str, Any]:
        """Platform-only aggregate evidence for template maintenance; never return content/errors."""
        days = min(max(int(days), 1), 30)
        async with self.db.tx() as c:
            rows = await all_rows(c, """SELECT p.project_id,
                (SELECT count(*) FROM core.tasks t WHERE t.project_id=p.project_id
                    AND t.updated_at > now()-make_interval(days => %s)) AS tasks,
                (SELECT count(*) FROM core.runs r WHERE r.project_id=p.project_id
                    AND r.created_at > now()-make_interval(days => %s) AND r.status='failed') AS failed_runs,
                (SELECT count(*) FROM core.runs r WHERE r.project_id=p.project_id
                    AND r.created_at > now()-make_interval(days => %s) AND r.attempt>1) AS retried_runs
                FROM core.projects p JOIN core.project_lifecycle l USING(project_id)
                WHERE p.status='active' AND p.kind='business' AND l.lifecycle_state='ready'
                ORDER BY p.project_id LIMIT 101""", (days, days, days))
        return {"observed_at": datetime.now(timezone.utc).isoformat(), "days": days,
                "items": rows[:100], "truncated": len(rows)>100,
                "limitations": "Counts are platform evidence only; content requires Project Access."}

    async def operational_baseline(self, project_id: str) -> dict[str, Any] | None:
        async with self.db.tx() as c:
            row = await one(c, "SELECT operational_baseline FROM core.projects WHERE project_id=%s", (project_id,))
            return (row or {}).get("operational_baseline")

    async def effect_review_errors(self, project_id: str, reviews: list) -> list[str]:
        from assistant_kernel.core import iterlog
        async with self.db.tx() as c:
            _, problems = await iterlog.resolve_effect_reviews(c, project_id, reviews)
            return problems

    async def active_projects(self) -> list[str]:
        """Ready projects only: maintenance must not create a pending bootstrap's vault."""
        async with self.db.tx() as c:
            return [r["project_id"] for r in await all_rows(c, """SELECT p.project_id
                FROM core.projects p JOIN core.project_lifecycle l USING(project_id)
                WHERE p.status='active' AND l.lifecycle_state='ready' ORDER BY p.project_id""")]

    async def routing_projects(self, exclude: str) -> list[dict[str, Any]]:
        """Public registration of ready, routable projects (business and Chief of Staff, never Kernel)."""
        async with self.db.tx() as c:
            return list(await all_rows(c, """SELECT p.project_id, p.kind, p.settings
                FROM core.projects p JOIN core.project_lifecycle l USING(project_id)
                WHERE p.status='active' AND p.kind IN ('business','chief_of_staff') AND l.lifecycle_state='ready'
                    AND p.project_id<>%s ORDER BY p.project_id LIMIT 100""", (exclude,)))

    async def routing_project_ids(self, exclude: str) -> list[str]:
        return [r["project_id"] for r in await self.routing_projects(exclude)]

    async def active_task(self, project_id: str) -> dict[str, Any] | None:
        async with self.db.tx() as c:
            return await one(c, "SELECT * FROM core.tasks WHERE project_id=%s AND state = ANY(%s)",
                             (project_id, list(rules.ACTIVE_STATES)))

    async def save_draft(self, project_id: str, task_id, answers: dict[str, Any]) -> None:
        async with self.db.tx() as c:
            await lock_project(c, project_id)
            await cards.save_draft(c, project_id, task_id, answers)

    # ------------------------------------------------------------ runs

    async def claim(self, worker_id: str, *, lease_seconds: int = 900,
                    run_types: tuple[str, ...] = CONTROL_RUNS,
                    allow_agent_jobs: bool = True) -> dict[str, Any] | None:
        """Claim one runnable run. Every execution condition is in this one statement (principle P6)."""
        eligible = """r.status='queued' AND r.available_at <= now() AND r.run_type = ANY(%s)
                      AND (r.task_id IS NULL OR t.state = ANY(%s))
                      AND NOT EXISTS (SELECT 1 FROM core.runs x WHERE x.mutex_key = r.mutex_key
                                      AND x.status IN ('running','reconciling'))
                      AND (r.task_id IS NULL OR NOT EXISTS (SELECT 1 FROM core.runs x WHERE x.task_id = r.task_id
                                      AND x.status IN ('running','reconciling')))
                      AND NOT EXISTS (SELECT 1 FROM core.skill_updates u WHERE u.project_id=r.project_id
                                      AND u.status IN ('applying','rolling_back','reconciling'))"""
        # Filter the subtype inside the claiming SQL, not after claiming a script or agent.
        eligible += " AND (%s OR r.run_type <> 'job' OR (r.input->'spec'->>'type') IS DISTINCT FROM 'agent')"
        conditions = (list(run_types), list(rules.ACTIVE_STATES), allow_agent_jobs)
        try:
            async with self.db.tx() as c:
                # Same project lock as skill adoption. Recheck all predicates in the UPDATE after acquiring it:
                # a pre-lock NOT EXISTS snapshot alone would permit a claim/adoption race.
                project = await one(c, f"""SELECT p.project_id FROM core.projects p JOIN LATERAL (
                    SELECT r.created_at FROM core.runs r LEFT JOIN core.tasks t ON t.task_id=r.task_id
                    WHERE r.project_id=p.project_id AND {eligible} ORDER BY r.created_at LIMIT 1) due ON true
                    ORDER BY due.created_at,p.project_id FOR UPDATE OF p SKIP LOCKED LIMIT 1""", conditions)
                if project is None:
                    return None
                run = await one(c, f"""
                    UPDATE core.runs SET status='running', attempt=attempt+1, worker_id=%s, claim_token=%s,
                           lease_until=now() + make_interval(secs => %s), started_at=now()
                    WHERE run_id = (
                        SELECT r.run_id FROM core.runs r LEFT JOIN core.tasks t ON t.task_id = r.task_id
                        WHERE {eligible} AND r.project_id=%s
                        ORDER BY r.created_at
                        FOR UPDATE OF r SKIP LOCKED LIMIT 1)
                    RETURNING *""",
                    (worker_id, uuid.uuid4(), lease_seconds, *conditions, project['project_id']))
                if run and run["run_type"] == "pm_turn":
                    # Pin the user input this turn will read; later input waits for the next turn.
                    await c.execute("""UPDATE core.messages SET consumed_by_run_id=%s WHERE task_id=%s
                                       AND direction='inbound' AND consumed_by_run_id IS NULL""",
                                    (run["run_id"], run["task_id"]))
                return run
        except UniqueViolation:
            return None  # a concurrent claim took the same mutex/task; try again on the next poll

    async def turn_inputs(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        async with self.db.tx() as c:
            return list(await (await c.execute(
                "SELECT * FROM core.messages WHERE consumed_by_run_id=%s ORDER BY created_at",
                (run["run_id"],))).fetchall())

    async def complete(self, run: dict[str, Any], result: dict[str, Any]) -> None:
        async with self.db.tx() as c:
            await lock_project(c, run["project_id"])
            held = await self._held(c, run)
            held = await one(c, """UPDATE core.runs SET status='succeeded', result=%s, ended_at=now(),
                                   lease_until=NULL WHERE run_id=%s RETURNING *""", (Jsonb(result), run["run_id"]))
            if held["cancel_requested"]:
                await event(c, run["project_id"], "cancelled_run_result", task_id=run["task_id"], run_id=run["run_id"])
                return
            try:
                async with c.transaction():
                    await machine.on_result(c, held, result)
            except machine.InvalidResult as exc:
                await self._fail(c, held, {"type": "invalid_result", "message": str(exc)}, retryable=True)
            except Exception as exc:   # a Kernel bug: running the model again cannot fix it, so tell the user now
                log.exception("handling the result of run %s failed", run["run_id"])
                await self._fail(c, held, {"type": "kernel_error", "message": f"{type(exc).__name__}: {exc}"[:2000]},
                                 retryable=False)

    async def fail(self, run: dict[str, Any], error: dict[str, Any], *, retryable: bool = True) -> None:
        async with self.db.tx() as c:
            await lock_project(c, run["project_id"])
            await self._fail(c, await self._held(c, run), error, retryable=retryable)

    async def heartbeat(self, run: dict[str, Any], lease_seconds: int = 900) -> bool:
        async with self.db.tx() as c:
            row = await one(c, """UPDATE core.runs SET lease_until=now() + make_interval(secs => %s)
                                  WHERE run_id=%s AND claim_token=%s AND status='running' AND NOT cancel_requested
                                  RETURNING run_id""", (lease_seconds, run["run_id"], run["claim_token"]))
            return row is not None

    async def recover_expired(self) -> int:
        """Runs whose worker vanished: retry (or hand to the user once retries are used up).

        The first scan is only a work list.  The locked read must check the lease again,
        because a heartbeat may renew it between the scan and project lock acquisition.
        """
        async with self.db.tx() as c:
            candidates = list(await (await c.execute(
                "SELECT run_id, project_id FROM core.runs WHERE status='running' AND lease_until < now()"
            )).fetchall())
        recovered = 0
        for row in candidates:
            try:   # one run that cannot be failed must not keep the others stuck
                async with self.db.tx() as c:
                    await lock_project(c, row["project_id"])
                    run = await one(c, """SELECT * FROM core.runs
                                       WHERE run_id=%s AND status='running'
                                         AND lease_until IS NOT NULL AND lease_until < now()
                                       FOR UPDATE""", (row["run_id"],))
                    if run:
                        # A publish may have partly applied: never retry it blindly. Nor a job that may have side
                        # effects, unless its manifest declares it idempotent: its outcome is unknown, a person checks.
                        spec = run["input"].get("spec") or {}
                        unknown = run["run_type"] == "job" and not spec.get("idempotent") \
                            and spec.get("type") != "service_restart"   # restarting a service again is harmless
                        await self._fail(c, run, {"type": "result_unknown" if unknown else "lease_expired",
                                                  "message": "worker stopped; the job may or may not have taken effect"
                                                  if unknown else "worker stopped"},
                                         retryable=run["run_type"] != "publish" and not unknown)
                        recovered += 1
            except Exception:
                log.exception("recovering expired run %s failed", row["run_id"])
        return recovered

    async def _held(self, c, run) -> dict[str, Any]:
        held = await one(c, "SELECT * FROM core.runs WHERE run_id=%s AND claim_token=%s AND status='running' FOR UPDATE",
                         (run["run_id"], run["claim_token"]))
        if held is None:
            raise LeaseLost(str(run["run_id"]))
        return held

    async def _fail(self, c, run, error: dict[str, Any], *, retryable: bool) -> None:
        if retryable and not run["cancel_requested"] and run["attempt"] < run["max_attempts"]:
            await c.execute("""UPDATE core.runs SET status='queued', error=%s, worker_id=NULL, claim_token=NULL,
                               lease_until=NULL, available_at=now() + make_interval(secs => %s) WHERE run_id=%s""",
                            (Jsonb(error), self.retry_backoff_seconds * run["attempt"], run["run_id"]))
            return
        status = "cancelled" if run["cancel_requested"] else "failed"
        failed = await one(c, """UPDATE core.runs SET status=%s, error=%s, ended_at=now(), lease_until=NULL
                                 WHERE run_id=%s RETURNING *""", (status, Jsonb(error), run["run_id"]))
        if run["run_type"] == "pm_turn":   # its input never reached a finished turn: the next PM turn reads it
            await c.execute("UPDATE core.messages SET consumed_by_run_id=NULL WHERE consumed_by_run_id=%s",
                            (run["run_id"],))
        if status == "failed":
            await machine.on_exhausted(c, failed)
