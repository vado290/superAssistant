-- Structured host arguments; preserve deployed read-only OBS operations.
ALTER TABLE core.host_operations
 ADD COLUMN arguments jsonb NOT NULL DEFAULT '{}',
 ADD COLUMN capability_revision text NOT NULL DEFAULT '1',
 ADD COLUMN effect text NOT NULL DEFAULT 'observe',
 ADD COLUMN caller_identity jsonb NOT NULL DEFAULT '{}',
 ADD COLUMN args_digest text;
