-- The configured platform project is not a business portfolio target.
UPDATE core.projects SET kind='kernel', updated_at=now()
WHERE project_id='kernel' AND kind='business';
