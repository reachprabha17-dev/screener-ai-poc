"""Screening daemon (spec 16).

A separate process from the API and the UI, because a 1,000-CV batch runs for
over an hour. It cannot live in a request or a Streamlit session, it must survive
a UI restart, and the API must stay responsive while it runs (2).

**One resume at a time. No thread pool.** `OLLAMA_NUM_PARALLEL=1` means parallel
calls would queue at Ollama anyway, so concurrency here would buy nothing and
cost the transaction pattern its simplicity — claim (tx) → screen (no tx) → save
(tx), with the write lock never held across a ~5 s inference call.

**The loop owns no SQL.** Every database touch goes through `service.py`, which
owns the transaction boundary (12.2). A daemon holding its own transactions
would be a second place where a saved result and its closed job could drift
apart, and that divergence either loses a candidate or screens them twice.

**Crash recovery is the first thing that happens.** On boot, any job still
`claimed` by *this* worker id is from a previous life — this process has claimed
nothing yet, so there is no ambiguity and no clock arithmetic. That matters on an
air-gapped box with no NTP, where a lease-expiry scheme would either reclaim live
work or strand dead work on a clock step (16.5).

The loop lives **inside the package** rather than in the root `worker.py`.
The wheel packages `screener/` and `config/` only, so a root-level module is
not importable from an installed environment — and `cli.py` needs this class
for its break-glass `work` command. Duplicating the loop there would give the
emergency path different semantics from the daemon, which is precisely when
you least want a surprise.

`worker.py` at the repository root remains the daemon entry point (4, 16.6).
"""

import signal
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import FrameType
from typing import Any

from config.settings import settings
from screener.clients.ollama_client import SchemaInvalidError
from screener.logging import bind_job, clear_context, get_logger, write_failure
from screener.models import Candidate, Rubric, Run
from screener.pipeline import Deps, file_sha256, judge_one, verify_one
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

    _stopping: bool = False
    _rubrics: dict[str, tuple[Rubric, Run]] = field(default_factory=dict)
    _log: Any = field(default_factory=get_logger)
    # Monotonic, not wall clock: this only decides *when to look*, and it must
    # not be knocked about by the same clock steps the lease check has to
    # tolerate. `-inf` so the first idle cycle always sweeps.
    _last_sweep: float = float("-inf")

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
        database of candidate decisions is an integrity incident (12.1).
        """
        require_current_schema()
        self._warn_if_no_escalation_budget()
        recovered = self.service.reclaim_orphaned(self.worker_id)
        # A rename or a changed WORKER_ID only takes effect at a restart, so a
        # restart is exactly when a job stranded under the old name appears.
        # Sweep here too rather than waiting out the first idle interval — the
        # lease still has to have expired, so this reclaims nothing that is live.
        self.sweep_expired_leases()
        return recovered

    def _warn_if_no_escalation_budget(self) -> None:
        """Nag on every start while `escalation_budget` is unset (19.2).

        The escalation rate is the constraint that decides whether human
        oversight is real: past roughly 10% of a run, reviewers click through and
        the control fails silently while still appearing to work. The number can
        only come from a real run, and without something saying so on every
        start, "measure it later" becomes "never".
        """
        if settings.escalation_budget is None:
            self._log.warning(
                "escalation_budget_unset",
                stage="startup",
                msg="Measure the rate on this run and record escalation_budget_source_run",
            )

    # --- the loop ------------------------------------------------------------

    def run_forever(self) -> None:
        self.startup()
        while not self._stopping:
            if not self.run_once():
                # Idle is the only safe moment to sweep: this worker holds no
                # job, so there is nothing of its own for a sweep to disturb.
                self.sweep_expired_leases()
                # Nothing to do. Sleep in short slices so a stop signal is not
                # left waiting out a full poll interval.
                self._sleep_interruptibly(self.poll_interval_s)

    def sweep_expired_leases(self) -> int:
        """Reclaim jobs another worker has held past the lease, at most so often.

        The backstop to startup reclaim (16.5). Startup handles a crash by name;
        this handles the job with no name left to match — claimed as
        `old-hostname` before a rename, which nothing will ever answer to again.
        Without it that job stays `claimed`, and `no_pending` counts `claimed`,
        so the run never completes.

        **Rate-limited on a monotonic clock.** The idle loop turns over every
        couple of seconds and this is a write; `time.monotonic()` decides when to
        look, so the scheduling cannot be dragged around by the wall-clock steps
        the lease comparison itself has to tolerate.

        The query cannot touch this worker's own job — it excludes
        `claimed_by = worker_id` — so calling it here is safe regardless of how
        long anything has been running.
        """
        if time.monotonic() - self._last_sweep < settings.lease_sweep_interval_s:
            return 0
        self._last_sweep = time.monotonic()
        recovered = self.service.reclaim_expired_leases(self.worker_id)
        if recovered:
            self._log.warning(
                "lease_expired_reclaim",
                jobs=recovered,
                lease_seconds=settings.job_lease_timeout_s,
            )
        return recovered

    def run_once(self) -> bool:
        """Claim and process at most one job. True if work was done.

        Returns rather than loops so tests can step the worker deterministically
        and assert on the state between jobs.
        """
        if not self._has_disk_headroom():
            # A full disk mid-batch is only recoverable if the worker stopped
            # before filling it (18).
            self._sleep_interruptibly(self.poll_interval_s)
            return False

        active = self.service.next_active_phase()
        if active is None:
            return False
        run, phase = active

        # **Once per phase, not once per resume.** 12 GB of VRAM holds one of the
        # two models, and swapping per candidate costs a 10–20 s load every time:
        # two loads across a 1,000-CV run against two thousand (17.4).
        self._ensure_model_for(phase)

        job = self.service.claim_next_job(self.worker_id, phase)
        if job is None:
            # The phase has drained. Advancing here rather than on a timer means
            # the transition happens the moment the last job lands.
            self.service.advance_phase_if_complete(run.id)
            self.service.finish_run_if_complete(run.id)
            return False

        bind_job(job.run_id, job.id, self.worker_id)
        started = time.monotonic()
        try:
            candidate = self._verify(job) if job.phase == "verify" else self._process(job)
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
            # `judge_one` turns document problems into flagged candidates, so
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
            self.service.advance_phase_if_complete(job.run_id)
            self.service.finish_run_if_complete(job.run_id)
            clear_context()

        return True

    def _ensure_model_for(self, phase: str) -> None:
        model = settings.verifier_model if phase == "verify" else settings.judge_model
        self.deps.llm.ensure_loaded(model)

    def _verify(self, job: Job) -> Candidate:
        """Phase 2 for one already-judged candidate.

        A missing candidate is a completed job, not a failure: the row was
        purged between enqueue and claim, and retrying would fail identically
        until the attempt cap gave up and marked the job failed for a reason
        that has nothing to do with the run.
        """
        if job.candidate_id is None:
            raise ValueError(f"verify job {job.id} has no candidate")

        rubric, _run = self._rubric_for(job.run_id)
        candidate = self.service.get_candidate(job.candidate_id)

        verified = verify_one(candidate, rubric, self.deps)
        self.service.save_verification(job.candidate_id, verified)
        self.service.complete_verify_job(job)
        return verified

    def _process(self, job: Job) -> Candidate:
        rubric, run = self._rubric_for(job.run_id)
        path = Path(job.file_path)

        sha = file_sha256(path) if path.is_file() else ""
        key = self.service.cache_key_for(run, rubric, sha)

        candidate = self._cached(key, job) if sha else None
        if candidate is not None:
            self._log.info("cache_hit", stage="cache", file_sha256=sha)
        else:
            try:
                candidate = judge_one(
                    path, rubric, self.deps, run_id=job.run_id, root=Path(run.folder)
                )
            except SchemaInvalidError as exc:
                # Capture here rather than in the client or the pipeline: this is
                # the first frame that knows *which candidate* the output belongs
                # to, and a capture that cannot be attributed to a file hash is
                # one `purge_candidate` can never find again (18).
                write_failure(
                    file_sha256=sha,
                    prompt_hash=run.prompt_hash,
                    raw_output=exc.raw_output,
                    error=str(exc)[:2000],
                    job_id=job.id,
                )
                raise
            # `screen_one` hashes the file itself; prefer that over the pre-hash,
            # which is empty when the path vanished between claim and read.
            key = self.service.cache_key_for(run, rubric, candidate.file_sha256 or sha)

        self.service.complete_job(job, candidate, key)
        return candidate

    def _cached(self, key: CacheKey, job: Job) -> Candidate | None:
        """A prior judgment under identical conditions, re-filed under this run.

        This is what makes resumption and re-runs cheap. The stored candidate
        belongs to whichever run first produced it, so it is re-stamped with this
        `run_id` — the judgment is the same, the run it appears in is not.

        Transient failures never surface here: the partial index excludes them,
        so a network blip is re-attempted rather than served back as a permanent
        verdict (12.5).
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
