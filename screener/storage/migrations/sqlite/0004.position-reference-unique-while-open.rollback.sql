-- Rollback for 0004.
--
-- **Lossy by construction, and can fail outright.** If two closed positions now
-- share a reference — the entire point of the forward migration — rebuilding
-- the blanket `UNIQUE` fails with a constraint violation. There is no
-- automatic resolution: renaming one of the references would change what a
-- reviewer sees as the requisition's folder, on a row that was never a
-- candidate for renaming. Reconcile duplicates by hand before rolling back.

DROP INDEX IF EXISTS idx_positions_open_reference;

CREATE TABLE positions_v7 (
  id TEXT PRIMARY KEY,
  reference TEXT NOT NULL UNIQUE,      -- folder name under data/resumes/
  title TEXT NOT NULL,
  jd_text TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
  created_by TEXT NOT NULL REFERENCES users(id),
  created_at TEXT NOT NULL,
  closed_at TEXT
);

INSERT INTO positions_v7 (
  id, reference, title, jd_text, status, created_by, created_at, closed_at)
SELECT
  id, reference, title, jd_text, status, created_by, created_at, closed_at
FROM positions;

DROP TABLE positions;
ALTER TABLE positions_v7 RENAME TO positions;
