-- Rollback for 0002. Lossless: a rename in both directions, no data touched.

ALTER TABLE runs RENAME COLUMN judge_digest TO model_digest;
ALTER TABLE runs RENAME COLUMN judge_model TO model_name;

ALTER TABLE candidates RENAME COLUMN judge_digest TO model_digest;
