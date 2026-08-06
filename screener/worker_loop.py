"""Screening daemon (spec §16).

A separate process from the API and the UI, because a 1,000-CV batch runs for
over an hour. It cannot live in a request or a Streamlit session, it must survive
a UI restart, and the API must stay responsive while it runs (§2).

**One resume at a time. No thread pool.** `OLLAMA_NUM_PARALLEL=1` means parallel
calls would queue at Ollama anyway, so concurrency here would buy nothing and
cost the transaction pattern its simplicity — claim (tx) → screen (no tx) → save
(tx), with the write lock never held across a ~5 s inference call.

**The loop owns no SQL.** Every database touch goes through `service.py`, which
owns the transaction boundary (§12.2). A daemon holding its own transactions
would be a second place where a saved result and its closed job could drift
apart, and that divergence either loses a candidate or screens them twice.

**Crash recovery is the first thing that happens.** On boot, any job still
`claimed` by *this* worker id is from a previous life — this process has claimed
nothing yet, so there is no ambiguity and no clock arithmetic. That matters on an
air-gapped box with no NTP, where a lease-expiry scheme would either reclaim live
work or strand dead work on a clock step (§16.5).

The loop lives **inside the package** rather than in the root `worker.py`.
The wheel packages `screener/` and `config/` only, so a root-level module is
not importable from an installed environment — and `cli.py` needs this class
for its break-glass `work` command. Duplicating the loop there would give the
emergency path different semantics from the daemon, which is precisely when
you least want a surprise.

`worker.py` at the repository root remains the daemon entry point (§4, §16.6).
"""

import signal
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import FrameType
from typing import Any

from config.settings import settings
from screener.logging import TraceWriter, bind_job, clear_context, get_logger
from screener.models import Candidate, Rubric, Run
from screener.pipeline import Deps, TraceRecord, TraceSink, file_sha256, screen_one
from screener.ports import CacheKey, Job
from screener.service import ScreenerService
from screener.storage.connection import require_current_schema


@dataclass
class Worker:
    """The loop, as an object so it can be driven one job at a time in tests.

    A `while True` in a module-level function is only testable by killing it.
    """

    service: ScreenerService
    deps: Deps
    worker_id: str = field(default_factory=lambda: settings.worker_id)
    poll_interval_s: float = field(default_factory=lambda: settings.worker_poll_interval_s)

    traces: TraceWriter = field(default_factory=TraceWriter)

    _stopping: bool = False
    _rubrics: dict[str, tuple[Rubric, Run]] = field(default_factory=dict)
    _log: Any = field(default_factory=get_logger)

    # --- lifecycle -----------------------------------------------------------

    def request_stop(self, signum: int | None = None, frame: FrameType | None = None) -> None:
        """Finish the job in hand, then exit. Never abandon work mid-flight.

        SIGTERM arrives on every deploy and every `systemctl restart`. Dropping
        the current resume would leave a claimed job for startup reclaim to find
        and waste an inference call already paid for; finishing it costs seconds.
        """
        self._stopping = True

    def install_signal_handlers(self) -> None:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

    def startup(self) -> int:
        """Refuse to run against a stale schema, then reclaim my own orphans.

        Migrations are a gate, not a warning: a schema/code mismatch on a
        database of candidate decisions is an integrity incident (§12.1).
        """
        require_current_schema()
        return self.service.reclaim_orphaned(self.worker_id)

    # --- the loop ------------------------------------------------------------

    def run_forever(self) -> None:
        self.startup()
        while not self._stopping:
            if not self.run_once():
                # Nothing to do. Sleep in short slices so a stop signal is not
                # left waiting out a full poll interval.
                self._sleep_interruptibly(self.poll_interval_s)

    def run_once(self) -> bool:
        """Claim and process at most one job. True if work was done.

        Returns rather than loops so tests can step the worker deterministically
        and assert on the state between jobs.
        """
        if not self._has_disk_headroom():
            # Traces grow fast, and a full disk mid-batch is only recoverable if
            # the worker stopped before filling it (§17).
            self._sleep_interruptibly(self.poll_interval_s)
            return False

        job = self.service.claim_next_job(self.worker_id)
        if job is None:
            return False

        bind_job(job.run_id, job.id, self.worker_id)
        started = time.monotonic()
        try:
            candidate = self._process(job)
            self._log.info(
                "screened",
                stage="complete",
                file_sha256=candidate.file_sha256,
                scoreable=candidate.scoreable,
                review_required=candidate.review_required,
                band=candidate.band,
                flags=[f.value for f in candidate.flags],
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        except Exception as exc:  # noqa: BLE001 — the loop must outlive any single resume
            # `screen_one` turns document problems into flagged candidates, so
            # reaching here means infrastructure: the database, the model, or a
            # bug. Retryable, capped by `job_max_attempts` so a job that reliably
            # kills the worker cannot loop forever.
            self._log.error(
                "job_failed",
                stage="screen",
                error=f"{type(exc).__name__}: {exc}"[:500],
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            self.service.fail_job(job, f"{type(exc).__name__}: {exc}"[:2000], retryable=True)
        finally:
            self.service.finish_run_if_complete(job.run_id)
            clear_context()

        return True

    def _process(self, job: Job) -> Candidate:
        rubric, run = self._rubric_for(job.run_id)
        path = Path(job.file_path)

        sha = file_sha256(path) if path.is_file() else ""
        key = self.service.cache_key_for(run, rubric, sha)

        candidate = self._cached(key, job) if sha else None
        if candidate is not None:
            self._log.info("cache_hit", stage="cache", file_sha256=sha)
        else:
            deps = replace(self.deps, trace=self._trace_sink(job.run_id, sha))
            candidate = screen_one(path, rubric, deps, run_id=job.run_id, root=Path(run.folder))
            # `screen_one` hashes the file itself; prefer that over the pre-hash,
            # which is empty when the path vanished between claim and read.
            key = self.service.cache_key_for(run, rubric, candidate.file_sha256 or sha)

        self.service.complete_job(job, candidate, key)
        return candidate

    def _trace_sink(self, run_id: str, sha: str) -> TraceSink | None:
        """Write the trace **and** index it, or neither.

        A file with no row is resume text outside the erasure path; a row with
        no file is harmless. So the index write follows the file write, and a
        failure to index is logged loudly rather than swallowed.
        """
        if not self.traces.enabled:
            return None

        def sink(record: TraceRecord) -> None:
            path = self.traces.write(run_id, record)
            if path is None:
                return
            try:
                self.service.record_trace(run_id, record.file_sha256, path)
            except Exception as exc:  # noqa: BLE001 — never lose the resume, never lose the run
                self._log.error(
                    "trace_unindexed",
                    stage="trace",
                    file_sha256=record.file_sha256,
                    path=str(path),
                    error=str(exc)[:200],
                )

        return sink

    def _cached(self, key: CacheKey, job: Job) -> Candidate | None:
        """A prior judgment under identical conditions, re-filed under this run.

        This is what makes resumption and re-runs cheap. The stored candidate
        belongs to whichever run first produced it, so it is re-stamped with this
        `run_id` — the judgment is the same, the run it appears in is not.

        Transient failures never surface here: the partial index excludes them,
        so a network blip is re-attempted rather than served back as a permanent
        verdict (§12.5).
        """
        hit = self.service.cached_candidate(key)
        if hit is None:
            return None
        return hit.model_copy(update={"run_id": job.run_id})

    def _rubric_for(self, run_id: str) -> tuple[Rubric, Run]:
        """Cached per process: one lookup per run, not one per resume."""
        if run_id not in self._rubrics:
            self._rubrics[run_id] = self.service.rubric_for_run(run_id)
        return self._rubrics[run_id]

    # --- helpers -------------------------------------------------------------

    def _has_disk_headroom(self) -> bool:
        return self.service.health().disk_ok

    def _sleep_interruptibly(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while not self._stopping and time.monotonic() < deadline:
            time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))


def build_worker() -> Worker:
    from screener.clients.ollama_client import OllamaClient
    from screener.intake.sandbox import SandboxedParser

    llm = OllamaClient()
    return Worker(
        service=ScreenerService(llm=llm),
        deps=Deps(parser=SandboxedParser(), llm=llm),
    )


def main() -> int:
    worker = build_worker()
    worker.install_signal_handlers()
    worker.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
