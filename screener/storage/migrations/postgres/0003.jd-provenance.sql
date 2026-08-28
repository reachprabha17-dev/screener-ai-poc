-- Where a requisition's job description came from (spec 8, 9.1).
--
-- `jd_text` used to have exactly one origin: a person typing into a textarea.
-- It can now also be the output of the sandboxed parser reading an uploaded PDF
-- or DOCX, and those two are not equally trustworthy. Extraction from a
-- two-column layout interleaves; OCR on a scan approximates. The rubric drafted
-- from that text is what every candidate in the run is measured against, so
-- "which document produced this, and how was it read" is audit data about an
-- adverse decision, not interface decoration.
--
-- `jd_source` is NOT NULL DEFAULT 'paste'. Every row that exists when this runs
-- was pasted — that is the only way a position could have been created — so the
-- default is a true statement about history rather than a backfilled guess.
--
-- The other three are nullable because they are meaningless for a pasted
-- description: there was no file, so there is no name and no hash. NULL here
-- reads as "not applicable", never as "unknown".
--
-- `jd_file_sha256` identifies the *uploaded file*, not the stored `jd_text`.
-- The reviewer sees the extracted text and may correct it before submitting, by
-- design — so the hash establishes which document was uploaded, and does not
-- claim the text is a faithful function of it.
ALTER TABLE positions
  ADD COLUMN jd_source TEXT NOT NULL DEFAULT 'paste'
    CHECK (jd_source IN ('paste', 'upload')),
  ADD COLUMN jd_filename TEXT,
  ADD COLUMN jd_file_sha256 TEXT,
  ADD COLUMN jd_ocr_used BOOLEAN;
