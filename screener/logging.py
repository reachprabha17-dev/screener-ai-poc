"""Structured logging and failure capture (spec 18).

**Two streams, and they are not interchangeable.**

`audit_log` (SQLite) records *who did what* — overrides, approvals, purges,
sign-off. Append-only, trigger-enforced, and readable by a human going through a
decision months later.

`screener.jsonl` records *what the system did* — latency, retries, parser
crashes, budget overflows, digest mismatches. It rotates. Free-text log lines are
unsearchable at batch scale, and the events that matter most are exactly the ones
you need to aggregate: "how many parses crashed last night" is a question you
cannot ask of prose.

Every event carries `run_id`, `job_id`, `file_sha256`, `stage`, `duration_ms`,
`worker_id`, `actor_id` where they apply — so a single resume's journey can be
reconstructed from one grep.

**Failure capture replaced continuous tracing.** Tracing every judge call wrote
the exact strings sent to the model — full resume text on disk, outside the
database, needing its own index, retention, permissions and erasure path. A
second PII store is a second thing to get wrong, and `sent_text` now holds the
same content inside the database where all of that already exists (12.6).

What removing it cost was debuggability: when the model returns something that
fails schema validation, the output that failed is the only thing that explains
why, and it was gone. So the **raw model output is captured on validation failure
and only then** (`capture_raw_on_failure`). Failure-only keeps it bounded — a
healthy run writes nothing — but it is still candidate text, so it is written
`0600` inside a `0700` directory and named with the file hash so
`purge_candidate` can find it without a second index table.
"""

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from config.settings import settings

# Failure captures hold model output derived from resume text; logs hold file
# hashes and timings. Neither should be world-readable.
DIR_MODE = 0o700
FILE_MODE = 0o600


def configure_logging(*, to_file: bool = True) -> None:
    """JSON to `data/logs/screener.jsonl`, once per process.

    Idempotent: the API's threadpool and the worker's loop both call it, and
    reconfiguring structlog mid-run would change the shape of a stream something
    is already parsing.
    """
    if structlog.is_configured():
        return

    # stderr takes everything; the console is the right place for a library's
    # chatter. `screener.jsonl` takes **only this package's events**.
    logging.basicConfig(
        format="%(message)s", handlers=[logging.StreamHandler(sys.stderr)], level=logging.INFO
    )

    if to_file:
        log_dir = Path(settings.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, DIR_MODE)  # noqa: PTH101 — Path.chmod is the same call
        # **On the `screener` logger, not the root one.** Every `get_logger`
        # name is a child of it (`screener.worker_loop`, `screener.clients...`),
        # so one handler here catches all of them and nothing else.
        #
        # Attached to root, this file collected every INFO line any dependency
        # emitted: httpx logs one per Ollama health poll, and the file measured
        # 15.7 MB of which 99.8% was `HTTP Request: GET /api/tags` — 229,769
        # noise lines against 395 real events. Worse, those lines are not JSON,
        # so a `.jsonl` file nothing could parse. Silencing httpx by name fixes
        # the library that happened to be loudest; urllib3, uvicorn, watchdog,
        # yoyo and streamlit are all still there to take its place. Scoping the
        # handler removes the whole class instead of the instance.
        handler = logging.FileHandler(log_dir / "screener.jsonl", encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(message)s"))
        logging.getLogger("screener").addHandler(handler)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str = "screener") -> Any:  # noqa: ANN401 — structlog's BoundLogger is dynamic
    """A logger whose output reaches `screener.jsonl`, whatever it is named.

    The file handler is bound to the `screener` logger so third-party chatter
    cannot reach the stream (see `configure_logging`). Module callers pass
    `__name__` and are already children of it, but the parameter accepts any
    string — so a name from outside the package is namespaced under it here
    rather than silently logging into a void. The logger name does not appear
    in the rendered JSON, so this changes routing and nothing an operator reads.
    """
    configure_logging()
    if name != "screener" and not name.startswith("screener."):
        name = f"screener.{name}"
    return structlog.get_logger(name)


def bind_job(run_id: str, job_id: int, worker_id: str) -> None:
    """Attach run/job context to every event on this thread.

    Bound once per job rather than passed to every call: a `duration_ms` with no
    `file_sha256` beside it cannot be traced back to a candidate, and that is
    exactly the event someone will need six months later.
    """
    structlog.contextvars.bind_contextvars(run_id=run_id, job_id=job_id, worker_id=worker_id)


def clear_context() -> None:
    structlog.contextvars.clear_contextvars()


# --- failure capture (18) ----------------------------------------------------

# `<file_sha256>.<something>.json`. The hash is the first component so purge can
# find every capture for a candidate with a glob — deliberately not a second
# index table, since needing one is what made continuous tracing expensive.
FAILURE_SUFFIX = ".json"


def failure_dir() -> Path:
    return Path(settings.failure_dir)


def write_failure(
    *,
    file_sha256: str,
    prompt_hash: str,
    raw_output: str,
    error: str,
    job_id: int | None = None,
) -> Path | None:
    """Persist one model output that failed validation. Returns the path, or None.

    Called on `ValidationError` and nowhere else. A run where nothing fails
    writes nothing, which is what keeps this bounded where tracing was not.

    The raw output is the whole point: a `SCHEMA_INVALID` with the offending text
    thrown away leaves nothing to diagnose, and the usual causes — the model
    emitting prose around the JSON, or `num_predict` truncating mid-object — are
    indistinguishable from the exception alone.
    """
    if not settings.capture_raw_on_failure:
        return None

    directory = failure_dir()
    directory.mkdir(parents=True, exist_ok=True)
    os.chmod(directory, DIR_MODE)  # noqa: PTH101 — Path.chmod is the same call

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
    path = directory / f"{file_sha256}.{stamp}{FAILURE_SUFFIX}"
    payload = {
        "ts": datetime.now(UTC).isoformat(),
        "file_sha256": file_sha256,
        "job_id": job_id,
        "prompt_hash": prompt_hash,
        "error": error,
        "num_ctx": settings.num_ctx,
        "num_predict": settings.num_predict,
        "raw_output": raw_output,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, default=str), encoding="utf-8")
    os.chmod(path, FILE_MODE)  # noqa: PTH101
    return path


def failure_paths_for(file_sha256: str) -> list[Path]:
    """Every capture belonging to one candidate, for the erasure path (12.10)."""
    directory = failure_dir()
    if not directory.is_dir():
        return []
    return sorted(directory.glob(f"{file_sha256}.*{FAILURE_SUFFIX}"))
