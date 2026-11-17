-- P0: platform-owned project identity and immutable review purpose per task.
ALTER TABLE core.projects DROP CONSTRAINT IF EXISTS projects_kind_check;
ALTER TABLE core.projects ADD CONSTRAINT projects_kind_check
    CHECK (kind IN ('kernel', 'business', 'chief_of_staff'));

ALTER TABLE core.tasks ADD COLUMN IF NOT EXISTS review_profile text;
UPDATE core.tasks t SET review_profile = CASE p.kind
    WHEN 'kernel' THEN 'kernel_maintenance'
    WHEN 'chief_of_staff' THEN 'chief_of_staff'
    ELSE 'project_review' END
FROM core.projects p WHERE t.project_id=p.project_id AND t.kind='evolution'
    AND t.review_profile IS NULL;
ALTER TABLE core.tasks DROP CONSTRAINT IF EXISTS tasks_review_profile_check;
ALTER TABLE core.tasks ADD CONSTRAINT tasks_review_profile_check CHECK (
    (kind='evolution' AND review_profile IS NOT NULL
        AND review_profile IN ('project_review', 'kernel_maintenance', 'chief_of_staff'))
    OR (kind<>'evolution' AND review_profile IS NULL));
