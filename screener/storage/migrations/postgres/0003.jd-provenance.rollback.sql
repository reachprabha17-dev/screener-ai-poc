-- Rollback for 0003. Drops the record of which document each rubric was drafted
-- from; `jd_text` itself is unaffected, so nothing a run was scored against is
-- lost — only the ability to say where the text came from.
ALTER TABLE positions
  DROP COLUMN IF EXISTS jd_source,
  DROP COLUMN IF EXISTS jd_filename,
  DROP COLUMN IF EXISTS jd_file_sha256,
  DROP COLUMN IF EXISTS jd_ocr_used;
