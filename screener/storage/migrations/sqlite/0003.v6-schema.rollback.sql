-- Rollback for 0003.
--
-- **Lossy by construction.** Rolling back drops `resume_text`, `sent_text`, the
-- redaction map, every phase-2 verdict column and all decision state. The v4
-- schema has nowhere to put them, so this is a one-way door in practice: take a
-- copy of the database first if the decisions matter.
--
-- Runs and jobs are rebuilt back to their v4 shape. Phase-2 jobs are discarded
-- rather than collapsed into phase 1, because a v4 queue has no way to express
-- them and folding them in would re-judge every candidate.

DROP INDEX IF EXISTS idx_cand_review;
DROP INDEX IF EXISTS idx_cache;

CREATE TABLE traces (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  file_sha256 TEXT NOT NULL,
  trace_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_traces_sha ON traces(file_sha256);

CREATE TABLE jobs_v4 (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  file_path TEXT NOT NULL,
  file_sha256 TEXT,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','claimed','done','failed')),
  attempts INTEGER NOT NULL DEFAULT 0,
  claimed_by TEXT,
  claimed_at TEXT,
  heartbeat_at TEXT,
  heartbeat_seq INTEGER DEFAULT 0,
  last_error TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(run_id, file_path)
);
INSERT INTO jobs_v4 (
  id, run_id, file_path, file_sha256, status, attempts, claimed_by, claimed_at,
  heartbeat_at, heartbeat_seq, last_error, created_at, updated_at)
SELECT
  id, run_id, file_path, file_sha256, status, attempts, claimed_by, claimed_at,
  heartbeat_at, heartbeat_seq, last_error, created_at, updated_at
FROM jobs WHERE phase = 'judge';
DROP INDEX IF EXISTS idx_jobs_claim;
DROP TABLE jobs;
ALTER TABLE jobs_v4 RENAME TO jobs;
CREATE INDEX idx_jobs_claim ON jobs(status, run_id, id);

CREATE TABLE runs_v4 (
  id TEXT PRIMARY KEY,
  position_id TEXT NOT NULL REFERENCES positions(id),
  rubric_id TEXT NOT NULL REFERENCES rubrics(id),
  folder TEXT NOT NULL,
  judge_model TEXT NOT NULL,
  judge_digest TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  redaction_on INTEGER NOT NULL,
  num_ctx INTEGER NOT NULL,
  num_predict INTEGER NOT NULL,
  seed INTEGER NOT NULL,
  app_version TEXT NOT NULL,
  reproducibility_rate REAL,
  escalation_rate REAL,
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','completed','failed','aborted')),
  reviewed_by TEXT REFERENCES users(id),
  reviewed_at TEXT,
  created_by TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);
-- 'empty' has no v4 equivalent; it becomes 'completed', which is precisely the
-- blank screen 17.2 introduced the status to avoid.
INSERT INTO runs_v4 (
  id, position_id, rubric_id, folder, judge_model, judge_digest, prompt_hash,
  redaction_on, num_ctx, num_predict, seed, app_version, reproducibility_rate,
  escalation_rate, status, reviewed_by, reviewed_at, created_by, created_at,
  started_at, finished_at)
SELECT
  id, position_id, rubric_id, folder, judge_model, judge_digest, prompt_hash,
  redaction_on, num_ctx, num_predict, seed, app_version, reproducibility_rate,
  escalation_rate,
  CASE status WHEN 'empty' THEN 'completed' ELSE status END,
  reviewed_by, reviewed_at, created_by, created_at, started_at, finished_at
FROM runs;
DROP TABLE runs;
ALTER TABLE runs_v4 RENAME TO runs;

CREATE INDEX idx_cache ON candidates(
  file_sha256, position_id, rubric_hash, judge_digest, prompt_hash,
  redaction_on, num_ctx, app_version
) WHERE cacheable = 1;

ALTER TABLE overrides DROP COLUMN old_decision;

ALTER TABLE verdicts DROP COLUMN absence_evidence;
ALTER TABLE verdicts DROP COLUMN absence_confirmed;
ALTER TABLE verdicts DROP COLUMN verifier_rationale;
ALTER TABLE verdicts DROP COLUMN suggested_verdict;
ALTER TABLE verdicts DROP COLUMN support;
ALTER TABLE verdicts DROP COLUMN negation_suspected;
ALTER TABLE verdicts DROP COLUMN match_blocks_json;

ALTER TABLE candidates DROP COLUMN decided_at;
ALTER TABLE candidates DROP COLUMN decided_by;
ALTER TABLE candidates DROP COLUMN decision;
ALTER TABLE candidates DROP COLUMN verification_status;
ALTER TABLE candidates DROP COLUMN escalation_reasons_json;
ALTER TABLE candidates DROP COLUMN verifier_digest;
ALTER TABLE candidates DROP COLUMN redaction_map_json;
ALTER TABLE candidates DROP COLUMN sent_text_sha256;
ALTER TABLE candidates DROP COLUMN sent_text;
ALTER TABLE candidates DROP COLUMN resume_text;
