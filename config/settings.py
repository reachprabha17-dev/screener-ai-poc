"""Typed configuration, read from the environment and `.env` (spec 7).

Several values here are load-bearing rather than cosmetic:

``num_ctx`` — Ollama's default is 4096 and it truncates the prompt *silently* on
overflow. The model then judges a partially-read resume with no error raised,
which is indistinguishable downstream from a genuinely weak candidate.

``num_predict`` — left unset, output caps truncate the JSON mid-object and the
failure surfaces as a schema error whose real cause is invisible.

``judge_digest_pin`` — model tags are mutable. Re-pulling ``granite4.1:8b`` can
change the weights underneath decisions already stored, breaking reproducibility
and the audit record.

``evidence_*`` — the three conditions in 10.5 are combined with AND. Any one of
them alone admits fabricated evidence.

``OLLAMA_NUM_PARALLEL=1`` belongs in the service environment, not here: slot
reuse is the largest remaining source of run-to-run variation (10.8).
"""

import os
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
    # Two models, deliberately different weights (1.3). A verifier trained on
    # the same data as the judge shares its blind spots, and a second opinion
    # that agrees for the same reason as the first is not a second opinion.
    # They do not co-reside in 12 GB, which is why runs are two-phase (17.4).
    ollama_host: str = "http://localhost:11434"
    judge_model: str = "granite4.1:8b"
    verifier_model: str = "gemma4:12b"
    judge_digest_pin: str | None = None
    verifier_digest_pin: str | None = None
    num_ctx: int = 8192
    num_predict: int = 1536
    # 10.6 B sends the whole resume plus every `none` criterion in one call, so
    # the verifier needs materially more room than the judge.
    verifier_num_ctx: int = 16384
    # verify_support (10.6 A) batches a support judgment, a suggested verdict,
    # and up to a 200-char rationale *per criterion in scope* into one call —
    # strictly more per-item output than the judge's own verdict+evidence, over
    # the same set of criteria. Sharing the judge's 1536-token budget truncates
    # that response mid-JSON on any candidate with more than a handful of
    # in-scope criteria (observed in practice: SCHEMA_INVALID, "output hit
    # num_predict=1536", identical on every retry since the overflow is
    # deterministic). Kept well under `verifier_num_ctx` so the prompt still has
    # room for a full resume.
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
    # `all` by default. `must_have_and_borderline` is the lever to pull if the
    # escalation rate proves unmanageable — a smaller queue, not a bigger one
    # (19.2). Absence checking is never skipped by scope: it is the only check
    # on a `none`, and `none` on a must-have is the disqualifying outcome.
    verify_scope: Literal["all", "must_have_and_borderline"] = "all"
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
    # **Unique per process, not a constant.** Startup reclaim resets every job
    # still `claimed` by this worker_id, on the reasoning that a process which
    # has claimed nothing yet can only be seeing its own previous life. That
    # holds exactly as long as the id is unique: two workers sharing `worker-1`
    # would each reset the other's *in-flight* jobs at startup, and both would
    # then judge the same resume. Override with WORKER_ID where a stable name
    # matters (a systemd unit that must reclaim its own work across a restart).
    worker_id: str = Field(default_factory=lambda: f"{socket.gethostname()}-{os.getpid()}")
    worker_poll_interval_s: int = 2
    heartbeat_interval_s: int = 15
    job_max_attempts: int = 3
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
    # Selects the migration directory and, later, the SQL dialect (12.2). It is
    # not cosmetic: migrations live one directory per backend because yoyo reads
    # a single directory without recursing, so the wrong value here means every
    # migration is silently skipped rather than failing loudly.
    db_backend: Literal["sqlite", "mssql"] = "sqlite"
    db_path: str = "data/screener.db"
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
    # Readiness gate. `resume_text` + `sent_text` add roughly 40 MB per 1,000-CV
    # run to the database, so the floor still matters after traces are gone.
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
