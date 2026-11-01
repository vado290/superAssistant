# Discord image input (0.46.2)

Scope: user images from a configured Discord PM or Supervisor channel to the
existing Codex LLM. Replies remain text. No outbound image delivery, PDF parsing,
image generation, arbitrary URL ingestion or automatic cross-project image transfer.

## Input and storage

- Static PNG, JPEG and WebP: at most 4 attachments per message, 5 MiB per file,
  25 million pixels per image. Pillow 12.3.0 verifies format and decodes the file;
  file extensions and Discord MIME metadata are not trusted.
- Guild filtering remains in the Discord adapter. Channel routing and user
  authorization are checked before downloading. Existing text commands keep their
  command semantics and do not consume attachments.
- Downloads accept HTTPS Discord attachment CDN URLs only, without redirects.
  Actual streamed bytes are capped. The batch has a 45-second download/validation
  timeout; individual HTTP operations have a 15-second timeout.
- All files in a message must pass validation before the message is routed.
  Unsupported attachments (including PDF), invalid images and failed downloads
  produce a visible rejection; the caption alone is not sent to the model.
- Files are content-addressed under `vaults/<project>/runtime/attachments/`, with
  temporary-file replacement and project-bound references. Repeated content is
  reused. The message JSON has an optional `images` list; no schema migration is
  needed. No images are embedded as Base64 in message text or database rows.
- A per-channel ingress lock prevents subsequent text from overtaking a pending
  image upload. Other channels and the outbound pump remain independent. Text in
  the same channel can wait for that bounded download.
- Discord event IDs retain existing database deduplication; already received image
  events are checked before another download.

## Model context and task continuity

Triage, Supervisor and PM executors attach images through the existing shared
model caller. `AgentTurnRequest.image_paths` defaults to an empty tuple. Only
image-bearing calls add `--image` arguments (and an end-of-options delimiter) to
`codex exec` or `codex exec resume`. A selected model must advertise image input
in the installed CLI catalog; otherwise the call fails explicitly instead of
silently falling back to text. This candidate supports the OpenAI provider path.

For ordinary chats, the image window is the same user's last 20 incoming messages
in the same channel and role, ending at the triggering message. For task runs,
images come from that task and its source message, bounded by the run's input
cutoff. Newly arrived PM inputs wait for their claimed turn. Source images of a
Supervisor-proposed task remain available after the user approves creation.

At most 12 unique unseen images are attached per turn, in message order, retaining
the most recent occurrences. If the limit excludes older images, the model is
explicitly told those images were not attached and to request a resend if needed.
The PM's existing session `seen` map records image hashes: continuation adds only
new images; a new stage/session rebuilds the bounded image context. A JSON-format
repair reuses the same session without attaching the images again.

When a text follow-up starts a task based on earlier images, the source message
retains an `image_context` field containing the executor-selected references.
This is distinct from `images`, which denotes actual attachments to that message.
The model cannot set this internal field through its output JSON.

Input filenames, message IDs and captions are identified as user data in the
prompt. The invocation's `request.json` records actual image paths, order and
hashes. Task ledger supplements retain image references. File bytes are checked
against their stored hash before use; missing or modified inputs fail visibly.

Current-message images cannot route to another project. For a text follow-up
depending on older images, `route.requires_images: true` triggers the same resend
notice. Unrelated text routing remains available. Task approvals, state transitions,
formal file publication and outbound message/Markdown attachment interfaces are
unchanged. Images do not grant shell or workspace permissions.

## Retention and deployment boundary

The feature adds no new service or database migration. Release installation and
Kernel service restarts use the existing `scripts/deploy.ps1` procedure.
The existing runtime Git exclusion is reused; the three currently configured
projects were checked to ignore the attachment directory and have no tracked
files there. No cleanup daemon is added: referenced images remain on disk for task
continuity. Files left after a database/host failure may be unreferenced; future
cleanup must check message references rather than delete by age alone.

Install the declared dependency through the normal release process and complete
a real Discord-to-vision check in a test project.
Automated tests use synthetic images, mocked Discord HTTP and a scripted model;
they establish delivery and workflow behavior, not visual recognition quality.
Before rollback, stop new image ingestion and drain image-bearing runs; the old
release does not understand pending image messages. Retain the image files.

## Verification

Tests
run in an isolated temporary Python environment and UUID-named temporary Postgres
databases, with `POSTGRES_DB=postgres` for database administration. Production
database contents, venvs, source checkout and supervisor configuration are not
changed. No live Discord messages or paid model requests are made by these tests.

### Initial candidate verification — 2026-11-01

- Base: `3baf9db0a69f456ee69b5feaba2538e1524684c3` (0.46.1). Changes are uncommitted
  in the isolated `codex/discord-image-input` worktree; no merge, push or deployment.
- Final full regression: **440 passed, 4 skipped, 0 failures**, 370.16 seconds.
  This includes 38 new image tests plus existing text, cards, approvals, task
  execution, cancellation, concurrency, validation and Git/file-delivery tests.
- The first full run found six cases whose shared test stub lacked the newly
  introduced `image_inputs` method. The stub now returns an empty list for its
  text-only scenarios. The original sandbox/write-permission assertions were
  retained, and the full suite was rerun successfully.
- Three existing scenario placeholders remain skipped (earlier rebuild/deferred
  cases); one existing skill-adoption test lacks its historical release fixture.
  No image test is skipped. No existing assertion was weakened.
- Environment: Python 3.10.18, pytest 9.1.1, pytest-asyncio 1.4.0, Pillow 12.3.0;
  pytest-xdist 3.8.0 was installed only in the temporary test environment.
  Command: `python -B -m pytest tests -n 4 -q -p no:cacheprovider --tb=short --durations=5`.
  `PYTHONDONTWRITEBYTECODE=1`, source-first `PYTHONPATH` and `POSTGRES_DB=postgres`
  were set; dependencies from the installed production package were imported
  read-only, never installed into or changed there.
- `git diff --check` passed. All 75 production Python files passed syntax checking.
- Candidate wheel build passed. Its 13 changed package files matched the source
  byte-for-byte and its metadata declared Pillow 12.3.0. Wheel SHA-256:
  `63c301d65e32409ef8b9f2b0db802a6c64699e472cccd885bae21074e800b6d4`.
  This is a packaging check, not a release: the package version is still 0.46.1.
- Production API, Dispatcher and Discord Gateway retained their original PIDs
  (10841, 10842, 10848). The main checkout remained clean. No production database
  migration, service restart or production dependency installation was performed.
- Raw final JUnit results on this workstation:
  `H:/AI/reports/kernel-image-input-20261101/regression.xml`.
- Remaining acceptance boundary: actual Discord CDN access, real model image
  recognition and live user experience require a later controlled end-to-end test.
  Automated checks do not claim that production already supports images.

### Release review — 0.46.2

The user subsequently authorized review, regression and deployment, with manual
Discord image upload if browser automation is unavailable. The earlier no-deploy
restriction applied to the initial implementation only.

Review found one deployment gap: Pillow was declared in `pyproject.toml` but
missing from `requirements/kernel.lock`. The release script installs that lock
before installing the wheel with `--no-deps`. The lock now pins Pillow 12.3.0;
`test_deployment_lock_covers_declared_runtime_dependencies` checks that every
declared runtime dependency is present with its declared version. No database
migration, new service, outbound image support or PDF support was added.

Review covered the authenticated Discord ingress, full-batch rejection, bounded
download and decoding, project-local references and hashes, source/task message
selection, fresh and resumed model calls, internal context provenance, task
approval/state ownership, text routing, and outbound compatibility. Existing
image-input tests exercise those paths; real visual recognition remains a
separate acceptance step. The installed model catalog advertises text and image
input for the configured `gpt-6-astra` model.

The 0.46.2 wheel was installed into the test venv using the deployment lock and
`--no-deps`. `pip check`, executor/gateway imports, Pillow 12.3.0 import, and 24
installed-package image/dependency tests passed. All 138 declared runtime package
files were compared byte-for-byte with the reviewed source. The packaged SQL
migrations intentionally do not include their documentation-only README.

Final pre-deployment regression of 0.46.2: **441 passed, 4 existing skips, zero
failures**, in 387.12 seconds. The four skips are the same three historical
scenario placeholders and one unavailable historical skill-adoption fixture
listed above. The source changes contain no SQL migration or service definition
change. The production dependency check passed before promotion; the 0.46.1
deployed wheel is retained as the rollback target.

An additional pre-promotion version check caught the separate API version
constant still reporting 0.46.1. The release runner was stopped at its test stage,
before stopping or changing production services. `assistant_kernel.__version__`
now agrees with the 0.46.2 distribution version; an explicit version-consistency
test guards this release requirement. The schema remains core6.0008.

Live acceptance: upload a non-sensitive PNG/JPEG/WebP in the validation project's
Supervisor channel. Put a random code and recognizable shapes in the image but
do not include the answer in the caption or filename. Ask for an image-only
description, without creating a task or changing files. Verify the Discord
message, stored image SHA, invocation image input and returned answer together.
