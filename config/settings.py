"""Typed configuration, read from the environment and `.env` (spec 7).

Several values here are load-bearing rather than cosmetic:

``num_ctx`` — Ollama's default is 4096 and it truncates the prompt *silently* on
overflow. The model then judges a partially-read resume with no error raised,
which is indistinguishable downstream from a genuinely weak candidate.

``num_predict`` — left unset, output caps truncate the JSON mid-object and the
failure surfaces as a schema error whose real cause is invisible.

``judge_digest_pin`` — model tags are mutable. Re-pulling ``gemma4:12b`` can
change the weights underneath decisions already stored, breaking reproducibility
and the audit record.

``evidence_*`` — the three conditions in 10.5 are combined with AND. Any one of
them alone admits fabricated evidence.

``OLLAMA_NUM_PARALLEL=1`` belongs in the service environment, not here: slot
reuse is the largest remaining source of run-to-run variation (10.8).
"""

import socket
from pathlib import Path
from typing import Any, Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_VERSION_FILE = Path(__file__).resolve().parent.parent / "screener" / "_version.txt"


def _app_version() -> str:
    """Build-generated git description. Part of the cache key, never hand-edited."""
    try:
        return _VERSION_FILE.read_text(encoding="utf-8").strip() or "0.0.0-dev"
    except OSError:
        return "0.0.0-dev"


class Settings(BaseSettings):
    # --- Inference ---
    #
    # Judge and verifier currently share one model. The original design used
    # two deliberately different ones (1.3) — a verifier trained on the same
    # data as the judge shares its blind spots, and a second opinion that
    # agrees for the same reason as the first is not a second opinion — which
    # is also why runs were two-phase with a model swap between them (17.4).
    # Consolidated to one model for GPU/ops cost (no swap, no 12 GB VRAM
    # contention) now that `verify_scope="none"` retires the check that
    # benefited most from a genuinely independent second model
    # (`verify_support`). `confirm_absence` — the check that remains — asks
    # "did you literally miss this," not "would a different model see it
    # differently," so it loses less from sharing a model, but it is no longer
    # a fully independent second opinion either. Revisit if `verify_scope` is
    # ever turned back on: that check is the one this tradeoff actually costs.
    ollama_host: str = "http://localhost:11434"
    judge_model: str = "bvassie/gemma4:12b-it-qat-mtp"
    verifier_model: str = "bvassie/gemma4:12b-it-qat-mtp"
    judge_digest_pin: str | None = None
    verifier_digest_pin: str | None = None
    num_ctx: int = 8192
    num_predict: int = 1536
    # 10.6 B sends the whole resume plus every `none` criterion in one call, so
    # the verifier needs materially more room than the judge.
    verifier_num_ctx: int = 16384
    # confirm_absence (10.6 B) batches a confirmed-absent judgment and an
    # optional quote *per `none` criterion* into one call — more per-item
    # output than the judge's own verdict+evidence over the same set. Sharing
    # the judge's 1536-token budget truncates that response mid-JSON on a
    # candidate with more than a handful of `none` verdicts (observed in
    # practice, when this was verify_support's batch rather than
    # confirm_absence's: SCHEMA_INVALID, "output hit num_predict=1536",
    # identical on every retry since the overflow is deterministic). Kept well
    # under `verifier_num_ctx` so the prompt still has room for a full resume.
    # Matters again for verify_support the moment `verify_scope` is not `"none"`.
    verifier_num_predict: int = 4096
    temperature: float = 0.0
    top_k: int = 1
    seed: int = 42
    keep_alive: str = "30m"
    # Raised from 180: phase 2 reads a full resume at `verifier_num_ctx` on a
    # 12b model, which is slower than anything phase 1 does.
    request_timeout_s: int = 300
    max_retries: int = 2
    # Reasoning models emit their chain of thought *into the generation budget*
    # before producing any JSON. Measured on `gemma4:12b`: 1536 tokens consumed
    # entirely by `thinking`, empty `content`, and the call surfaced as
    # SCHEMA_INVALID; at 4096 tokens it still had not finished after 82 s.
    #
    # This also breaks 10.1's arithmetic, which reserves `num_predict` for
    # *output* — a thinking model spends that reservation on reasoning nobody
    # scores. Disabling it is what makes such a model usable here at all.
    disable_thinking: bool = True

    # --- Verification (10.6) ---
    verification_enabled: bool = True
    # `must_have_and_borderline` is the lever to pull if `all`'s escalation
    # rate proves unmanageable — a smaller queue, not a bigger one (19.2).
    # `none` turns `verify_support` off entirely: measured this session
    # (`eval/compare_verifiers.py` against the 12-case labelled set) as adding
    # no catch beyond what Stage B's Python check already catches, at the cost
    # of a full second-model GPU call per candidate. Default is `none` for
    # that reason — flip back to `all` or `must_have_and_borderline` to
    # re-enable it, and re-run the eval against a bigger corpus before trusting
    # that measurement generally. Absence checking is never skipped by scope
    # regardless of its value: it is the only check on a `none`, and `none` on
    # a must-have is the disqualifying outcome.
    verify_scope: Literal["all", "must_have_and_borderline", "none"] = "none"
    borderline_ratio_max: float = 0.75

    # --- Budget (10.1) ---
    max_resume_tokens: int = 4200
    max_criteria: int = 12
    exact_token_count: bool = True
    token_estimate_safety_margin: float = 1.35

    # --- File intake (8) ---
    max_file_bytes: int = 5 * 1024 * 1024
    max_pages: int = 50
    max_decompressed_bytes: int = 50 * 1024 * 1024
    max_compression_ratio: int = 100
    parse_timeout_s: int = 60
    # Raised from the spec's 1024 by measurement. Applied as RLIMIT_DATA (see
    # intake/sandbox.py — RLIMIT_AS cannot be used for this). The OCR path holds
    # ~950 MB at its plateau regardless of page count, and 1024 MB aborts on any
    # document past three pages. Measured against a 20-page scan: 1024 fails,
    # 1536 is the floor, 2048 leaves headroom.
    #
    # Under-provisioning does not fail cleanly — it segfaults or hangs until the
    # parent's wall clock fires. Re-measure before lowering.
    parse_mem_limit_mb: int = 2048
    allowed_extensions: tuple[str, ...] = (".pdf", ".docx")

    # --- Job-description intake (8, 9.1) ---
    #
    # Which ways a requisition may be raised. `upload` parses a PDF/DOCX through
    # the same sandboxed parser resumes go through; `paste` is the original
    # textarea. `both` offers the choice.
    #
    # **Enforced server-side, not only in the interface.** `GET /config` tells
    # the browser which controls to render, but a control that only hides a
    # button is not a control — `POST /jd-documents` and `create_position` each
    # refuse the method this forbids.
    jd_intake_mode: Literal["both", "upload", "paste"] = "both"
    # Deliberately far tighter than the resume path's 5 MB / 50 pages / 60 s. A
    # job description is a one-to-three page document, and unlike a resume this
    # parse happens inside a request, holding one of the API's threadpool
    # threads for its whole duration — so these caps are what bound that hold,
    # and they are the only thing that does. Raising them trades API
    # availability for the ability to accept a document nobody writes.
    jd_max_file_bytes: int = 2 * 1024 * 1024
    jd_max_pages: int = 10
    jd_parse_timeout_s: int = 15
    # How many job descriptions may be parsed at once, across the process.
    # Each parse permits a child `parse_mem_limit_mb`, so this is a memory
    # ceiling as much as a concurrency one: 2 x 2048 MB is defensible beside
    # Ollama's VRAM on the same host. Acquired non-blocking — waiting for the
    # semaphore would park the threadpool thread this exists to protect, which
    # converts a memory problem into an identical availability problem.
    jd_max_concurrent_parses: int = 2

    # --- Safety / fairness ---
    redact_pii: bool = True
    injection_detection: bool = True
    evidence_match_ratio: float = 0.60  # 10.5 — AND
    # 10.5 — AND. **Recalibrated from 25 at build step 19, against measured data.**
    #
    # 25 was inherited from the rejected `or 25 chars` clause (22.1), where it
    # was a *sufficient* condition — "verified if at least 25 chars matched".
    # Reusing it as a *necessary* condition was never measured.
    #
    # `python -m eval.escalation --sweep` over 25 real quotes:
    #
    #     chars   would fail   of those, exact (ratio=1.00)
    #        12            4                             0
    #        16            4                             0   <- chosen
    #        20            5                             1
    #        25            7                             3   <- was
    #
    # At 25, three verbatim correct quotes were escalated purely for brevity
    # ('Mentored 3 juniors', 'Python/Django REST APIs', 'wrote tooling in
    # Python'). 16 removes every one of those false positives while rejecting
    # exactly the same bad quotes as 12 — so nothing was traded away for it.
    #
    # The stopword-confetti attack this guarded is still blocked, by
    # `evidence_min_block_tokens = 3` requiring a contiguous run and
    # `evidence_match_ratio = 0.60` requiring most of the quote to align.
    evidence_match_min_chars: int = 16
    evidence_min_block_tokens: int = 3  # 10.5 — AND. Do not set to 1.
    # 10.5 C. How far back to look for a negation marker before a verified
    # quote. Six tokens covers "has no production experience with" without
    # reaching back into the previous bullet, where a "not" belonging to a
    # different sentence would flag a perfectly good quote.
    negation_window_tokens: int = 6
    freetext_screen: bool = True
    # 19.2 — **unset on purpose.** 3% was a guess, and a guessed budget is worse
    # than none: it reads as a measurement in every report that quotes it. The
    # worker warns on every run while this is None, so "measure it later" cannot
    # quietly become "never", and setting it requires naming the run it came
    # from — a number nobody can trace back is a number nobody can revisit.
    escalation_budget: float | None = None
    escalation_budget_source_run: str | None = None

    # --- Ranking ---
    band_thresholds: tuple[float, float, float] = (7.5, 5.5, 3.5)

    # --- Review / UI (15.5) ---
    bulk_decision_enabled: bool = True
    # Do not disable. Candidates are `review_required` precisely because the
    # system could not be confident about them, so they are the exact set that
    # must be opened individually — a bulk action over them is the oversight
    # control being switched off through the interface.
    bulk_excludes_review_required: bool = True

    # --- API ---
    api_host: str = "127.0.0.1"  # loopback: no external listener in the PoC
    api_port: int = 8000
    auth_mode: Literal["stub", "ldap", "local"] = "stub"
    dev_actor_id: str = "poc-operator"

    # --- Reviewer interface (decision #11) ---
    #
    # The built React bundle, served by this process at `/ui/` when the directory
    # exists. Same-origin is the point: the browser talks to the process that
    # served it, so the API needs no CORS middleware and there is no configurable
    # API host for an operator to point at another machine. Absent (a source
    # checkout that has not run `npm run build`) the API serves its endpoints and
    # nothing at `/ui/`, which is exactly what the Vite dev server wants.
    web_dist_dir: str = "web/dist"

    # --- Worker (16) ---
    # **Stable across restarts, unique per worker.** Startup reclaim resets every
    # job still `claimed` by this worker_id, on the reasoning that a process
    # which has claimed nothing yet can only be seeing its own previous life
    # (16.5). Both halves of that name matter, and they pull in opposite
    # directions:
    #
    # *Stable*, or recovery never runs. This used to include `os.getpid()`, and
    # the pid is reissued on every start — so a worker killed mid-job came back
    # under a new name, found nothing claimed by it, and left the job `claimed`
    # forever. `no_pending` counts `claimed`, so the phase never drained and the
    # run never completed, silently. `Restart=on-failure` in the systemd unit
    # means that restart is the one the deployment performs by itself.
    #
    # *Unique*, or two workers reset each other's in-flight jobs at startup and
    # both judge the same resume. The hostname gives both properties for one
    # worker per host, which is the deployed topology. **Set WORKER_ID
    # explicitly before running a second worker on the same host.**
    worker_id: str = Field(default_factory=socket.gethostname)
    worker_poll_interval_s: int = 2
    heartbeat_interval_s: int = 15
    job_max_attempts: int = 3
    # **The last-resort lease, and it is deliberately enormous.** Startup reclaim
    # handles the ordinary crash; this covers the one case it cannot see, a job
    # left `claimed` under a *different* name after a host rename or a changed
    # `WORKER_ID`. Without it such a job stays claimed forever and — because
    # `no_pending` counts `claimed` — the run never completes.
    #
    # Sized against the worst case a job can legitimately take, not the typical
    # one: 60 s parsing (`parse_timeout_s`) plus up to two LLM calls, each of
    # which retries `max_retries` times at `request_timeout_s` with backoff.
    # That is roughly 30 minutes. Six hours leaves a 12x margin.
    #
    # The margin is the point. This is the one place a wall clock is consulted,
    # and a clock that steps forward would expire live work — the failure the
    # rest of 16.5 is built to avoid (an air-gapped box has no dependable NTP).
    # A window this wide means a step would have to be measured in hours to do
    # damage, and nothing here needs recovering quickly. Never shorten it toward
    # the worst-case job time to make recovery faster; add a heartbeat instead,
    # which is what a shorter lease actually requires.
    job_lease_timeout_s: int = 6 * 60 * 60
    # How often the sweep runs while the worker is idle. Measured on a monotonic
    # clock, so the *scheduling* of the check is immune to the clock steps the
    # check itself has to tolerate.
    lease_sweep_interval_s: int = 600
    # `fast_lane_max_files` and `run_aging_hours` were removed with the fast lane
    # itself (17.3). Scheduling is strict FIFO by run creation: batch duration
    # was never the constraint, and shortest-job-first bought nothing against
    # reviewer time while adding the starvation problem that aging existed to
    # fix. Both settings were unread by any code well before this.
    #
    # Measured end-to-end judge time at build step 9 (5.39 s), not the spec's
    # original 4.7 s estimate. Drives the ETA, so it should be re-measured
    # whenever the model or num_ctx changes — an ETA quoted from a stale
    # constant is worse than none, because people plan around it.
    seconds_per_resume: float = 5.4

    # --- Storage / observability ---
    # **Postgres only.** `db_backend` and `db_path` are gone: SQLite was the
    # proof-of-concept backend, and supporting both cost more than it saved —
    # three dialect bugs shipped behind a portability check that could only ever
    # catch the differences somebody had already thought of.
    #
    # SQLAlchemy URL, e.g. `postgresql+psycopg://user:pass@host:5432/screener`.
    # Kept out of the repository and required at runtime: put it in `.env` as
    # DB_URL (these settings carry no env prefix — the variable is the field
    # name). There is deliberately no default, because a default that silently
    # connects somewhere is worse than a startup that refuses to.
    db_url: str = ""
    # Connection pool, per process (12.3). `pool_size` is the number kept open;
    # `max_overflow` how many extra may be opened under burst before callers
    # queue. The API serves sync handlers from a threadpool, so the ceiling that
    # matters is threads-in-flight, not requests/second.
    #
    # The pool also bounds descriptors, which is what the hand-rolled
    # thread-local cache it replaced failed to do — that leaked one connection
    # per thread, forever, until the process ran out.
    db_pool_size: int = 5
    db_max_overflow: int = 10
    # Seconds to wait for a free connection before failing rather than hanging.
    db_pool_timeout: int = 30
    # Recycle before a server-side idle timeout closes a connection underneath
    # us — Postgres and pgbouncer both drop idle connections.
    db_pool_recycle_s: int = 1800
    # Override per deployment via RESUMES_DIR in `.env` — point it at the mount
    # when the share is available. **Prefer an absolute path there:** a relative
    # one resolves against each process's working directory, so `./resumes`
    # means `<cwd>/resumes`, not "next to the repo".
    resumes_dir: str = "data/resumes"
    quarantine_dir: str = "data/quarantine"
    # 18 — raw model output, captured **only** on a schema failure. Removing
    # continuous tracing left schema errors undebuggable; failure-only capture
    # is bounded, but it is still candidate text, so 0700 and inside the
    # erasure path.
    capture_raw_on_failure: bool = True
    failure_dir: str = "data/failures"
    log_dir: str = "data/logs"
    # Readiness gate on the **local** filesystem — failure captures, logs and
    # quarantined files, all under `data/`. It no longer measures the database:
    # `resume_text` and `sent_text` land in Postgres now, on a volume this
    # process may not be able to see at all. Sizing that volume is an operational
    # task, not something this gate can do; see `service.health`.
    min_free_disk_gb: int = 20

    @property
    def app_version(self) -> str:
        return _app_version()

    # An unset key in .env (e.g. `JUDGE_DIGEST_PIN=`) arrives as "" and would
    # otherwise fail parsing; treat it as "not configured".
    @field_validator("judge_digest_pin", "verifier_digest_pin", mode="before")
    @classmethod
    def _empty_str_to_none(cls, v: Any) -> Any:  # noqa: ANN401 — pre-validation input is genuinely untyped
        if isinstance(v, str) and not v.strip():
            return None
        return v

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
