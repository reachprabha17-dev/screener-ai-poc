-- Rename the single-model columns to name the judge explicitly (v6 1.3, 12.7).
--
-- v6 runs two models: `granite4.1:8b` judges and `gemma4:12b` verifies. A column
-- called `model_digest` stops meaning anything the moment a second model
-- contributes to a stored result, and the v6 cache key names both digests
-- (`judge_digest`, `verifier_digest`) so that changing either invalidates it.
--
-- This lands before the verifier columns rather than with them because the v6
-- cache index references `judge_digest`: created against a schema that still
-- says `model_digest`, that statement fails and takes the whole migration with
-- it. Renaming first keeps every later migration a pure addition.
--
-- SQLite rewrites index, trigger and view definitions that reference a renamed
-- column (3.25+, verified against 3.45), so `idx_cache` follows automatically
-- and is deliberately not dropped and rebuilt here. `PRAGMA legacy_alter_table`
-- must stay off — with it on, the rename does not propagate and the index is
-- left pointing at a column that no longer exists.

ALTER TABLE candidates RENAME COLUMN model_digest TO judge_digest;

ALTER TABLE runs RENAME COLUMN model_name TO judge_model;
ALTER TABLE runs RENAME COLUMN model_digest TO judge_digest;
