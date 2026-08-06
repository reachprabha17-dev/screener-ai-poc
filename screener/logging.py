"""Structured logging and the trace store (spec 17).

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

**Traces are a second store of candidate data, and that is the whole risk.**
Each judge call writes the exact system and user strings sent to the model, which
means full resume text on disk outside the database. That is what makes offline
evaluation and prompt regression testing possible without re-running a GPU batch
(18) — and it is also why, if `trace_dir` sat outside the erasure path, it would
silently defeat `purge_candidate` **while appearing implemented**. Worse than not
having traces at all, because it would be reported as working.

So every trace is indexed in the `traces` table by `file_sha256`, deleted by
purge (12.6), written `0600` inside a `0700` directory, and pruned on a shorter
retention than the audit log.
"""

import json
import logging
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import structlog

from config.settings import settings

# Traces hold resume text; logs hold file hashes and timings. Both are candidate-
# adjacent enough that neither should be world-readable.
DIR_MODE = 0o700
FILE_MODE = 0o600

# Shorter than the audit log by design (17). The audit trail is the record of
# decisions and must outlive the raw text those decisions were made from.
DEFAULT_TRACE_RETENTION_DAYS = 30


def configure_logging(*, to_file: bool = True) -> None:
    """JSON to `data/logs/screener.jsonl`, once per process.

    Idempotent: the API's threadpool and the worker's loop both call it, and
    reconfiguring structlog mid-run would change the shape of a stream something
    is already parsing.
    """
    if structlog.is_configured():
        return

    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    if to_file:
        log_dir = Path(settings.log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(log_dir, DIR_MODE)  # noqa: PTH101 — Path.chmod is the same call
        handlers.append(logging.FileHandler(log_dir / "screener.jsonl", encoding="utf-8"))

    logging.basicConfig(format="%(message)s", handlers=handlers, level=logging.INFO)

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
    configure_logging()
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


# --- traces ------------------------------------------------------------------


class TraceWriter:
    """Writes one JSONL record per judge call.

    Returns the path so the caller can index it in the `traces` table. **The
    write and the index must both happen**, or purge cannot find the file — which
    is the failure 17 calls out as worse than having no traces.
    """

    def __init__(self, trace_dir: Path | None = None) -> None:
        self._dir = trace_dir or Path(settings.trace_dir)

    @property
    def enabled(self) -> bool:
        return settings.trace_enabled

    def write(self, run_id: str, record: Any) -> Path | None:  # noqa: ANN401 — pipeline.TraceRecord
        """Persist one judge call. Returns the path, or None when disabled.

        Laid out one file per candidate per run rather than one giant file:
        `purge_candidate` deletes whole files, and rewriting a shared log to
        remove one person's text is the kind of operation that half-succeeds.
        """
        if not self.enabled:
            return None

        directory = self._dir / run_id
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self._dir, DIR_MODE)  # noqa: PTH101
        os.chmod(directory, DIR_MODE)  # noqa: PTH101

        path = directory / f"{record.file_sha256}.jsonl"
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "run_id": run_id,
            "file_sha256": record.file_sha256,
            "model": settings.chat_model,
            "num_ctx": settings.num_ctx,
            "num_predict": settings.num_predict,
            "seed": settings.seed,
            "temperature": settings.temperature,
            "prompt_tokens": record.prompt_tokens,
            "attempts": record.attempts,
            # The exact strings sent. This is the resume text, and the reason
            # this file sits inside the erasure path.
            "system": record.system,
            "user": record.user,
            "output": record.output,
        }
        with path.open("w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        os.chmod(path, FILE_MODE)  # noqa: PTH101
        return path


def prune_traces(older_than_days: int = DEFAULT_TRACE_RETENTION_DAYS) -> list[Path]:
    """Delete trace files past their retention. Returns what was removed.

    Deliberately **not** transactional with the index — a file that vanishes
    before its row does is harmless (purge tolerates a missing file), whereas a
    row deleted before its file leaves resume text on disk that nothing knows
    about.
    """
    root = Path(settings.trace_dir)
    if not root.is_dir():
        return []

    cutoff = (datetime.now(UTC) - timedelta(days=older_than_days)).timestamp()
    removed: list[Path] = []
    for path in root.rglob("*.jsonl"):
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            continue
    return removed
