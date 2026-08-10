-- v6 schema: stored text, offsets, verification, decisions, two-phase runs.
--
-- Four groups of change, two of which need a table rebuild because SQLite cannot
-- alter a CHECK constraint:
--
--   * `runs.status` has to admit 'empty'. A folder with no files must not
--     present as `completed` — a reviewer opening a blank screen has to be told
--     why (17.2). Adding the value means rewriting the constraint, which means
--     rewriting the table.
--
--   * `jobs` UNIQUE goes from (run_id, file_path) to (run_id, phase, file_path).
--     Without the phase in the key, the phase-2 job for a résumé collides with
--     the phase-1 row that judged it and the verify pass silently enqueues
--     nothing.
--
-- Both rebuilds use create-copy-drop-rename rather than rename-create-copy-drop.
-- SQLite 3.25+ rewrites REFERENCES clauses in *other* tables when a table is
-- renamed, so renaming `runs` out of the way would silently repoint
-- `candidates.run_id` and `jobs.run_id` at `runs_old`. Building the replacement
-- under a temporary name and renaming it into place afterwards leaves the
-- existing references naming `runs` throughout, which is what we want.

-- --- candidates: the two text versions, offsets, verification, decisions -----
--
-- `candidates` stops being a results table here and becomes a full-text store
-- (~40 KB per résumé). Both text columns are PII and inherit the same access
-- boundary as data/ — and both are listed in purge_candidate (12.10).
ALTER TABLE candidates ADD COLUMN resume_text TEXT;
ALTER TABLE candidates ADD COLUMN sent_text TEXT;
ALTER TABLE candidates ADD COLUMN sent_text_sha256 TEXT;
ALTER TABLE candidates ADD COLUMN redaction_map_json TEXT;
ALTER TABLE candidates ADD COLUMN verifier_digest TEXT;
ALTER TABLE candidates ADD COLUMN escalation_reasons_json TEXT;
ALTER TABLE candidates ADD COLUMN verification_status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE candidates ADD COLUMN decision TEXT NOT NULL DEFAULT 'undecided';
ALTER TABLE candidates ADD COLUMN decided_by TEXT REFERENCES users(id);
ALTER TABLE candidates ADD COLUMN decided_at TEXT;

-- The enum CHECKs that would accompany these columns cannot be added by ALTER,
-- and rebuilding `candidates` to get them would mean copying every résumé in the
-- database. Pydantic's Literal types enforce both values on the way in, which is
-- sufficient for the PoC; the MS SQL migration declares them properly.

-- --- verdicts: match blocks and everything phase 2 records -------------------
ALTER TABLE verdicts ADD COLUMN match_blocks_json TEXT;
ALTER TABLE verdicts ADD COLUMN negation_suspected INTEGER NOT NULL DEFAULT 0;
ALTER TABLE verdicts ADD COLUMN support TEXT;
ALTER TABLE verdicts ADD COLUMN suggested_verdict TEXT;
ALTER TABLE verdicts ADD COLUMN verifier_rationale TEXT;
ALTER TABLE verdicts ADD COLUMN absence_confirmed INTEGER;
ALTER TABLE verdicts ADD COLUMN absence_evidence TEXT;

-- --- overrides: what the decision was before -------------------------------
ALTER TABLE overrides ADD COLUMN old_decision TEXT;

-- --- runs: rebuilt for phase, the verifier, file_count and status 'empty' ---
CREATE TABLE runs_v6 (
  id TEXT PRIMARY KEY,
  position_id TEXT NOT NULL REFERENCES positions(id),
  rubric_id TEXT NOT NULL REFERENCES rubrics(id),
  folder TEXT NOT NULL,
  judge_model TEXT NOT NULL,
  judge_digest TEXT NOT NULL,
  verifier_model TEXT,
  verifier_digest TEXT,
  verification_enabled INTEGER NOT NULL DEFAULT 1,
  prompt_hash TEXT NOT NULL,
  redaction_on INTEGER NOT NULL,
  num_ctx INTEGER NOT NULL,
  num_predict INTEGER NOT NULL,
  seed INTEGER NOT NULL,
  app_version TEXT NOT NULL,
  reproducibility_rate REAL,
  escalation_rate REAL,
  file_count INTEGER NOT NULL DEFAULT 0,
  phase TEXT NOT NULL DEFAULT 'judge' CHECK (phase IN ('judge','verify','done')),
  status TEXT NOT NULL DEFAULT 'pending'
    CHECK (status IN ('pending','running','completed','empty','failed','aborted')),
  reviewed_by TEXT REFERENCES users(id),
  reviewed_at TEXT,
  created_by TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT
);

-- Existing runs are phase-1-only work that already finished under one model, so
-- they carry no verifier and are marked 'done' rather than 'judge' — a completed
-- v4 run must not start a verify pass when the worker next comes up.
INSERT INTO runs_v6 (
  id, position_id, rubric_id, folder, judge_model, judge_digest,
  verifier_model, verifier_digest, verification_enabled, prompt_hash,
  redaction_on, num_ctx, num_predict, seed, app_version,
  reproducibility_rate, escalation_rate, file_count, phase, status,
  reviewed_by, reviewed_at, created_by, created_at, started_at, finished_at)
SELECT
  id, position_id, rubric_id, folder, judge_model, judge_digest,
  NULL, NULL, 0, prompt_hash,
  redaction_on, num_ctx, num_predict, seed, app_version,
  reproducibility_rate, escalation_rate,
  (SELECT COUNT(*) FROM jobs j WHERE j.run_id = runs.id),
  'done', status,
  reviewed_by, reviewed_at, created_by, created_at, started_at, finished_at
FROM runs;

DROP TABLE runs;
ALTER TABLE runs_v6 RENAME TO runs;

-- --- jobs: rebuilt for phase in the identity and the claim index ------------
CREATE TABLE jobs_v6 (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  phase TEXT NOT NULL DEFAULT 'judge' CHECK (phase IN ('judge','verify')),
  file_path TEXT NOT NULL,
  file_sha256 TEXT,
  candidate_id INTEGER REFERENCES candidates(id),
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
  UNIQUE(run_id, phase, file_path)
);

INSERT INTO jobs_v6 (
  id, run_id, phase, file_path, file_sha256, candidate_id, status, attempts,
  claimed_by, claimed_at, heartbeat_at, heartbeat_seq, last_error,
  created_at, updated_at)
SELECT
  id, run_id, 'judge', file_path, file_sha256, NULL, status, attempts,
  claimed_by, claimed_at, heartbeat_at, heartbeat_seq, last_error,
  created_at, updated_at
FROM jobs;

DROP INDEX IF EXISTS idx_jobs_claim;
DROP TABLE jobs;
ALTER TABLE jobs_v6 RENAME TO jobs;
CREATE INDEX idx_jobs_claim ON jobs(status, run_id, phase, id);

-- --- traces removed (18) ----------------------------------------------------
--
-- Superseded by `sent_text`. A separate JSONL store of full résumé text was a
-- second PII store to index, retain, permission and purge; the same content now
-- lives in the database where all of that already exists. Schema-failure
-- debuggability comes back as failure-only capture, which writes nothing on a
-- healthy run.
--
-- NOTE: this drops the *index*. Files already written under data/traces/ are not
-- touched by a migration and must be disposed of separately — a dropped index
-- with the files still on disk is the exact failure the traces table existed to
-- prevent.
DROP INDEX IF EXISTS idx_traces_sha;
DROP TABLE IF EXISTS traces;

-- --- indexes ----------------------------------------------------------------
--
-- The cache key gains `verifier_digest`: verification is part of the stored
-- result now, so changing the verifier has to invalidate it.
DROP INDEX IF EXISTS idx_cache;
CREATE INDEX idx_cache ON candidates(
  file_sha256, position_id, rubric_hash, judge_digest, verifier_digest,
  prompt_hash, redaction_on, num_ctx, app_version
) WHERE cacheable = 1;

-- The review queue is filtered by run, then by whether a human still has work to
-- do on it. That is the query the list screen runs on every load (15.4).
CREATE INDEX idx_cand_review ON candidates(run_id, review_required, decision);
