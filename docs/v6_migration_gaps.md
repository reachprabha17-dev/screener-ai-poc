# Gaps in `v4_to_v6_migration.md`

Checked the migration guide against `screener_spec_v6.md` **and** against the v4 code actually in
this tree. It is a good guide for the *new* work — offsets, negation, the verifier, the two-phase
worker are all specified well enough to build from. Where it falls down is the *existing* code: it
describes a v4 that is not the v4 in this repository, and it leaves out roughly a third of the spec
surface that has to change.

Five findings will stop the migration mid-phase. The rest are silent — they leave a control looking
implemented while it is not, which is the failure mode this spec cares most about.

---

## 1. Blockers — the guide as written does not apply cleanly

### 1.1 `model_digest` is never renamed to `judge_digest` — Phase B fails

`0001.initial-schema.sql:124` has `candidates.model_digest`; `:62–63` has `runs.model_name` and
`runs.model_digest`. B1's new index is:

```sql
CREATE INDEX idx_cache ON candidates(
  file_sha256, position_id, rubric_hash, judge_digest, verifier_digest, ...)
```

`judge_digest` does not exist. The migration aborts at that statement. B1 adds `verifier_digest`
everywhere but never renames the v4 column, and the rename is not confined to SQL:

| Location | v4 today |
|---|---|
| `ports.py:37` | `CacheKey.model_digest` |
| `models.py:315` | `Run.model_digest` |
| `schemas.py:152` | `RunResponse.model_digest` |
| `api/deps.py:107` | `to_run` |
| `service.py:597` | `cache_key_for` |
| `storage/results_store.py`, `runs_store.py` | INSERT/SELECT column lists |
| `screener/cli.py`, `ui/` | displayed in status output |

SQLite supports `ALTER TABLE ... RENAME COLUMN`, so the DDL is cheap. The point is that the guide
does not say to do it, and the failure surfaces as a broken migration rather than a type error.

### 1.2 Migrations in `migrations/sqlite/` are silently never applied

`storage/connection.py:29` is `MIGRATIONS_DIR = <storage>/migrations`, read by
`yoyo.read_migrations(str(MIGRATIONS_DIR))`. yoyo globs that directory; it does not recurse. A file
at `migrations/sqlite/0002_v6.sql` is not seen — so `pending_migrations()` reports **current**, the
API and worker start happily, and every phase-B write fails against a v4 schema.

The guide also switches naming conventions without saying so: v4 is `0001.initial-schema.sql` plus
`0001.initial-schema.rollback.sql`; the guide writes `0002_v6.sql` with no rollback file.

Needed and unstated: move `0001.*` into `migrations/sqlite/`, point `MIGRATIONS_DIR` at the
per-dialect directory selected by `db_backend`, keep yoyo ids stable so the applied-ledger still
matches, write the rollback, and update `cli.py migrate`, the `migrations_current` health gate,
`scripts/dev.sh` and spec §20's `yoyo apply` path.

### 1.3 `runs.status` has no `'empty'` — E2 violates a CHECK constraint

`0001.initial-schema.sql:72–73`:

```sql
status TEXT NOT NULL DEFAULT 'pending'
  CHECK (status IN ('pending','running','completed','failed','aborted'))
```

E2's `create_run` sets `status='empty'` on an empty folder. That raises `IntegrityError` at runtime,
not at migration time — so it lands as a bug in the first empty-folder run. SQLite cannot alter a
CHECK, so this needs the same create/copy/drop/rename rebuild the guide prescribes for the `jobs`
UNIQUE — and B1 only calls out `jobs`. The `runs.phase` CHECK B1 adds via `ALTER` has the same
problem in reverse: `ALTER TABLE ADD COLUMN ... CHECK` is accepted by SQLite but the guide's own note
says enums are enforced in Pydantic instead, which is inconsistent with it writing the CHECK inline.

Also missing: `Run.status` in `models.py:314` is a `Literal` without `"empty"`, and gains no `phase`
field at all.

### 1.4 `redact_pii`'s "BEFORE" signature is wrong

A3 claims v4 is `def redact_pii(text: str) -> str`. It is not — `core/redact_pii.py:89` is already
`-> tuple[str, RedactionReport]`, and `RedactionReport` carries per-category counts used downstream.
The guide's "AFTER" returns `tuple[str, list[Span]]`, which **deletes the report**.

Worse, the single caller is `pipeline.py:206`:

```python
sent = redact_pii(text)[0] if settings.redact_pii else text
```

That keeps type-checking and keeps working after the change, silently discarding the span map. Every
highlight then lands wrong — the exact failure A1 exists to prevent — with no error anywhere. The
guide's "every caller must be updated" is not enforceable here because nothing breaks.

Fix: return `tuple[str, RedactionReport, list[Span]]`, or hang the span map on `RedactionReport`.
Either way, change the call site's shape so the compiler catches it.

### 1.5 The proposed `worker.py` fails `tests/test_layering.py`

C2 rewrites `worker.py` with `jobs.claim_next(...)`, `results.save(tx, ...)`,
`results.save_verification(...)`, `runs.set_phase(...)` and `with uow() as tx`. `test_layering.py:239`:

```python
def test_the_worker_owns_no_sql() -> None:
    assert_forbidden(
        package_imports("screener/worker_loop.py", "worker.py"),
        ("sqlite3", "screener.storage.results_store", "screener.storage.jobs_store"),
    )
```

Spec §2 agrees: the worker uses `service.py` for reads/writes and `pipeline.py` for execution.

Two further mismatches: the loop is not in `worker.py` (18 lines, an entrypoint) but in
`screener/worker_loop.py` as a `Worker` class; and the phase work has to land as new methods on
`service.py`'s worker-facing surface (`service.py:512–642`) — `claim_next_job(worker_id, phase)`,
a verification-write path, `finish_run_if_complete` becoming `advance_phase_if_complete`. The guide
never mentions `service.py` changing for the worker at all.

---

## 2. Spec requirements with no migration step

### 2.1 `prompt_hash` does not cover the verifier prompts

`llm/judge_resume.py:119` and `llm/extract_rubric.py:85` hash their own prompt files into
`prompt_hash`, which is a cache-key field (`ports.py:38`). v6 adds `verify_support.md` and
`confirm_absence.md`, and verification results are now stored on the candidate — so editing a
verifier prompt must invalidate the cache and does not. The guide adds `verifier_digest` to the key
and stops there.

### 2.2 `rubric_hash` does not cover `claim`

`models.py:161` hashes `[c.id, c.text, c.weight, c.must_have]`. §10.6 feeds `claim` straight to the
verifier, so regenerating a claim without touching the criterion text changes verification output
while leaving `rubric_hash` — and therefore the whole cache key — unchanged. Decide explicitly:
include `claim` in `content_hash` (and exclude `claim_stale`, which is workflow state, not content).

### 2.3 gemma4:12b's thinking budget — the most likely day-one phase-2 failure

`config/settings.py:53–61` records a measurement made on this box:

> Measured on `gemma4:12b`: 1536 tokens consumed entirely by `thinking`, empty `content`, and the
> call surfaced as SCHEMA_INVALID; at 4096 tokens it still had not finished after 82 s.

That is the model v6 makes the verifier. The guide sets `verifier_num_ctx = 16384` and says nothing
about `disable_thinking`, a `verifier_num_predict`, or the fact that a thinking model spends
§10.1's reserved output budget on reasoning. The v4 client already has `_thinking()` handling
(`clients/ollama_client.py:211`) — the migration needs to state that it applies to the verifier path
and that phase 2's budget arithmetic accounts for it.

### 2.4 Measured constants would be reverted by anyone following the spec

Two settings were deliberately moved off the spec's values, with the measurements recorded in
comments:

| Setting | v4 (measured) | v6 spec §7 | Consequence of "fixing" it |
|---|---|---|---|
| `evidence_match_min_chars` | 16 (`settings.py:90–112`, sweep over 25 real quotes) | 25 | 3 verbatim-correct quotes escalate purely for brevity |
| `parse_mem_limit_mb` | 2048 (`settings.py:75–83`) | 1024 | OCR aborts on any document past 3 pages |

A7 diffs settings against the spec and mentions neither. Either the guide says "keep the measured
values, the spec is stale", or someone reconciles them and loses the tuning. Add
`seconds_per_resume: 5.4` to the same list — §17.3 wants ~9.4 s across two phases for the ETA.

### 2.5 The heartbeat surface is neither kept nor deleted

B3 says "delete `reclaim_expired()` (lease-based)". There is no such function. What exists is:
`jobs.heartbeat_at` / `heartbeat_seq` columns, `jobs_store.heartbeat` / `stalled_candidates` /
`reclaim_if_unchanged` (`:198`, `:290`, `:304`), `service.heartbeat` (`:542`), the worker's calls,
and `settings.heartbeat_interval_s`. Spec §17.5 keeps the heartbeat sequence as the deferred
multi-worker mechanism, so "keep it" is defensible — but the guide deletes something that isn't
there and leaves the thing that is.

### 2.6 Trace removal is roughly ten times larger than F5 says

F5 lists four items. The actual surface:

- `storage/traces_store.py`, the `traces` table, `idx_traces_sha`
- `logging.py:110` `TraceWriter`, `logging.py:164` `prune_traces`, `DEFAULT_TRACE_RETENTION_DAYS`
- `pipeline.py:53` `TraceRecord`, `:69` `TraceSink`, `Deps.trace`, and the trace emission at `:234`
- `worker_loop.py:41,61,169–189` — `_trace_sink`, the write-and-index-or-neither invariant
- `service.py:567` `record_trace`, `:691` `trace_count`
- `results_store.py:155` `purge_candidate` — its return type *is* `list[Path]` of trace files, and
  `service.py:469` unlinks them outside the transaction. Removing traces guts that contract; it has
  to be rebuilt to return failure-dir paths and the source file (see 2.7).
- `settings.trace_dir`, `trace_enabled`; the disk-headroom comment at `worker_loop.py:108`
- `tests/test_logging_traces.py`, plus CLI/health output that reports trace counts

And the part with real-world consequence: **`data/traces/` currently holds 445 runs of full resume
text.** Dropping the `traces` table without disposing of those files leaves PII on disk with the
index deleted — precisely the failure `traces_store.py:7` documents ("erasure looking implemented
while a second copy survives"). The migration needs a disposal step, ideally before B1 drops the
table.

### 2.7 `purge_candidate` still never deletes the source resume

§12.10 requires the source file. v4 (`results_store.py:172–182`) redacts `filename`, `summary`,
`notable_strengths_json`, nulls `verdicts.evidence`, sets `cacheable = 0`, and unlinks trace files —
the original PDF under `data/resumes/<ref>/` survives. B2 adds `resume_text`, `sent_text`,
`redaction_map_json`, `absence_evidence` and `failure_dir` files, but not `sent_text_sha256` and
not the source file. Six text locations is the guide's own phase-B gate; the real count is eight.

### 2.8 `EVIDENCE_IRRELEVANT` — kept or dropped?

v4 has `Flag.EVIDENCE_IRRELEVANT` (`models.py:62`) backed by
`verify_evidence.evidence_mentions_criterion` (`:163`), downgraded to flag-only after it removed the
two strongest candidates from a live run (`CODEBASE_GUIDE.md:160`). v6 §5's `Flag` enum does not
contain it. A5 says "keep all v4 members", which contradicts the spec.

Meanwhile C1's pipeline calls `verify_evidence(out, sent)` — v4's signature is
`verify_evidence(output, sent_text, rubric)` (`:232`), and the rubric argument exists *only* for the
relevance check. So the guide deletes the check by dropping an argument, while its models section
says to keep the flag. Stage D plausibly supersedes it; that is a decision worth writing down, not
one to make by accident. If it goes, `EscalationReason` needs no new member but the flag's rows in
`flags_json` on existing candidates become unmapped enum values on read.

### 2.9 Two-phase caching is undefined

`get_cached` gains `verifier_digest`, and phase-2 claiming skips `verification_status='done'`. But a
phase-1 cache hit copies a *previous run's* candidate row, including its verification columns and
`verification_status`. Does the copied candidate get a phase-2 job? `enqueue_verify_jobs` is
specified as "only for scoreable candidates" with no mention of already-verified ones. Both answers
are defensible; neither is written down, and the wrong one either re-verifies a whole cached run or
serves stale verification under a new verifier digest.

### 2.10 `ports.Job`, `RunStatus` and `RankedResult` stop short

- `ports.Job` (`:45`) gains neither `phase` nor `candidate_id`, though B1 adds both columns and C2's
  loop reads `job.candidate_id`.
- §15.4's run header needs phase, per-phase progress (`judging 340/1000` vs `verifying 120/1000`),
  escalation breakdown and undecided count. That means `RunStatus`, `RunStatusResponse`,
  `to_run_status`, `service.run_status` (`:335`) and `jobs_store.progress`/`eta_seconds` (`:321`,
  `:66` — all single-phase today). The guide mentions only the UI rendering.
- A5 adds `escalation_breakdown` and `undecided_count` to `RankedResult` but nothing updates
  `core/rank.py`, which computes the partitions, or `RankedResponse`.
- §17.6's rule that `verification_status='pending'` candidates **count as `review_required`** at
  sign-off is absent from E2's precondition, which counts only `review_required AND undecided`.

### 2.11 `save_rubric(base_version)` never reaches the wire

E2 changes the service signature. Unstated: `SaveRubricRequest` (`schemas.py:36`) gains
`base_version`, `PUT /positions/{id}/rubric` (`routes/positions.py:56`) passes it, `ui/api_client.py`
sends it, `cli.py edit_rubric` (`:176`) supplies it, and `ConflictError` maps to HTTP 409. Same for
`approve_rubric` raising on `claim_stale` — that needs a 4xx mapping, not a 500.

### 2.12 `GET /candidates/{id}/file` has nothing to serve from

E3 adds the route. `candidates` stores `filename` only; the path lives on `jobs.file_path`. Serving
the original needs a resolved path, a traversal guard (the filename is attacker-influenced), a
content-type decision, and defined behaviour after purge deletes the file. None of it is specified.

### 2.13 Decision vs. override — an API break with no statement

v4 has `POST /candidates/{id}/override` (`routes/candidates.py:22`), `service.record_override`
(`:430`), `OverrideRequest`, `cli override` (`:347`), and UI + `tests/test_api.py` coverage. E2/E3
introduce `record_decision` and `POST /candidates/{id}/decision` without saying whether override is
removed, aliased, or kept alongside. Everything above breaks on removal and nothing in the guide
flags it.

### 2.14 Eval work is only in the gate table

`eval/` already has `run_eval.py`, `accuracy.py`, `escalation.py`, `corpus.py`,
`labelled_set.jsonl`. Missing from the guide:

- §10.9's reproducibility gates went from one to **four** (phase-1 ≥98%, phase-2 ≥95%, band 100%,
  `review_required` ≥97%). `eval/run_eval.py` needs all four; the guide never mentions it.
- `eval/escalation.py` must print the **breakdown by reason** — C4's nag and §19.2's budget decision
  both depend on it.
- §19.5's verifier-targeted injections belong in the versioned `eval/corpus.py`, not only as a
  phase-D checkbox.

---

## 3. Where the guide misreads v4

### 3.1 `schemas.py` is not new, and the stated rationale is backwards

E1 says: *"v4 returned domain models directly, which leaks audit fields to recruiters."* v4 has
`screener/schemas.py` — 188 lines of request and response models — whose docstring opens by
explaining that `model_verdict` is deliberately excluded, and closes with *"the alternative —
returning domain models directly and remembering to strip fields — fails silently."* The guide's
premise is the opposite of the code.

What actually needs doing, and isn't stated:

- `CriterionResponse` **does** expose `match_ratio` and `longest_span` to every caller
  (`schemas.py:78–79`), with a comment justifying it. §15.2 makes both auditor-only. This is a
  *removal* from the existing recruiter view plus a new audit subclass — not a greenfield addition.
- No mechanism is given for selecting a view by role. `to_candidate` (`api/deps.py:124`) takes no
  actor, and `get_actor` currently returns `{"admin", "auditor"}` for everyone. Role-scoped exposure
  is a phase-E gate; the seam it needs does not exist yet.
- The fate of `CandidateResponse` / `RankedResponse` / `CriterionResponse` is unspecified — rename,
  keep both, or subclass.

### 3.2 File layout conflicts with spec §4

The guide creates `screener/llm/verify.py`. Spec §4 and build step 11 name
`llm/verify_support.py` and `llm/confirm_absence.py`. Similarly `storage/trace_store.py` (guide) vs
`storage/traces_store.py` (actual).

### 3.3 Phase F is sequenced after the SQL it is supposed to own

F1 introduces `storage/dialect/` as "the only backend-specific code", but B1–C2 write raw SQLite SQL
directly into the stores across three phases first. Either `dialect/` lands with B (so new SQL is
written through it once), or F includes an explicit re-homing step for everything B and C added.

### 3.4 The change estimate is low

"Roughly 12 new files, 15 modified, 2 deleted." Counting only what the spec requires and the guide
already implies — plus the items above — modified lands closer to 35: every store, `ports.py`,
`service.py` (worker surface + decisions + preconditions), `worker_loop.py`, `pipeline.py`,
`schemas.py`, `api/deps.py`, all four route modules, `cli.py`, `ui/screener_app.py`,
`ui/api_client.py`, `logging.py`, `connection.py`, both `llm/` modules, both prompts, `settings.py`,
`models.py`, and ~15 test modules.

---

## 4. Adjacent work the guide doesn't scope

- **`docs/CODEBASE_GUIDE.md`** is a v4 artefact throughout: it routes readers to
  `screener_spec_v4.md` (deleted in this working tree), teaches `screen_one` as "the spine", tells
  people to read a trace under `data/traces/`, and cites "448 tests". It needs a pass at phase F or
  it becomes actively misleading.
- **`pyproject.toml`** — spec §3 requires `testcontainers` (F3), `--cov-fail-under=85`, and the
  `mssql = ["pyodbc"]` extra. Not mentioned.
- **`deploy/*.service`, `scripts/dev.sh`, `.env.example`** — two models to pull, `OLLAMA_NUM_PARALLEL=1`,
  the new `failure_dir` at `0700`, the removed trace dir.
- **Data disposal** — `data/traces/` (see 2.6) and `data/screener.db` carrying pre-migration rows
  whose `flags_json` may contain `EVIDENCE_IRRELEVANT` (see 2.8).

---

## Suggested amendments, in order

1. Add a **Phase A0** to the guide: rename `model_digest` → `judge_digest` across SQL, `CacheKey`,
   `Run`, responses and stores; move migrations to `migrations/sqlite/` and repoint
   `MIGRATIONS_DIR`; dispose of `data/traces/`. Nothing else can land cleanly first.
2. Rewrite B1 to include the two table rebuilds (`runs` for `status='empty'`, `jobs` for the UNIQUE)
   and a rollback file.
3. Rewrite C2 against `screener/worker_loop.py` and `service.py`, not a fictional `worker.py`.
4. Fix A3's before/after signature and make the `pipeline.py:206` call site shape-breaking.
5. Add explicit decisions for: `EVIDENCE_IRRELEVANT`, verifier prompts in `prompt_hash`, `claim` in
   `rubric_hash`, cache-hit-vs-phase-2, `disable_thinking` on the verifier, and the three measured
   constants that differ from the spec.
6. Rewrite E1 to describe modifying `schemas.py` — including removing `match_ratio`/`longest_span`
   from the recruiter view and adding an actor-aware mapper.
