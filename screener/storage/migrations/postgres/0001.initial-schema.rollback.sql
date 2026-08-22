-- Rollback for 0001. Present so `yoyo rollback` is not a trap, but note that
-- rolling this back destroys every candidate record and the audit log with it.
-- The audit triggers block DELETE, not DROP.
--
-- Dropped children-first so the foreign keys never block a drop, and with
-- IF EXISTS throughout so a partially-applied migration can still be undone.
DROP TRIGGER IF EXISTS audit_log_no_delete ON audit_log;
DROP TRIGGER IF EXISTS audit_log_no_update ON audit_log;
DROP FUNCTION IF EXISTS audit_log_append_only();

DROP INDEX IF EXISTS idx_positions_open_reference;
DROP INDEX IF EXISTS idx_cache;
DROP INDEX IF EXISTS idx_jobs_claim;
DROP INDEX IF EXISTS idx_verdicts_candidate;
DROP INDEX IF EXISTS idx_cand_review;
DROP INDEX IF EXISTS idx_cand_run;

DROP TABLE IF EXISTS audit_log;
DROP TABLE IF EXISTS overrides;
DROP TABLE IF EXISTS verdicts;
DROP TABLE IF EXISTS jobs;
DROP TABLE IF EXISTS candidates;
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS rubrics;
DROP TABLE IF EXISTS positions;
DROP TABLE IF EXISTS users;
