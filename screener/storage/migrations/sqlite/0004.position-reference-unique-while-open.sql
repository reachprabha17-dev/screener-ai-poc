-- `positions.reference` is unique among **open** positions only.
--
-- The blanket `UNIQUE` from the initial schema meant a closed requisition
-- permanently squatted its folder: nobody could raise a new requisition
-- against the same resume folder ever again, even years later, because the
-- constraint did not know the old one was closed. What it actually needs to
-- prevent — two *simultaneously open* requisitions pointing at the same
-- folder, which would screen the same résumés against two different rubrics
-- with no way to tell which run a given file belongs to — is what the partial
-- index below enforces instead.
--
-- Table rebuild because SQLite cannot drop or narrow a column-level UNIQUE.
-- Create-copy-drop-rename, not rename-create-copy-drop: renaming `positions`
-- out of the way would have SQLite rewrite `rubrics.position_id` and
-- `runs.position_id` to point at the temporary name (0003).

CREATE TABLE positions_v8 (
  id TEXT PRIMARY KEY,
  reference TEXT NOT NULL,             -- folder name under data/resumes/
  title TEXT NOT NULL,
  jd_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  created_by TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  closed_at TEXT
);

INSERT INTO positions_v8 (
  id, reference, title, jd_text, status, created_by, created_at, closed_at)
SELECT
  id, reference, title, jd_text, status, created_by, created_at, closed_at
FROM positions;

DROP TABLE positions;
ALTER TABLE positions_v8 RENAME TO positions;

CREATE UNIQUE INDEX idx_positions_open_reference ON positions(reference)
  WHERE status = 'open';
