-- Project-level cross-project access facts (P1).
-- Existing projects are imported as ready so this migration does not change their behavior.
-- No access policy is created: revision 0 means no cross-project content access.

CREATE TABLE core.project_lifecycle (
    project_id text PRIMARY KEY REFERENCES core.projects(project_id),
    lifecycle_state text NOT NULL CHECK (lifecycle_state IN ('pending', 'ready', 'failed', 'archived')),
    registration_origin text NOT NULL CHECK (registration_origin IN ('legacy_import', 'bootstrap', 'config_import')),
    ready_claim_sha256 text,
    metadata jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO core.project_lifecycle(project_id, lifecycle_state, registration_origin, ready_at)
SELECT project_id,
       CASE WHEN status = 'archived' THEN 'archived' ELSE 'ready' END,
       'legacy_import',
       created_at
FROM core.projects
ON CONFLICT (project_id) DO NOTHING;

CREATE INDEX project_lifecycle_discovery
    ON core.project_lifecycle(lifecycle_state, project_id)
    WHERE lifecycle_state = 'ready';

CREATE TABLE core.project_access_policies (
    source_project_id text NOT NULL REFERENCES core.projects(project_id),
    revision integer NOT NULL CHECK (revision > 0),
    is_current boolean NOT NULL DEFAULT true,
    envelope_json jsonb NOT NULL,
    envelope_sha256 text NOT NULL CHECK (envelope_sha256 ~ '^[0-9a-f]{64}$'),
    expires_at timestamptz,
    origin text NOT NULL CHECK (origin IN ('user', 'creation', 'request', 'legacy')),
    approval_ref text,
    provenance jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    superseded_at timestamptz,
    PRIMARY KEY (source_project_id, revision)
);
CREATE UNIQUE INDEX project_access_policies_current
    ON core.project_access_policies(source_project_id)
    WHERE is_current;
CREATE INDEX project_access_policies_expiry
    ON core.project_access_policies(source_project_id, expires_at)
    WHERE is_current;

CREATE TABLE core.project_access_requests (
    request_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_project_id text NOT NULL REFERENCES core.projects(project_id),
    source_run_id uuid REFERENCES core.runs(run_id),
    source_task_id uuid REFERENCES core.tasks(task_id),
    request_kind text NOT NULL CHECK (request_kind IN ('selected_read', 'full_policy')),
    target_projects jsonb NOT NULL DEFAULT '[]',
    read_scopes jsonb NOT NULL DEFAULT '[]',
    reason text NOT NULL,
    idempotency_key text NOT NULL,
    request_semantic_sha256 text NOT NULL CHECK (request_semantic_sha256 ~ '^[0-9a-f]{64}$'),
    candidate_expires_at timestamptz,
    status text NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected', 'withdrawn', 'expired', 'stale')),
    terminal_reason text,
    card_item_id uuid,
    approval_ref text,
    approved_revision integer,
    notice_json jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    resolved_at timestamptz,
    UNIQUE(source_project_id, idempotency_key)
);
CREATE INDEX project_access_requests_pending
    ON core.project_access_requests(source_project_id, created_at)
    WHERE status = 'pending';
CREATE UNIQUE INDEX project_access_requests_semantic_pending
    ON core.project_access_requests(source_project_id, request_semantic_sha256)
    WHERE status = 'pending';

CREATE TABLE core.project_bootstrap_claims (
    project_id text PRIMARY KEY,
    source_project_id text NOT NULL REFERENCES core.projects(project_id),
    source_task_id uuid NOT NULL REFERENCES core.tasks(task_id),
    spec_sha256 text NOT NULL CHECK (spec_sha256 ~ '^[0-9a-f]{64}$'),
    charter_sha256 text,
    approved_envelope_sha256 text,
    state text NOT NULL CHECK (state IN ('reserved', 'ready', 'cancelled')),
    external_evidence jsonb NOT NULL DEFAULT '{}',
    created_at timestamptz NOT NULL DEFAULT now(),
    ready_at timestamptz,
    UNIQUE(source_task_id)
);
