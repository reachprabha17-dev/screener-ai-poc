-- Rollback for 0002. Drops the recorded grounds for every injection flag; the
-- flags themselves live in flags_json and are unaffected.
ALTER TABLE candidates DROP COLUMN IF EXISTS injection_findings_json;
