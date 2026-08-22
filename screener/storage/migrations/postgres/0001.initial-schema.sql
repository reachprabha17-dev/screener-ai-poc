-- Initial Postgres schema (spec 12.2, 12.4).
--
-- **One migration, not a replay of the twelve SQLite ones.** Those carry the
-- history of a database that could not alter a constraint in place: 0004 builds
-- `positions_v8`, copies into it and renames it, purely to add a partial unique
-- index SQLite would not accept on the original table. Postgres needs none of
-- that, and replaying it would enshrine a workaround for a limitation this
-- backend does not have. What a fresh install needs is the end state, which is
-- what this file is. The SQLite set stays as it is; it is that backend's history
-- and still applies there.
--
-- Three things in here are load-bearing and easy to mistake for boilerplate:
--
--   * The audit_log triggers make append-only a property of the database rather
--     than a convention the application is trusted to follow. A convention
--     survives exactly until someone writes a cleanup script. SQLite spells this
--     `RAISE(ABORT)` inside the trigger body; Postgres needs a function, so the
--     mechanism differs while the guarantee does not.
--
--   * idx_cache is a PARTIAL index (WHERE cacheable). Transient failures are
--     stored for audit and shown to reviewers, but are structurally invisible to
--     cache lookup — enforced by the index rather than by a filter in Python
--     that a future query could forget (12.5).
--
--   * idx_positions_open_reference is UNIQUE but partial (WHERE status = 'open').
--     A reference may be reused once its requisition is closed, and must not be
--     duplicated while one is open.
--
-- **Booleans are real booleans here.** SQLite has no boolean type and stores
-- 0/1 in INTEGER columns; Postgres does, and using INTEGER to imitate SQLite
-- would carry a limitation across a boundary that exists to leave it behind.
-- The application passes Python `bool` and compares against `TRUE`, both of
-- which SQLite also accepts (3.23+), so one code path serves both backends.
--
-- **Timestamps stay TEXT (ISO-8601).** Deliberate, and worth revisiting: the
-- application writes `.isoformat()` and parses it back, and every ordering in
-- the codebase is lexicographic over that format, which is correct for ISO-8601
-- with a fixed offset. Moving to `timestamptz` is a real improvement — indexable
-- date ranges, no parsing — but it changes read paths in every store and is not
-- something to bundle into the migration that first makes Postgres work at all.

CREATE TABLE users (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  roles_json TEXT NOT NULL DEFAULT '["admin"]',
  auth_ref TEXT,                       -- LDAP DN or argon2 hash, later (21)
  active BOOLEAN NOT NULL DEFAULT TRUE,
  created_at TEXT NOT NULL
);

CREATE TABLE positions (
  id TEXT PRIMARY KEY,
  reference TEXT NOT NULL,             -- folder name under data/resumes/
  title TEXT NOT NULL,
  jd_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  created_by TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  closed_at TEXT
);

CREATE TABLE rubrics (
  id TEXT PRIMARY KEY,
  position_id TEXT NOT NULL REFERENCES positions(id),
  version INTEGER NOT NULL,
  criteria_json TEXT NOT NULL,
  rubric_hash TEXT NOT NULL,
  created_by TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  approved_by TEXT REFERENCES users(id),
  approved_at TEXT,
  UNIQUE(position_id, version)
);

CREATE TABLE runs (
  id TEXT PRIMARY KEY,
  position_id TEXT NOT NULL REFERENCES positions(id),
  rubric_id TEXT NOT NULL REFERENCES rubrics(id),
  folder TEXT NOT NULL,
  judge_model TEXT NOT NULL,
  judge_digest TEXT NOT NULL,
  verifier_model TEXT,
  verifier_digest TEXT,
  verification_enabled BOOLEAN NOT NULL DEFAULT TRUE,
  prompt_hash TEXT NOT NULL,
  redaction_on BOOLEAN NOT NULL,
  num_ctx INTEGER NOT NULL,
  num_predict INTEGER NOT NULL,
  seed INTEGER NOT NULL,
  app_version TEXT NOT NULL,
  reproducibility_rate DOUBLE PRECISION,
  escalation_rate DOUBLE PRECISION,
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

CREATE TABLE candidates (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  filename TEXT NOT NULL,              -- NOTE: usually contains the person's name
  file_sha256 TEXT NOT NULL,
  score DOUBLE PRECISION,              -- NULL, never 0.0, when not scoreable (5)
  band TEXT,
  must_haves_met BOOLEAN,              -- NULL while unscored; not a third state
  scoreable BOOLEAN NOT NULL DEFAULT TRUE,
  review_required BOOLEAN NOT NULL DEFAULT FALSE,
  cacheable BOOLEAN NOT NULL DEFAULT TRUE,
  summary TEXT,
  notable_strengths_json TEXT,
  red_flags_json TEXT,
  flags_json TEXT,
  -- deferred-compliance columns: nullable now, un-backfillable later (21)
  source TEXT,
  consent_ref TEXT,
  retention_expires_at TEXT,
  objection_status TEXT,
  -- cache columns, denormalized from runs so a lookup is one index probe
  position_id TEXT NOT NULL,
  rubric_hash TEXT NOT NULL,
  judge_digest TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  redaction_on BOOLEAN NOT NULL,
  num_ctx INTEGER NOT NULL,
  app_version TEXT NOT NULL,
  scored_at TEXT NOT NULL,
  resume_text TEXT,
  sent_text TEXT,
  sent_text_sha256 TEXT,
  redaction_map_json TEXT,
  verifier_digest TEXT,
  escalation_reasons_json TEXT,
  verification_status TEXT NOT NULL DEFAULT 'pending',
  decision TEXT NOT NULL DEFAULT 'undecided',
  decided_by TEXT REFERENCES users(id),
  decided_at TEXT,
  UNIQUE(file_sha256, run_id)
);

CREATE TABLE jobs (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
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

CREATE TABLE verdicts (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  criterion_id TEXT NOT NULL,
  verdict TEXT NOT NULL,
  model_verdict TEXT NOT NULL,         -- what the model said, retained for audit
  evidence TEXT,
  verified BOOLEAN NOT NULL,
  match_ratio DOUBLE PRECISION,        -- persisted so thresholds tune against data (18)
  longest_span INTEGER,
  match_blocks_json TEXT,
  negation_suspected BOOLEAN NOT NULL DEFAULT FALSE,
  support TEXT,
  suggested_verdict TEXT,
  verifier_rationale TEXT,
  absence_confirmed BOOLEAN,           -- NULL when the check did not run
  absence_evidence TEXT,
  evidence_irrelevant BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE overrides (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  actor_id TEXT NOT NULL REFERENCES users(id),
  old_score DOUBLE PRECISION,
  old_band TEXT,
  old_decision TEXT,
  new_decision TEXT NOT NULL CHECK (new_decision IN ('advance','reject','hold')),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE audit_log (
  id INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
  ts TEXT NOT NULL,
  actor_id TEXT,
  action TEXT NOT NULL,
  entity TEXT,
  entity_id TEXT,
  detail_json TEXT
);

-- --- indexes -----------------------------------------------------------------

CREATE INDEX idx_cand_run ON candidates(run_id);
CREATE INDEX idx_cand_review ON candidates(run_id, review_required, decision);
CREATE INDEX idx_verdicts_candidate ON verdicts(candidate_id);
CREATE INDEX idx_jobs_claim ON jobs(status, run_id, phase, id);

-- PARTIAL, and that is the point: a non-cacheable row cannot be returned by a
-- cache lookup because it is not in the index at all (12.5). `WHERE cacheable`
-- rather than `WHERE cacheable = 1` — the column is a real boolean here.
CREATE INDEX idx_cache ON candidates(
  file_sha256, position_id, rubric_hash, judge_digest, verifier_digest,
  prompt_hash, redaction_on, num_ctx, app_version
) WHERE cacheable;

-- A reference is unique among OPEN requisitions only. Closing one frees it.
CREATE UNIQUE INDEX idx_positions_open_reference ON positions(reference)
  WHERE status = 'open';

-- --- append-only audit log ---------------------------------------------------
--
-- The same guarantee as the SQLite triggers, expressed the way Postgres
-- requires: a trigger body is a function here, and the abort is RAISE EXCEPTION.
-- This is a control, not hygiene — it is what stops the record of who decided
-- what from being edited afterwards, including by this application's own code.

CREATE FUNCTION audit_log_append_only() RETURNS trigger AS $$
BEGIN
  RAISE EXCEPTION 'audit_log is append-only';
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();

CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();
