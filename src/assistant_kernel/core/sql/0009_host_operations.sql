-- Read-only host operations; no writes to task or run state.
CREATE TABLE core.host_operations (
 id uuid PRIMARY KEY,
 project_id text NOT NULL,
 owner_key text NOT NULL,
 run_id uuid NOT NULL REFERENCES core.runs(run_id),
 claim_token uuid NOT NULL,
 device_id text NOT NULL,
 action text NOT NULL,
 request_key text NOT NULL,
 state text NOT NULL DEFAULT 'queued' CHECK(state IN ('queued','running','succeeded','failed','expired','unknown')),
 dispatch_digest text,
 result jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 expires_at timestamptz NOT NULL DEFAULT now() + interval '120 seconds',
 started_at timestamptz,
 ended_at timestamptz,
 UNIQUE(project_id, owner_key, request_key)
);
CREATE INDEX host_operations_claim ON core.host_operations(device_id, state, created_at);
