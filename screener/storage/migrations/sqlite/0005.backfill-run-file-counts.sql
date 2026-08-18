-- Backfill `runs.file_count` for every run created before `create_run` and
-- `rescan_run` learned to write it back (screener/service.py).
--
-- `runs_store.create` always defaulted `file_count` to 0, and nothing ever
-- called back with the real number once the folder was actually snapshotted
-- into `jobs` — a second call, after the run row already exists, since
-- `jobs.run_id` is a foreign key into `runs`. Every run in this database reads
-- `file_count = 0` regardless of what it screened, which is what made the
-- Files column on the runs list look empty for everything.
--
-- Data-only, no schema change, unconditional and idempotent: recomputed for
-- every row from the same source the live code now uses (16.2), not only the
-- ones currently at 0, so re-running this is harmless.
UPDATE runs SET file_count = (
  SELECT COUNT(*) FROM jobs WHERE jobs.run_id = runs.id AND jobs.phase = 'judge'
);
