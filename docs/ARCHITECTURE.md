# Kernel architecture

The Kernel runs every project of the personal super assistant. A project has a vault
(`assistant-data/vaults/<project>/`: charter, agents, skills, files, run evidence) and two Discord channels:
#pm (requests, conversation with the PM agent) and #supervisor (cards, consultation with the Supervisor agent).

## Core principles (a change must keep all of them)
1. **One state owner.** Only `core/machine.py` changes task state; only `machine.py` and `kernel.py` write
   `core.runs`; only `cards.py` writes `core.interactions` (checked by `tests/core/test_core_rules.py`).
   Executors return result dicts; they never change state themselves.
2. **One cursor per active task.** An active task waits on exactly one thing: one run or one card item
   (`rules.CURSOR_RUN`, `invariants.py`). One active task per project; others queue.
3. **One card per project.** Everything that needs the user is an item on the project's single card in #supervisor.
   A handled or replaced item gets a new id, so old buttons are recognised as stale.
4. **Approvals are derived, never cleared by hand.** An approval stores the sha of what was approved
   (document revision, commit); it is valid only while the current sha still matches (`rules.approval_valid`).
5. **Claim in one statement.** `Kernel.claim` checks every execution condition in a single SQL statement;
   it first locks the candidate project, then rechecks all conditions in the claiming UPDATE so skill adoption cannot race a new Run.
   Leases expire and are recovered; a stale result (not the task's current cursor) is discarded.
6. **Contracts vs skills.** What Kernel code parses or relies on lives in `prompts/core/` and changes with the code.
   Skills are project-owned templates and can change freely.
7. **Kernel writes what follows from state.** Files that mirror database state (charter archive, iteration log,
   shared context, worktree cleanup) are written by `vault_sync.py` on the dispatcher's maintenance tick, each
   step idempotent.
8. **Each agent owns its skills and works in its own folder** (ROUND5). Skills live only in
   `agents/<agent>/.agents/skills/` (one copy per agent that uses a skill); no `.agents/skills` above an agent's
   folder and no `AGENTS.md` in a project (`skills.layout_problems`, checked at validation). A model call runs in
   the agent's folder, so Codex lists exactly that agent's skills; Kernel injects only the core skills of the
   current stage in full and switches off the skills Codex ships with. Code-delivery build / review run in the
   task worktree and get Kernel's on-demand skill list instead.
9. **A task waiting 7 days for the user ends on its own** (`Kernel.expire_idle_tasks`), so it never blocks the
   project's next task.

## Flow of a task
user message in #pm → triage (chat / start a task / route) → task `queued` → `supervising` (Supervisor proposes
the next step) → card item (user confirms) → `pm` (one PM turn of the current stage) → `supervising` → …
Stages: requirements → design → build → review → release (full track) or requirements → build → release (quick
track). Build output is a candidate bundle (file delivery) or a commit in the task's worktree (code delivery);
`validating` runs Kernel validation; release needs the user's approval; `publishing` applies it.
A project-creation task (`bootstrap`) only drafts its charter; approval creates the project. A weekly
`evolution` task reviews the project and proposes follow-up tasks.

## Chief of Staff
A Chief of Staff is an independent project with `core.projects.kind=chief_of_staff`, not a third agent role.
Creation is approved through the normal charter/bootstrap flow, including its Discord category, PM/Supervisor channels
and gateway routing. `project_kind` in project.yaml is descriptive only. Existing projects remain business/kernel.
Evolution tasks freeze `review_profile` from database identity: project_review, kernel_maintenance or chief_of_staff.
CoS is excluded from the ordinary weekly scheduler; `/evolve` queues its bounded manual review. Pending/failed
projects are not scheduled. There is no operation task kind or permanent active task. Identity grants no cross-project
read/write access. Portfolio content uses Project Access; without a policy its business-content coverage is unknown.
The ordinary routing directory exposes public registration only (name, purpose and charter routing section), independently
of Project Access. CoS uses authorized portfolio views, not legacy handoffs. read_portfolio_snapshot provides bounded current
catalog/summary pages, policy revision, observation time, omissions and continuation. Every page rechecks lease, expiry,
active/ready status and policy; a changed revision invalidates the cursor. Background archives are not exported in P1.
Kernel maintenance separately receives platform counts without goals, messages, errors or private background. Legacy raw
digest and shared-candidate scans are disabled; authorizations must be configured explicitly, never inferred from
share_with_kernel. CoS uses the public operation/evolution templates for its own outputs, and observes targets without
operating them. Template rollout compares Kernel version plus shipped template fingerprint, preserving customized copies.
Shared background ownership is unchanged pending P3. The first live CoS uses CHIEF_OF_STAFF_CHARTER.md through normal
bootstrap. The platform project is kind kernel, never a portfolio business target. See CHIEF_OF_STAFF_PLAN.md.

## Modules (`src/assistant_kernel/core/`)
| Module | Role |
|---|---|
| `kernel.py` | API used by gateway and dispatcher: receive messages, card actions, claim/complete/fail runs, digests |
| `machine.py` | the state machine: every transition, card item and run enqueue |
| `rules.py` | stage moves, approvals, creation parameters — pure functions |
| `cards.py` | the per-project card |
| `dispatch.py` / `dispatch_main.py` | claims runs and executes them; maintenance tick (lease recovery, evolution schedule, vault sync, job runtime), each step isolated so a failing one never stops claiming |
| `executors/` | `pm` (PM turns), `supervisor`, `triage`, `delivery` (validation, publish), `bootstrap`, `job`, plus helpers (`common`, `state` ledger, `digest`) |
| `tools.py` | the MCP tool server for model turns; every call checks the run's lease |
| `drafts.py` | task documents as drafts edited through tools, saved as immutable revisions at turn end |
| `skills.py` / `agents.py` | skill loading by `metadata.load` rules; agent config loading |
| `skill_updates.py` | project skill-adoption batches, verified full-directory baselines, guarded writes and recovery; disabled without an approved policy; see SKILL_ADOPTION.md |
| `gitdelivery.py` | code delivery: worktrees, Kernel commits, tests on the commit, fast-forward publish |
| `runtime.py` / `jobs.py` | released `jobs.yaml` (vault top level; code delivery: repository root on main) → scheduled jobs and supervisord services of projects |
| `vault_sync.py` | Kernel's idempotent file writes that follow from state; template rollout per Kernel version (D24); imports commits made outside tasks into the change stream; pushes project history to GitHub |
| `projectgit.py` | each project folder (and each folder it may write to) is a repository: snapshot before a release, commit after it, private GitHub remote via `gh` |
| `projectmap.py` | the project map `map.yaml` (one index entry per file; every product Python file listed), checked at validation, `map_lookup` tool |
| `models.py` | the model settings `assistant-data/config/models.yaml`: profiles, default profile, profile per role; merged into each agent's config at load time (0.46.1) |
| `credentials.py` | the project's credentials `runtime/credentials.json`: look first, ask with a `credential` question, never in the conversation |
| `gateway/` | Discord gateway: messages, cards, buttons, text commands (`/status /pending /cancel /evolve /jobs /iterations /grants /secrets /models`); `setup.py` checks a bot token for the dashboard |
| `i18n.py` | text and language rules (ROUND6): interface text from `i18n/en.yaml`, the English headings Kernel parses (older Chinese headings still found), contract placeholders (`{{heading:key}}`), the language note that makes models reply in the user's language |
| `sql/` | migrations, applied in order, never edited after applying |

## Dispatcher resource and recovery boundaries
`CORE_MODEL_CAP` is a per-dispatcher limit for PM, Supervisor, triage and agent jobs. Script jobs and service
restarts remain claimable when model capacity is full. This is not a cross-process model quota.
Lease recovery rechecks expiry after locking the project and run. Script cancellation stops its POSIX process
group and tolerates an already-exited group; this is cleanup, not a sandbox or proof that a disconnected writer
has stopped. Automatic execution recipes remain outside this change's verified capability boundary.

## Processes (supervisord in the AI container)
`kernel-core-dispatcher`, `kernel-core-gateway`, `kernel-api` (health/version and the dashboard at `/dashboard`), plus
project services `proj-<project>-<service>` registered by the job runtime. Deploy: `scripts/deploy.ps1` (the user runs it).
Deployment is forward-only for 0.46.4: read-only migration preflight, backup-tool check, stop Kernel writers, database
backup and Supervisor configuration snapshot, then install/migrate/start. Before production installation changes, an
aborted backup restarts the original services; thereafter failure stops Kernel services and retains recovery artifacts
for forward repair. No old-wheel downgrade or automatic database restore. Backup archive listing checks structure, not
restore completeness. Backups include the configured database, which may be shared: a manual restore needs a separate
impact decision. Full packaged tests remain required; no new Docker images or containers for verification.

## Languages (ROUND6)
The interface is English: Kernel's own text (cards, buttons, notices, commands, the dashboard, files Kernel writes into
projects) comes from `i18n/en.yaml`, never from literals in code. Contracts, skills, roles and templates are English. Every
model turn ends with a language note: models reply in the language the user writes in (the latest message, otherwise the
language the user mostly writes in, otherwise English), and the text of documents follows it, while document headings stay
in English — the parser also accepts the Chinese headings of older documents.

## Model routing and prompt usage
Models v1 remains readable; v2 adds role/stage routes, provider protocol records and project purpose bindings.
Agent explicit fields remain exceptions. Plans, catalog and execution share models.route. Product calls use
model_gateway through released purposes; Codex remains the agent harness. Ordinary API providers need no CLI.
Release approval freezes the plan profiles; successful publication activates bindings idempotently before closing.
Operation uses an independent consultation model-change card for existing project purposes. Global policy and
embedding migrations remain tasks. No automatic vendor fallback or speculative installation is included.
Fixed role/output rules precede mutable context. The language reminder stays at the end for compatibility and is
also in the fresh-session fixed prefix. PM resumed deltas are preserved. Context occupancy, invocation usage and
session totals have separate metadata; repair calls preserve individual receipts. Unknown usage remains unknown.
Adapter/configuration readiness is not a real capability test or measured cache saving. Non-Agent release gates require
a successful gateway receipt bound to model, endpoint and credential revision; review still evaluates output quality.
Operation switches require the same existing capability evidence; unverified targets use the product build preflight.
Setup reuses existing credentials, and a different account/endpoint uses a new provider instance. Explicit routing
endpoints precede stored regional endpoints, which precede catalog defaults. Product chat supplies stable system rules
to protocol-aware cache policies; dynamic input is excluded from cache keys. Provider-reported usage determines hits.
