-- Backfill projects registered while cross-project access was disabled.
-- Existing lifecycle facts (including pending/failed) always take precedence.
-- Registration does not grant cross-project content access.
INSERT INTO core.project_lifecycle(project_id, lifecycle_state, registration_origin, ready_at)
SELECT p.project_id,
       CASE WHEN p.status = 'archived' THEN 'archived' ELSE 'ready' END,
       'legacy_import',
       CASE WHEN p.status = 'active' THEN p.created_at ELSE NULL END
FROM core.projects p
ON CONFLICT (project_id) DO NOTHING;
