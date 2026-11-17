-- Keep 0011 immutable. The project row plus lifecycle.pending is the sole creation reservation.
-- Refuse unexpected data rather than deleting an unknown deployment's reservation/history.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM core.project_bootstrap_claims) THEN
        RAISE EXCEPTION 'project_bootstrap_claims contains data; reconcile it before simplifying';
    END IF;
    IF EXISTS (SELECT 1 FROM core.project_access_requests WHERE request_kind <> 'selected_read') THEN
        RAISE EXCEPTION 'unsupported project access requests exist; reconcile them before simplifying';
    END IF;
END $$;

DROP TABLE core.project_bootstrap_claims;
ALTER TABLE core.project_access_requests
    DROP CONSTRAINT project_access_requests_request_kind_check,
    ADD CONSTRAINT project_access_requests_request_kind_check CHECK (request_kind = 'selected_read');
