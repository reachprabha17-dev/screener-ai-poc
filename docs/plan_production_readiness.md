# Production readiness review: pipeline, scaling, secrets, ingestion failures

Reviewed against the implementation, not the spec. Findings are ordered by severity,
then a plan. Line references are to the current working tree.

---

## Summary

| Question | Answer |
|---|---|
| Scalable job pipeline? | **Yes for vertical scale.** The queue design is sound — atomic claim, attempt caps, resumable. Bounded by one SQLite writer and one GPU. |
| Message broker / task queue needed? | **No.** Adding Celery/RabbitMQ now would be a downgrade — the DB-backed queue already gives durability, resumption and audit in one transaction. Revisit only at multi-host. |
| Cluster-safe if horizontally scaled? | **No.** Three specific defects, all fixable. Details in §2. |
| Secrets in prod? | **No secrets exist today** (fully local, no external calls). Becomes real only with LDAP auth or the MSSQL backend — neither implemented. |
| Ingestion failure handling? | **Good on retry/caps, one serious hole:** permanently-failed files vanish from the run and do not block sign-off. §4. |

**The one finding that is not a scaling concern and should be fixed regardless of
deployment shape is §4.1** — it silently breaks the oversight guarantee the system
is built around.

---

## 1. Job pipeline — sound, and the broker question

The queue is a database table with an atomic claim:

```sql
UPDATE jobs SET status='claimed', claimed_by=?, attempts=attempts+1
WHERE id = (SELECT j.id FROM jobs j JOIN runs r ON r.id=j.run_id
            WHERE j.status='pending' AND j.phase=? AND r.status IN (...)
            ORDER BY r.created_at, j.id LIMIT 1)
RETURNING ...
```
`jobs_store.claim_next:177`. One statement, so two workers cannot claim the same
row — SQLite serialises writers. WAL + `busy_timeout=5000` + `BEGIN IMMEDIATE`
(`connection.py:58-60`, `uow.py:65`) are all correctly set, and `BEGIN IMMEDIATE`
is the right call: it takes the write lock at the start where `busy_timeout`
handles contention, rather than mid-transaction where SQLite returns `SQLITE_BUSY`.

**Do not add a message broker.** Celery/RabbitMQ/Redis would *lose* properties here:

- `complete_job` writes the candidate and closes the job **in one transaction**
  (`service.py:772-782`). With a broker, ack and DB write are two systems — the
  crash window between them gives you either a lost candidate or a duplicate.
- Resumption is already free: state lives in `jobs.status`, so a killed worker
  resumes by reading the table. A broker needs its own durability story.
- Every state change is audited in the same transaction as its effect.

The current design is the *correct* one for this workload (a few hundred to a few
thousand documents, GPU-bound at ~5 s each). A broker solves fan-out across many
hosts, which is not the bottleneck — the GPU is.

**When a broker would genuinely be needed:** multiple physical GPU hosts. At that
point SQLite must go first (§2.1), and the queue would move to the same database
rather than to a broker — `SELECT ... FOR UPDATE SKIP LOCKED` on Postgres gives
the identical claim semantics and keeps the one-transaction guarantee.

---

## 2. Horizontal scaling — three defects

### 2.1 Two workers with the default config corrupt each other's work — **critical**

`config/settings.py:167`: `worker_id: str = "worker-1"` — a hardcoded default.

Startup reclaim resets **anything claimed by this worker_id** (`jobs_store.reclaim_orphaned:330`),
on the reasoning that "this process has claimed nothing yet, so there is no ambiguity."
That reasoning holds only if `worker_id` is unique per process. Run two workers
without setting `WORKER_ID` and worker B's startup reclaim resets worker A's
**in-flight** jobs to `pending` while A is mid-inference. Both then judge the same
file; the second to finish hits `UNIQUE(file_sha256, run_id)` and the job dies.

Fix: derive a unique default (`f"{socket.gethostname()}-{os.getpid()}"`) instead
of a constant. One line, and it makes the failure mode impossible rather than
documented.

### 2.2 A dead worker's jobs are stranded forever — **high**

`stalled_candidates` (`jobs_store.py:338`) and `reclaim_if_unchanged` (`:352`) are
implemented, correct, and **never called** — grep shows zero call sites outside the
module. The docstrings say "unused while there is one worker."

Consequence: if worker A dies and never returns under the same id — a rescheduled
container, a renamed host — its `claimed` jobs stay claimed forever. `no_pending`
counts `claimed` as outstanding (`:227`), so the run never advances phase and never
completes. **This is exactly the stall we hit earlier in this session**, where a run
sat in `verify` with two jobs that could not be claimed.

Fix: wire the existing primitives into the worker loop — sample `stalled_candidates`,
hold the observed `heartbeat_seq`, re-sample after a poll interval, and reclaim only
where the sequence has not moved. The design is already correct (relative progress,
never wall-clock), it just needs calling.

### 2.3 More workers on one GPU makes throughput worse — **design constraint**

`_ensure_model_for(phase)` (`worker_loop.py:180`) loads judge or verifier weights per
phase, and the comment at `:134-136` is explicit that 12 GB of VRAM holds one of the
two. Two workers on different phases will evict each other's model on every job —
a 10–20 s load per candidate instead of two loads per run.

This is not a bug; it is the reason horizontal scaling of workers **on one host** is
pointless. Scaling means more GPU hosts, and that requires §2.1, §2.2, and a database
that accepts concurrent writers from multiple machines.

### 2.4 API and UI scale fine

Both are stateless over HTTP. The API's `_shared_service` is `lru_cache`'d per process
with thread-local connections (`deps.py:79-87`, `connection.py:4`) — correct. The UI
holds only `st.session_state`. Neither is the constraint; **SQLite is**, and the
`db_backend: Literal["sqlite","mssql"]` setting (`settings.py:188`) is aspirational —
grep finds no MSSQL implementation and `migrations/` contains only `sqlite/`.

---

## 3. Secrets — nothing to manage yet, two future triggers

There are **no credentials in the codebase**. Deliberate, and a real advantage:

- Decision #1 is fully on-prem; Ollama is on loopback with no API key.
- `auth_mode` defaults to `"stub"` and trusts an `X-Actor` header (`deps.py:64-71`) —
  acceptable *only* because the API binds to `127.0.0.1` and `deps.py:9-11` says so.
- `.env` is gitignored and currently holds one non-secret path.

**Two things turn this into a real secrets problem, and both are already stubbed for:**

1. `auth_mode="ldap"` — a bind DN and password.
2. `db_backend="mssql"` — a connection string with credentials.

Recommendation: do nothing now. When either lands, read from environment injected by
systemd `EnvironmentFile=` with `0600` root-owned permissions (the units already use
`EnvironmentFile`), not from `.env` in the repo tree. A vault is not warranted for a
single on-prem host; it becomes warranted at multi-host, alongside §2.

**One thing to fix now regardless:** `auth_mode` must be moved off `stub` before the
API listens on anything but loopback. Consider making `create_app` refuse to start
if `auth_mode == "stub"` and the bind host is not loopback — the current protection
is a comment, and comments do not survive a deployment change.

---

## 4. Ingestion failure handling

**What is already right** — this is genuinely well built:

- Attempts are counted **at claim**, not at failure (`jobs_store.fail:277-280`), so a
  worker killed mid-job still burns an attempt. A poison file cannot retry forever.
- `job_max_attempts=3`, then `failed` with `last_error` retained.
- `release` decrements on clean shutdown (`:299`) so restarts do not exhaust the cap.
- Document-level problems (unreadable PDF, injection, budget) do **not** fail the job —
  `judge_one` returns a `Candidate` with `scoreable=False, review_required=True`, per
  `validate_file.py:10-14`: *"Rejection is not a skip… Nobody drops out of a run
  without a human seeing it."*

### 4.1 Infrastructure failures silently delete applicants — **critical, fix regardless of scale**

The guarantee above holds for *document* failures. It does **not** hold for
infrastructure failures. When `judge_one` raises (LLM down, DB error, bug), the
worker calls `fail_job` (`worker_loop.py:172`) and **no candidate row is created**.
After 3 attempts the job is `failed` and that applicant exists nowhere in the results.

Three compounding problems:

1. **`last_error` is stored but never exposed.** No API field, no UI. Grep confirms
   the only surfacing is a numeric `st.metric("Failed", …)` (`screener_app.py:495`).
   The reviewer sees "Failed: 3" — no filenames, no reasons.
2. **The run still reaches `completed`.** `finish_run_if_complete` (`service.py:931`)
   checks `progress.is_complete`, which counts `failed` as finished.
3. **Sign-off does not block.** `sign_off_run:515-520` inspects only *candidates* with
   `review_required` or `pending` verification. A failed job has no candidate, so it is
   invisible to the check.

**Net effect: a reviewer can sign off a run in which three applicants were never
screened, with nothing on screen naming them.** That is precisely the failure the
sign-off gate exists to prevent, arriving through a path the gate does not inspect.

### 4.2 Byte-identical duplicates crash their own job — **medium**

`candidates` has `UNIQUE(file_sha256, run_id)` and `results_store.save` is a plain
INSERT. Two identical files in one folder — routine when HR copies CVs onto a share —
make the second job raise `IntegrityError`, retry 3×, and retire as `failed`, landing
it in §4.1. `Flag.POSSIBLE_DUPLICATE` is declared (`models.py:74`) and set by nothing.

---

## Plan

Ordered by severity. Items 1–3 are independent of any scaling decision.

### 1. Surface and gate permanently-failed jobs *(§4.1 — highest value)*
- `jobs_store`: add `failed_jobs(tx, run_id) -> list[FailedJob]` returning
  `file_path`, `attempts`, `last_error`.
- `service.run_status`: include a `failed_files` list, not just a count.
- `service.sign_off_run`: add failed jobs to the `outstanding` check so sign-off is
  refused while any file was never screened. Same rationale as the existing gate,
  extended to the path it currently misses.
- UI: render the failed files with their reasons beside the escalation sections, so
  "Failed: 3" becomes three named files a person can act on.
- Tests: a run with a permanently-failed job must refuse sign-off and name the file.

### 2. Handle duplicates instead of crashing *(§4.2)*
- `results_store.save`: `INSERT … ON CONFLICT(file_sha256, run_id) DO NOTHING`, and
  set `Flag.POSSIBLE_DUPLICATE` on the surviving candidate.
- Removes a whole class of §4.1 failures at the source.

### 3. Make `worker_id` unique by default *(§2.1)*
- `settings.worker_id` default → `f"{socket.gethostname()}-{os.getpid()}"`.
- Prevents two workers from resetting each other's in-flight jobs. One line.
- Do this **before** anyone runs a second worker, not as part of a scaling project.

### 4. Refuse stub auth off loopback *(§3)*
- `create_app`: raise at startup if `auth_mode == "stub"` and the bind host is not
  loopback. Replaces a comment with a check.

### 5. Wire multi-worker reclaim *(§2.2 — only when scaling)*
- Call `stalled_candidates` / `reclaim_if_unchanged` from the worker loop using the
  two-sample heartbeat-sequence protocol the docstrings describe.
- Needed the moment a second worker exists; the primitives are already written.

### 6. Not now: broker, Postgres, vault
- **Broker:** would remove the single-transaction guarantee that makes the current
  pipeline correct. No.
- **Postgres/MSSQL:** required only for multiple GPU hosts. `SELECT … FOR UPDATE SKIP
  LOCKED` preserves the claim semantics; `db_backend` is already a seam, but nothing
  behind it is implemented — treat the enum as a to-do, not a capability.
- **Vault:** warranted alongside multi-host, not before. There are no secrets today.

### Verification
`bash scripts/dev.sh check` (ruff format, ruff, mypy, pytest — coverage gate 85%).
For items 1–2, the end-to-end check is: force a job to fail 3× (stop Ollama mid-run),
confirm the run does **not** reach sign-off and that the failed file is named on screen.
