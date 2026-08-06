"""Typed configuration, read from the environment and `.env` (spec 7).

Several values here are load-bearing rather than cosmetic:

``num_ctx`` — Ollama's default is 4096 and it truncates the prompt *silently* on
overflow. The model then judges a partially-read resume with no error raised,
which is indistinguishable downstream from a genuinely weak candidate.

``num_predict`` — left unset, output caps truncate the JSON mid-object and the
failure surfaces as a schema error whose real cause is invisible.

``model_digest_pin`` — model tags are mutable. Re-pulling ``granite4.1:8b`` can
change the weights underneath decisions already stored, breaking reproducibility
and the audit record.

``evidence_*`` — the three conditions in 10.5 are combined with AND. Any one of
them alone admits fabricated evidence.

``OLLAMA_NUM_PARALLEL=1`` belongs in the service environment, not here: slot
reuse is the largest remaining source of run-to-run variation (10.8).
"""

from pathlib import Path
from typing import Any, Literal

from pydantic import field_validator
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
    ollama_host: str = "http://localhost:11434"
    chat_model: str = "granite4.1:8b"
    model_digest_pin: str | None = None
    num_ctx: int = 8192
    num_predict: int = 1536
    temperature: float = 0.0
    top_k: int = 1
    seed: int = 42
    keep_alive: str = "30m"
    request_timeout_s: int = 180
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
    freetext_screen: bool = True
    escalation_budget: float = 0.03  # 18.2 — warn above this

    # --- Ranking ---
    band_thresholds: tuple[float, float, float] = (7.5, 5.5, 3.5)

    # --- API ---
    api_host: str = "127.0.0.1"  # loopback: no external listener in the PoC
    api_port: int = 8000
    auth_mode: Literal["stub", "ldap", "local"] = "stub"
    dev_actor_id: str = "poc-operator"

    # --- Worker (16) ---
    worker_id: str = "worker-1"
    worker_poll_interval_s: int = 2
    heartbeat_interval_s: int = 15
    job_max_attempts: int = 3
    fast_lane_max_files: int = 150
    # Hours a run may wait before it is promoted into the fast lane. Without
    # aging, a steady trickle of small runs starves a 1,000-CV posting forever
    # (16.3).
    run_aging_hours: float = 2.0
    # Measured end-to-end judge time at build step 9 (5.39 s), not the spec's
    # original 4.7 s estimate. Drives the ETA, so it should be re-measured
    # whenever the model or num_ctx changes — an ETA quoted from a stale
    # constant is worse than none, because people plan around it.
    seconds_per_resume: float = 5.4

    # --- Storage / observability ---
    db_path: str = "data/screener.db"
    resumes_dir: str = "data/resumes"
    quarantine_dir: str = "data/quarantine"
    trace_dir: str = "data/traces"
    log_dir: str = "data/logs"
    trace_enabled: bool = True
    min_free_disk_gb: int = 20  # readiness gate; traces grow fast

    @property
    def app_version(self) -> str:
        return _app_version()

    # An unset key in .env (e.g. `MODEL_DIGEST_PIN=`) arrives as "" and would
    # otherwise fail parsing; treat it as "not configured".
    @field_validator("model_digest_pin", mode="before")
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
