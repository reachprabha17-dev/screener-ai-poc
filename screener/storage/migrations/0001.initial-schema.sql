-- Initial schema (spec §12.4).
--
-- Three things in here are load-bearing and easy to mistake for boilerplate:
--
--   * The audit_log triggers make append-only a property of the database rather
--     than a convention the application is trusted to follow. A convention
--     survives exactly until someone writes a cleanup script.
--
--   * idx_cache is a PARTIAL index (WHERE cacheable = 1). Transient failures are
--     stored for audit and shown to reviewers, but are structurally invisible to
--     cache lookup — enforced by the index rather than by a filter in Python
--     that a future query could forget (§12.5).
--
--   * The deferred-compliance columns on candidates (source, consent_ref,
--     retention_expires_at, objection_status) are nullable and unused today.
--     They land now because backfilling context you no longer have is
--     impossible, and adding the column later is the cheap part (§21).
--
-- All timestamps are TEXT holding UTC ISO-8601 with offset (§25). SQLite has no
-- datetime type; storing epoch integers would make the database unreadable
-- without the application, and this one is candidate data an auditor may need to
-- read directly.

CREATE TABLE users (
  id TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  roles_json TEXT NOT NULL DEFAULT '["admin"]',
  auth_ref TEXT,                       -- LDAP DN or argon2 hash, later (§21)
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);

CREATE TABLE positions (
  id TEXT PRIMARY KEY,
  reference TEXT NOT NULL UNIQUE,      -- folder name under data/resumes/
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
  model_name TEXT NOT NULL,
  model_digest TEXT NOT NULL,
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

CREATE TABLE jobs (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  file_path TEXT NOT NULL,
  file_sha256 TEXT,                    -- computed at claim time, not scan time (§16.2)
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
CREATE INDEX idx_jobs_claim ON jobs(status, run_id, id);

CREATE TABLE candidates (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL REFERENCES runs(id),
  filename TEXT NOT NULL,              -- NOTE: usually contains the person's name
  file_sha256 TEXT NOT NULL,
  score REAL,                          -- NULL, never 0.0, when not scoreable (§5)
  band TEXT,
  must_haves_met INTEGER,
  scoreable INTEGER NOT NULL DEFAULT 1,
  review_required INTEGER NOT NULL DEFAULT 0,
  cacheable INTEGER NOT NULL DEFAULT 1,
  summary TEXT,
  notable_strengths_json TEXT,
  red_flags_json TEXT,
  flags_json TEXT,
  -- deferred-compliance columns: nullable now, un-backfillable later (§21)
  source TEXT,
  consent_ref TEXT,
  retention_expires_at TEXT,
  objection_status TEXT,
  -- cache columns, denormalized from runs so a lookup is one index probe
  position_id TEXT NOT NULL,
  rubric_hash TEXT NOT NULL,
  model_digest TEXT NOT NULL,
  prompt_hash TEXT NOT NULL,
  redaction_on INTEGER NOT NULL,
  num_ctx INTEGER NOT NULL,
  app_version TEXT NOT NULL,
  scored_at TEXT NOT NULL,
  UNIQUE(file_sha256, run_id)
);

CREATE TABLE verdicts (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  criterion_id TEXT NOT NULL,
  verdict TEXT NOT NULL,
  model_verdict TEXT NOT NULL,         -- what the model said, retained for audit
  evidence TEXT,
  verified INTEGER NOT NULL,
  match_ratio REAL,                    -- persisted so thresholds tune against data (§18)
  longest_span INTEGER
);
CREATE INDEX idx_verdicts_candidate ON verdicts(candidate_id);

CREATE TABLE overrides (
  id INTEGER PRIMARY KEY,
  candidate_id INTEGER NOT NULL REFERENCES candidates(id),
  actor_id TEXT NOT NULL REFERENCES users(id),
  old_score REAL,
  old_band TEXT,
  new_decision TEXT NOT NULL CHECK (new_decision IN ('advance','reject','hold')),
  reason TEXT NOT NULL,
  created_at TEXT NOT NULL
);

-- Contains full resume text. This is a second store of candidate data and sits
-- inside the erasure path — see §17 and purge_candidate (§12.6).
CREATE TABLE traces (
  id INTEGER PRIMARY KEY,
  run_id TEXT NOT NULL,
  file_sha256 TEXT NOT NULL,
  trace_path TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX idx_traces_sha ON traces(file_sha256);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  actor_id TEXT,
  action TEXT NOT NULL,
  entity TEXT,
  entity_id TEXT,
  detail_json TEXT
);

-- Append-only enforced by the database, not by convention.
CREATE TRIGGER audit_log_no_update BEFORE UPDATE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;
CREATE TRIGGER audit_log_no_delete BEFORE DELETE ON audit_log
  BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END;

-- Partial: non-cacheable rows are invisible to cache lookup by construction.
CREATE INDEX idx_cache ON candidates(
  file_sha256, position_id, rubric_hash, model_digest, prompt_hash,
  redaction_on, num_ctx, app_version
) WHERE cacheable = 1;

CREATE INDEX idx_cand_run ON candidates(run_id);
