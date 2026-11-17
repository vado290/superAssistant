# Chief of Staff rollout

## Boundaries
Kernel owns state, permissions, scheduling and delivery. CoS owns user-level observation and advice. Each target project
owns its execution. CoS is an independent project with the existing PM/Supervisor and one-card/one-active-task rules.
No new agent role, operation task kind, free agent chat or direct writes to another project.

## P0 — Identity and entry points (implemented)
- Add database project kind chief_of_staff. Existing kinds are unchanged; a charter cannot create kernel.
- Freeze review_profile on evolution tasks from database identity; backfill older reviews by existing project kind.
- Create CoS through an approved charter with project_kind: chief_of_staff. Use vault-files, no outside directories,
  and at most project_management. Cross-project reads still need their own explicit access policy.
- Bootstrap creates/finds PROJECT · <project-id>, #pm and #supervisor, writes routing atomically and returns channel links.
  A retry reuses channels and restores its own failed reservation to pending before activating it as ready.
- Append the platform CoS contract to model calls for PM triage/turns and Supervisor consultations/evaluations.
- Keep manual /evolve; exclude CoS from ordinary weekly scheduling until P1. Skip pending/failed projects.
- Keep shared context and Kernel template-maintenance statistics operational; gate the legacy review scan by true kernel
  identity. No private background migration or automatic all-project access.

Acceptance: legacy creation works; CoS identity survives bootstrap/retry; ordinary config cannot grant identity; manual
review has the CoS profile; generic scheduling excludes it; Discord creation/routing is repeatable. No production deploy
or live channel creation is performed by source implementation/tests.

## P1 — Authorized portfolio reads (implemented; verification below)
Provide one bounded portfolio snapshot through Project Access: observed_at, coverage, omissions, policy revision and
pagination. Replace legacy catalog/candidate/digest bypasses, retaining authorized template-maintenance evidence for
Kernel. Keep platform operational metrics separate from project content. Define an independent CoS review schedule only
after its data coverage and workload rules are implemented.
Acceptance: denial/revocation/expiry and non-ready projects are enforced; no credentials/runtime/arbitrary paths leak;
ordinary projects and Kernel maintenance retain their intended behavior.

## First operational release (2026-11-16)
- CoS is instantiated from `CHIEF_OF_STAFF_CHARTER.md`, not as an empty Discord shell. Its live functions are goal and
  decision analysis, portfolio catalog/summary review, option comparison, executable next steps, manual reviews and
  approved new-project preparation.
- The policy freezes current ready business projects with catalog/summary only. Future projects and detailed scopes need
  explicit policy changes. Migration 0015 marks the configured platform project as Kernel so it is not a business target.
- Pending bootstrap vaults are excluded from maintenance. Ordinary routing uses ready business/CoS public registration
  independently of Project Access. CoS portfolio content still requires authorization; its Git push is disabled by default.
- Discord is created only through the approved bootstrap: category, #pm, #supervisor, gateway routing and links.
- P2 one-way dispatch and P3 private-background ownership remain disabled. CoS must not claim delivery, receipts or
  migrated private memory before those phases are implemented and accepted.

## P2 — One-way coordination
Add leased, schema-checked single-hop requests with separate send authorization, provenance, idempotency, expiry,
minimal context and machine status. Target PM/Supervisor decides whether to queue/decline or seek the user. No automatic
reply loop or forwarding; no direct cross-project edits. Existing consultation routing remains a different operation.
Acceptance: retry never duplicates work; status is traceable; busy targets keep their cursor; requests are not user orders.

## P3 — Background ownership and migration
Keep platform environment/conventions in Kernel. Separate CoS-private facts/goals/assumptions from project-specific
context; give target projects authorized minimal views. Audit all injection and export paths before private data moves.
Preserve source/date, correction, expiry and migration receipts; migrate in batches without deleting old sources until
acceptance. Do not silently reinterpret share_with_kernel as CoS access. Sensitive records require an explicit policy.
Acceptance: no duplicate authority, unauthorized injection or health-background propagation; corrections override inference.

## Deployment and operational creation
Source changes do not create a live project. After the normal release and user deployment, use the existing Kernel
project-creation flow with an approved CoS charter. Discord bot guild/configuration and Manage Channels permission must
already be available. Successful bootstrap returns both channel links; readiness follows completed files/channels/routing
and creation policy activation. Do not create disconnected channels manually before registering the project.

## P0 verification (2026-11-16)
- CoS/bootstrap/evolution/core ownership tests: 39 passed.
- Triage and Project Access compatibility tests: 53 passed, 1 skipped, 1 failed.
- The failing real stdio MCP subprocess test starts Windows' default Proactor event loop, which psycopg async rejects.
  It is not counted as passed. Re-run in the normal Linux runtime before release; the full repository suite was not run.
- Windows test execution used the existing isolated PostgreSQL test container and per-test temporary databases.
  A temporary test adapter disabled Linux-only login-file synchronization (fcntl); authentication synchronization was
  not tested. Discord calls used a fake/mock transport, so no live channels were created or validated.
- P0 is source implementation, not deployment or live project provisioning. Keep automatic CoS scheduling disabled
  until P1 and use the normal approved bootstrap after release to create the actual Discord entry points.

## P1 implementation (2026-11-16)
- One leased read_portfolio_snapshot service/tool supplies bounded current catalog/summary pages, policy revision,
  observed_at, coverage, omitted scopes and continuation. Policy changes invalidate continuation; expiry/readiness and
  source lease are rechecked. Pages are current reads, not an atomic historical portfolio or live health checks.
- Portfolio content paths use this service; routing uses the separate public registration directory described below.
  Kernel no longer scans raw target digests/background archives; platform task/failure/retry counts remain separate.
  Existing share_with_kernel is not converted into a grant.
- Shared background stays in place; private export and candidate discovery wait for P3. No new read scope, migration,
  agent role or task state was added. Automatic CoS reviews stay disabled until a separate scheduling design is accepted.
- Verified the incoming assistant-operation 3.5.0 update. Extended it to 3.6.0 for CoS ownership boundaries; evolution-review
  2.5.0 and kernel-maintenance 1.3.0 now distinguish authorized evidence, platform counts and recommendations.
- Bootstrap installs the same current public templates in CoS. Existing projects sync by Kernel version plus template-set
  fingerprint, so same-version shipped updates are detected; customized/unproven copies stay and appear as adoption items.
- Discord provisioning still uses the P0 approved bootstrap, PM/Supervisor channels and routing; no disconnected manual
  channels or live project were created by this source implementation.

## P1 verification and release gate (2026-11-16)
- AST parsing passed for 86 core/test Python files before final corrections.
- The first Linux test attempt encountered pytest capture failures on a Windows-mounted temporary directory. The
  completed retry used container-local temporary files: 102 passed, 2 failed. Real stdio MCP access, including the new
  portfolio tool, passed. One failure was a concurrently reintroduced disabled-access legacy catalog fallback violating
  the CoS boundary; the other was a Git-based archival test in a temporary image without Git.
- Historical intermediate fix (superseded by the 0.46.4 routing decision below): CoS reported disabled/unknown and
  ordinary routing received ready business identifiers only. That intermediate correction was not rerun.
- Concurrent changes were observed in common.py and this plan during validation. Results do not certify an immutable
  final revision. Validate the integrated revision before release.
- Differing skill support files now block automatic replacement and remain adoption candidates; an unchanged SKILL.md
  alone cannot authorize overwriting local support-file customization. Same-version template differences are detected.
- Linux validation used Python 3.12 and available dependencies (MCP 2.3.0), not the declared Python 3.10 locked environment.
  Full suite, Git-backed rollout, exact locked-environment validation and live Discord provisioning remain release gates.
- Historical cleanup claim was corrected after inspection: leftover verification containers were subsequently removed.
  No production deployment, live project/channel creation, private
  background migration or automatic CoS schedule was performed.

## 0.46.4 usable release (2026-11-16)
- Routing directory is public registration only: registered name/purpose plus the charter routing section of ready
  business and CoS projects (Kernel excluded). It no longer depends on Project Access, so Kernel #pm can route to CoS.
- Deployment enables Project Access (supervisord environment). Ordinary projects keep self-only reads; CoS reads its
  approved catalog/summary policy.
- CoS working method moved from the core prompt to the `assistant-chief-of-staff` skill (`load.project_kinds`); the core
  prompt keeps identity and hard boundaries. Runtime text no longer names rollout phases.
- CoS goes live first as an advisor; one-way requests (P2) follow as a patch.

## Integrated corrections and release review (2026-11-17)
- Routing methods and the new-project contract now distinguish catalog visibility, routing authority and
  project_management. CoS prepares recommendations and may initiate approved bootstrap tasks; it cannot send handoffs.
- Newly approved business/CoS projects receive daily_operations=true. Existing projects and explicit opt-outs are not
  changed. This enables bounded busy-message triage and own-job observations, not automatic weekly CoS reviews.
- Runtime skill selection uses database project kind across PM, Supervisor, triage and agent jobs, and filters CoS
  capabilities consistently. External directory grants are rejected both before proposal and at the grant write boundary.
- Shared assistant-operation remains 3.6.0; assistant-triage-routing is 1.3.0. Customized live skill copies still need
  normal adoption. No production project, channel or settings are created by these source corrections.

Release blockers to resolve before promotion:
1. Earlier dependency checks inherited the container's PYTHONPATH and saw unrelated base packages. Clearing PYTHONPATH
   and PYTHONHOME yields passing pip checks in existing test/prod environments and a fresh locked validation venv.
   A passing full installed-wheel suite is still required.
2. Migration 0014 requires non-null review_profile for evolution tasks. The installed old production code and all three
   marked rollback wheels (0.46.1, 0.46.2, 0.46.3) do not write that field. Wheel-only rollback is incompatible after migration.
   Resolved by the user's 2026-11-17 forward-only recovery decision below; do not edit applied migrations.
3. Migration 0014 backfills profiles before 0015 corrects a legacy Kernel business identity. Historical Kernel reviews can
   therefore retain project_review. Resolve whether to preserve that history or add a narrow forward correction.
4. Historical script blockers: production dependencies were updated outside the protected block, Supervisor exit codes
   were unchecked, and wheel rollback could not restore configuration/dependencies. Replaced by forward-only recovery
   below; release-branch validation and backup tooling remain required.
5. Full integrated regression and packaged-wheel validation remain gates; live Discord provisioning follows deployment.
   No new Docker images or containers are permitted for validation; use the existing AI environment and temporary databases.


Validation evidence (2026-11-17, existing AI container, Python 3.10.18):
- Final focused source run: 64 passed, 0 failed, 0 skipped (CoS and daily operations), 177.43 seconds.
- Core state-owner rules and skill-loader regression: 9 passed, 0 failed, 0 skipped, 26.23 seconds.
- CoS test fixtures now request the actual catalog/summary policy with valid business IDs and explicit Kernel identity.
  Added coverage for creation defaults, opt-out preservation, directory-grant refusal in consult/evaluate and at write,
  and database-kind/capability-aware skill selection. Daily-operation ID assertions compare UUIDs consistently.
- Broad suite collected 609 tests; its run was interrupted because projected duration substantially exceeded the short
  validation window. It reported test-fixture/assertion failures subsequently corrected above and hit a pytest cleanup
  KeyError on interruption. It is not a full-suite pass and does not certify the final integrated source.
- Wheel 0.46.4 built successfully without installing it. Required skills and migrations are present; all packaged source
  files match current source and there are no stale package files. SHA256:
  c4716360d2a2e0189b50bdc4c54248f18a6f6cad12c8f54d389652f8360603f4.
- git diff --check passed. Production API, dispatcher and gateway remained running with unchanged process IDs.
- No production deployment, migration, channel creation, virtual-environment change or Docker image/container creation.
  Test files, logs and a validation wheel were written to temporary locations in AI; tests used separate temporary DBs.
- Decision: feature-focused validation passes, production promotion is blocked by the release items above. This evidence
  covers simulated model/Discord transports, not live model quality or actual Discord provisioning.

## Approved forward-only recovery (2026-11-17)
The user accepted forward migration instead of an old-version compatibility/automatic rollback framework.
- scripts/deploy.ps1 now defaults to the whole tests directory. TestOnly exits before production preflight/backup.
- Preflight validates migration hashes in a read-only transaction and checks pg_dump/pg_restore availability.
- Before production installation, preserve the Supervisor configuration, stop Kernel writers and create a custom-format
  backup of the configured database; pg_restore --list checks its index. This is not a restore rehearsal or a snapshot
  of project files, and the configured database may have other users.
- A failure before production installation restarts the original services. From the first production dependency change
  onward, failure stops Kernel services and retains the wheel, database backup and configuration for forward repair.
  Never downgrade to an incompatible wheel or restore a live database automatically. Supervisor mutation exit codes are
  checked. Recovery artifacts are retained for explicit operator review rather than deleted by wheel cleanup.
- assistant-kernel-maintenance 1.4.0 documents the recovery policy, whole-system/main-workflow review, skill ownership,
  no new Docker images/containers, and the difference between source tests, deployment and live CoS creation.

Validation for this change:
- Existing AI container, Python test environment: test_harness_selfcheck.py 10 passed in 9.13 seconds. Real temporary
  database tests cover migration ordering, ledger hashes, transaction failure and a dry run with no ledger. Backup
  subprocess success/failure and password handling use mocks; no production backup or migration was performed.
- Windows PowerShell parser and 8 simulated deployment branches passed: success, TestOnly, missing backup tooling,
  backup failure, dependency-install failure, migration failure, Supervisor failure and unhealthy API. All Docker calls
  were mocked in this branch harness; no services were stopped or restarted by it.
- git diff --check passed before this evidence append. No implementation changes followed verification.

Final promotion checklist (open items are not release passes):
- [x] User accepted forward-only recovery; deployment code and maintenance skill agree.
- [x] Targeted migration tests and simulated deployment branches pass.
- [x] PostgreSQL client 18.7 installed in existing AI, compatible with server 18.6. A real custom-format backup restored
  into a second temporary database preserved its sentinel row; both temporary databases were removed.
- [x] Full installed-wheel suite in existing AI: 610 passed, 4 skipped in 933.04 seconds. Three skips are explicitly deferred
  scenario placeholders; one needs an unavailable historical skill fixture. Final maintenance-skill delta: 2 passed.
  Clean locked validation, existing test and existing prod pass pip check with inherited PYTHONPATH/PYTHONHOME cleared.
- [x] Preserve historical review profiles without editing applied migrations. Production inspection found no active Kernel
  evolution task; new reviews derive the corrected database identity. Historical labels do not block this release.
- [ ] Review/commit the final integrated source through the normal release process and record its wheel/version/hash.
- [ ] During authorized deployment: complete read-only preflight, actual backup and configuration capture before mutation.
- [ ] After deployment: verify services/version, then create CoS through the approved charter/bootstrap and validate real
  Discord PM/Supervisor routing plus authorized summaries. Verify ordinary-project daily operations separately.
No production deployment, production migration, live CoS creation or full-suite certification occurred in this change.
