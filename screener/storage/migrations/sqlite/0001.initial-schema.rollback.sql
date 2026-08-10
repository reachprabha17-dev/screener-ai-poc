-- Rollback for 0001. Present so `yoyo rollback` is not a trap, but note that
-- rolling this back destroys every candidate record and the audit log with it.
-- The audit triggers block DELETE, not DROP.
DROP INDEX IF EXISTS idx_cand_run;
DROP INDEX IF EXISTS idx_cache;
DROP TRIGGER IF EXISTS audit_log_no_delete;
DROP TRIGGER IF EXISTS audit_log_no_update;
DROP TABLE IF EXISTS audit_log;
DROP INDEX IF EXISTS idx_traces_sha;
DROP TABLE IF EXISTS traces;
DROP TABLE IF EXISTS overrides;
DROP INDEX IF EXISTS idx_verdicts_candidate;
DROP TABLE IF EXISTS verdicts;
DROP TABLE IF EXISTS candidates;
DROP INDEX IF EXISTS idx_jobs_claim;
DROP TABLE IF EXISTS jobs;
DROP TABLE IF EXISTS runs;
DROP TABLE IF EXISTS rubrics;
DROP TABLE IF EXISTS positions;
DROP TABLE IF EXISTS users;
