"""The work queue (spec 16, build gate 20 step 11).

The gate names four things: atomic claim, **startup reclaim after a simulated
`kill -9`**, the attempt cap, and run status transitions.

The reclaim test is the one that matters most. A worker that dies holding claimed
jobs is not a hypothetical — it is what happens on every deploy, every OOM, and
every power cut — and the failure mode is silent: the jobs stay `claimed`
forever, the run never completes, and nobody is told. So it is simulated with a
real `SIGKILL` against a real process, not by setting a column by hand.
"""

import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from helpers_storage import make_rubric_row

from config.settings import settings
from screener.models import Actor, Position, now
from screener.storage import jobs_store, positions_store, runs_store
from screener.storage.connection import apply_migrations, connect
from screener.storage.uow import UnitOfWork

ACTOR = Actor(id="poc-operator", display_name="PoC Operator", roles=frozenset({"admin"}))
WORKER = "worker-1"


@pytest.fixture
def db(tmp_path: Path) -> Path:
    path = tmp_path / "screener.db"
    apply_migrations(path)
    return path


@pytest.fixture
def uow(db: Path) -> Iterator[UnitOfWork]:
    connection = connect(db)
    try:
        yield UnitOfWork(connection)
    finally:
        connection.close()


def seed_run(
    uow: UnitOfWork,
    run_id: str = "run1",
    *,
    position_id: str = "p1",
    reference: str = "REQ-1",
    created_at: str | None = None,
) -> None:
    with uow as tx:
        positions_store.seed_user(tx, ACTOR.id, ACTOR.display_name)
        if positions_store.get(tx, position_id) is None:
            positions_store.create(
                tx,
                Position(
                    id=position_id,
                    reference=reference,
                    title="Backend Engineer",
                    jd_text="jd",
                    created_by=ACTOR.id,
                    created_at=now(),
                ),
            )
            make_rubric_row(tx, position_id=position_id, rubric_id=f"r-{position_id}")
        runs_store.create(
            tx,
            run_id=run_id,
            position_id=position_id,
            rubric_id=f"r-{position_id}",
            folder=f"data/resumes/{reference}",
            created_by=ACTOR.id,
            judge_model="granite4.1:8b",
            judge_digest="sha256:aaa",
            prompt_hash="p" * 64,
            redaction_on=True,
            num_ctx=8192,
            num_predict=1536,
            seed=42,
            app_version="v0.1.0",
        )
        if created_at is not None:
            tx.execute("UPDATE runs SET created_at = ? WHERE id = ?", (created_at, run_id))


def add_jobs(uow: UnitOfWork, run_id: str, count: int, prefix: str = "cv") -> None:
    stamp = now().isoformat()
    with uow as tx:
        for i in range(count):
            tx.execute(
                "INSERT INTO jobs (run_id, file_path, status, created_at, updated_at) "
                "VALUES (?, ?, 'pending', ?, ?)",
                (run_id, f"/data/{run_id}/{prefix}{i}.pdf", stamp, stamp),
            )


def minutes_ago(minutes: float) -> str:
    """A submission time relative to now, for ordering two runs against each other.

    Relative rather than a fixed date: `claim_next` orders by `runs.created_at`,
    and a hardcoded timestamp makes the intended ordering depend on when the
    suite happens to be run.
    """
    return (now() - timedelta(minutes=minutes)).isoformat()


def resumes(folder: Path, names: tuple[str, ...]) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_bytes(b"%PDF-1.4\n")
    return folder


# --- snapshot (16.2) --------------------------------------------------------


def test_snapshot_queues_one_job_per_eligible_file(uow: UnitOfWork, tmp_path: Path) -> None:
    seed_run(uow)
    folder = resumes(tmp_path / "REQ-1", ("a.pdf", "b.docx", "notes.txt", ".DS_Store"))

    with uow as tx:
        count = jobs_store.snapshot_folder(tx, "run1", folder)

    assert count == 2  # .txt and .DS_Store are not queued


def test_snapshot_recurses(uow: UnitOfWork, tmp_path: Path) -> None:
    seed_run(uow)
    folder = resumes(tmp_path / "REQ-1", ("a.pdf",))
    resumes(folder / "batch2", ("b.pdf",))

    with uow as tx:
        assert jobs_store.snapshot_folder(tx, "run1", folder) == 2


def test_snapshot_is_idempotent_which_is_what_makes_rescan_work(
    uow: UnitOfWork, tmp_path: Path
) -> None:
    """Rescan is the same call. It inserts only what is new (16.2)."""
    seed_run(uow)
    folder = resumes(tmp_path / "REQ-1", ("a.pdf", "b.pdf"))

    with uow as tx:
        assert jobs_store.snapshot_folder(tx, "run1", folder) == 2
    with uow as tx:
        assert jobs_store.snapshot_folder(tx, "run1", folder) == 0

    resumes(folder, ("c.pdf",))
    with uow as tx:
        assert jobs_store.snapshot_folder(tx, "run1", folder) == 1


def test_snapshot_does_not_follow_symlinks(uow: UnitOfWork, tmp_path: Path) -> None:
    """A symlinked directory could otherwise pull an unrelated tree into a run."""
    seed_run(uow)
    outside = resumes(tmp_path / "elsewhere", ("secret.pdf",))
    folder = resumes(tmp_path / "REQ-1", ("a.pdf",))
    (folder / "link.pdf").symlink_to(outside / "secret.pdf")

    with uow as tx:
        assert jobs_store.snapshot_folder(tx, "run1", folder) == 1


def test_snapshot_of_a_missing_folder_is_empty_not_an_error(
    uow: UnitOfWork, tmp_path: Path
) -> None:
    seed_run(uow)
    with uow as tx:
        assert jobs_store.snapshot_folder(tx, "run1", tmp_path / "nope") == 0


# --- gate: atomic claim ------------------------------------------------------


def test_claim_takes_exactly_one_job_and_marks_ownership(uow: UnitOfWork) -> None:
    seed_run(uow)
    add_jobs(uow, "run1", 3)

    with uow as tx:
        job = jobs_store.claim_next(tx, WORKER)

    assert job is not None
    assert job.claimed_by == WORKER
    assert job.attempts == 1

    with uow as tx:
        assert jobs_store.progress(tx, "run1") == jobs_store.RunProgress(
            pending=2, claimed=1, done=0, failed=0
        )


def test_a_claimed_job_is_never_claimed_twice(uow: UnitOfWork) -> None:
    """The property that makes double-processing impossible.

    One statement — `UPDATE ... WHERE id = (SELECT ...) RETURNING` — so there is
    no window between choosing a job and owning it.
    """
    seed_run(uow)
    add_jobs(uow, "run1", 5)

    claimed = []
    for _ in range(5):
        with uow as tx:
            job = jobs_store.claim_next(tx, WORKER)
        assert job is not None
        claimed.append(job.id)

    assert len(set(claimed)) == 5

    with uow as tx:
        assert jobs_store.claim_next(tx, WORKER) is None


CONCURRENT_CLAIMER = """
import sys, pathlib, time
sys.path.insert(0, {repo!r})
from screener.storage.connection import connect
from screener.storage.uow import UnitOfWork
from screener.storage import jobs_store

conn = connect(pathlib.Path({db!r}))
uow = UnitOfWork(conn)

# Start together, or the first process drains the queue before the others are
# even running and the test measures nothing.
target = float(sys.argv[2])
while time.time() < target:
    time.sleep(0.001)

claimed = []
while True:
    with uow as tx:
        job = jobs_store.claim_next(tx, sys.argv[1])
    if job is None:
        break
    claimed.append(job.id)
    time.sleep(0.005)  # stand-in for screening; without it there is no overlap
print(",".join(str(i) for i in claimed), flush=True)
"""

CONCURRENT_WORKERS = 4
CONCURRENT_JOBS = 40


def test_concurrent_workers_never_claim_the_same_job(db: Path, uow: UnitOfWork) -> None:
    """Atomicity under real contention, not just in sequence.

    Sequential claiming proves nothing about atomicity — the failure this guards
    against is two processes selecting the same row before either writes. A
    double-claim screens one candidate twice and writes the result twice, hitting
    `UNIQUE(file_sha256, run_id)` and aborting a batch mid-run.

    The barrier and the per-claim delay are both load-bearing *for the test*: an
    earlier version without them had one worker take all 40 jobs before the
    others started, and passed while exercising nothing.
    """
    seed_run(uow)
    add_jobs(uow, "run1", CONCURRENT_JOBS)

    script = CONCURRENT_CLAIMER.format(repo=str(Path.cwd()), db=str(db))
    start_at = time.time() + 2.0
    workers = [
        subprocess.Popen(  # noqa: S603 — fixed argv, test-local script
            [sys.executable, "-c", script, f"worker-{n}", str(start_at)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(CONCURRENT_WORKERS)
    ]

    per_worker: list[list[int]] = []
    for worker in workers:
        stdout, stderr = worker.communicate(timeout=120)
        assert worker.returncode == 0, stderr
        per_worker.append([int(i) for i in stdout.strip().split(",") if i])

    all_claimed = [job_id for claims in per_worker for job_id in claims]

    assert sorted(all_claimed) == sorted(set(all_claimed))  # no job claimed twice
    assert len(all_claimed) == CONCURRENT_JOBS  # and none lost
    # Proof the race actually happened rather than the queue being drained first.
    assert sum(1 for claims in per_worker if claims) > 1, per_worker


def test_claim_ignores_runs_that_are_not_active(uow: UnitOfWork) -> None:
    """An aborted run's jobs stay put — aborted is resumable, not discarded."""
    seed_run(uow)
    add_jobs(uow, "run1", 2)
    with uow as tx:
        runs_store.set_status(tx, "run1", "aborted")

    with uow as tx:
        assert jobs_store.claim_next(tx, WORKER) is None


def test_empty_queue_returns_none_rather_than_blocking(uow: UnitOfWork) -> None:
    seed_run(uow)
    with uow as tx:
        assert jobs_store.claim_next(tx, WORKER) is None


# --- scheduling (17.3) ------------------------------------------------------


def test_runs_are_served_first_in_first_out(uow: UnitOfWork) -> None:
    """FIFO by run creation, then job id. No fast lane (17.3).

    Shortest-job-first was removed rather than reimplemented: batch duration was
    never the constraint — recruiters currently screen a thousand CVs by hand and
    will not notice 2.6 hours against 78 minutes. Reviewer time is the
    constraint. The fast lane bought nothing against it, and it needed an aging
    rule of its own to stop a trickle of small runs starving a large one.
    """
    seed_run(uow, "first", position_id="p1", reference="A", created_at=minutes_ago(60))
    seed_run(uow, "second", position_id="p2", reference="B", created_at=minutes_ago(30))
    add_jobs(uow, "first", 2, prefix="f")
    add_jobs(uow, "second", 2, prefix="s")

    with uow as tx:
        job = jobs_store.claim_next(tx, WORKER)

    assert job is not None
    assert job.run_id == "first"


def test_a_large_run_is_not_overtaken_by_a_later_small_one(uow: UnitOfWork) -> None:
    """The behaviour change from v4, stated as a test so it is not a surprise.

    A specialist role with three applicants now waits behind a mass posting
    submitted before it. That is the accepted cost of dropping the fast lane —
    and the reason round-robin stays rejected too: ranking is only meaningful
    over a complete run, so partial progress across several runs gives every
    reviewer something they cannot act on.
    """
    seed_run(uow, "big", position_id="p1", reference="REQ-BIG", created_at=minutes_ago(60))
    seed_run(uow, "small", position_id="p2", reference="REQ-SMALL", created_at=minutes_ago(30))
    add_jobs(uow, "big", 200, prefix="big")
    add_jobs(uow, "small", 3, prefix="small")

    with uow as tx:
        job = jobs_store.claim_next(tx, WORKER)

    assert job is not None
    assert job.run_id == "big"


# --- gate: the attempt cap ---------------------------------------------------


def test_a_retryable_failure_returns_the_job_to_the_queue(uow: UnitOfWork) -> None:
    seed_run(uow)
    add_jobs(uow, "run1", 1)

    with uow as tx:
        job = jobs_store.claim_next(tx, WORKER)
        assert job is not None
        jobs_store.fail(tx, job.id, "ollama timeout", retryable=True)

    with uow as tx:
        assert jobs_store.progress(tx, "run1").pending == 1


def test_a_poison_file_stops_retrying_at_the_cap(uow: UnitOfWork) -> None:
    """A file that reliably kills the parser must not loop forever (16.5)."""
    seed_run(uow)
    add_jobs(uow, "run1", 1)

    for _ in range(settings.job_max_attempts):
        with uow as tx:
            job = jobs_store.claim_next(tx, WORKER)
            assert job is not None
            jobs_store.fail(tx, job.id, "parser crashed", retryable=True)

    with uow as tx:
        progress = jobs_store.progress(tx, "run1")
        assert progress.failed == 1
        assert jobs_store.claim_next(tx, WORKER) is None


def test_a_non_retryable_failure_retires_immediately(uow: UnitOfWork) -> None:
    seed_run(uow)
    add_jobs(uow, "run1", 1)

    with uow as tx:
        job = jobs_store.claim_next(tx, WORKER)
        assert job is not None
        jobs_store.fail(tx, job.id, "input rejected", retryable=False)

    with uow as tx:
        assert jobs_store.progress(tx, "run1").failed == 1


def test_a_clean_release_does_not_burn_an_attempt(uow: UnitOfWork) -> None:
    """Shutting down cleanly is not the file's fault.

    Without the decrement, a few restarts would exhaust the cap on a file that
    nothing is wrong with.
    """
    seed_run(uow)
    add_jobs(uow, "run1", 1)

    for _ in range(5):
        with uow as tx:
            job = jobs_store.claim_next(tx, WORKER)
            assert job is not None
            jobs_store.release(tx, job.id)

    with uow as tx:
        job = jobs_store.claim_next(tx, WORKER)

    assert job is not None
    assert job.attempts == 1


def test_completion_records_the_hash_read_at_claim_time(uow: UnitOfWork) -> None:
    """A file swapped between snapshot and processing is hashed as what was read."""
    seed_run(uow)
    add_jobs(uow, "run1", 1)

    with uow as tx:
        job = jobs_store.claim_next(tx, WORKER)
        assert job is not None
        jobs_store.complete(tx, job.id, "sha-of-actual-bytes")

    with uow as tx:
        stored = jobs_store.list_for_run(tx, "run1")[0]

    assert stored.file_sha256 == "sha-of-actual-bytes"


# --- gate: startup reclaim after kill -9 -------------------------------------


RECLAIM_CHILD = """
import sys, time
sys.path.insert(0, {repo!r})
from screener.storage.connection import connect
from screener.storage.uow import UnitOfWork
from screener.storage import jobs_store

conn = connect(__import__("pathlib").Path({db!r}))
uow = UnitOfWork(conn)
with uow as tx:
    job = jobs_store.claim_next(tx, {worker!r})
print(job.id, flush=True)
time.sleep(60)
"""


def test_startup_reclaim_recovers_jobs_from_a_killed_worker(db: Path, uow: UnitOfWork) -> None:
    """The gate, with a real SIGKILL against a real process.

    A worker dying mid-job is not hypothetical — it is every deploy, every OOM,
    every power cut. The failure is silent: jobs stay `claimed` forever, the run
    never completes, and nobody is told.

    Reclaim is exact and clock-free: on startup, anything still claimed by *this*
    worker_id is from a previous life, because this process has claimed nothing
    yet. No lease arithmetic, which matters on an air-gapped box with no NTP.
    """
    seed_run(uow)
    add_jobs(uow, "run1", 3)

    script = RECLAIM_CHILD.format(repo=str(Path.cwd()), db=str(db), worker=WORKER)
    child = subprocess.Popen(  # noqa: S603 — fixed argv, test-local script
        [sys.executable, "-c", script],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        claimed_id = int(child.stdout.readline().strip())
    finally:
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=10)

    assert child.returncode == -signal.SIGKILL

    with uow as tx:
        assert jobs_store.progress(tx, "run1").claimed == 1  # orphaned, invisible

    with uow as tx:
        recovered = jobs_store.reclaim_orphaned(tx, WORKER)

    assert recovered == 1
    with uow as tx:
        assert jobs_store.progress(tx, "run1").pending == 3
        reclaimed = next(j for j in jobs_store.list_for_run(tx, "run1") if j.id == claimed_id)
        # Not reset: the previous life may have died *because of* this file, and
        # a crash loop that resets its own counter never reaches the cap.
        assert reclaimed.attempts == 1


def test_reclaim_leaves_other_workers_jobs_alone(uow: UnitOfWork) -> None:
    """Startup reclaim is scoped to this worker's own id, by construction."""
    seed_run(uow)
    add_jobs(uow, "run1", 2)
    with uow as tx:
        jobs_store.claim_next(tx, "worker-2")

    with uow as tx:
        assert jobs_store.reclaim_orphaned(tx, WORKER) == 0
        assert jobs_store.progress(tx, "run1").claimed == 1


def test_the_default_worker_id_is_unique_per_process() -> None:
    """The precondition the test above depends on.

    Reclaim being scoped to "this worker's own id" is only a safety property
    while that id is unique. A shared constant default meant two workers each
    reset the other's *in-flight* jobs at startup and then judged the same
    resume — the scoping test would still pass, because both processes were
    telling the truth about an id they wrongly shared.
    """
    from config.settings import Settings

    assert Settings().worker_id != "worker-1", "a constant default is the bug"
    assert str(os.getpid()) in Settings().worker_id


def test_heartbeat_sequence_advances(uow: UnitOfWork) -> None:
    """A monotonic counter, not a timestamp — immune to a clock step (16.5)."""
    seed_run(uow)
    add_jobs(uow, "run1", 1)
    with uow as tx:
        job = jobs_store.claim_next(tx, WORKER)
        assert job is not None
        before = jobs_store.stalled_candidates(tx, "worker-2")[0][1]
        jobs_store.heartbeat(tx, job.id, WORKER)
        after = jobs_store.stalled_candidates(tx, "worker-2")[0][1]

    assert after == before + 1


def test_a_worker_that_heartbeats_keeps_its_job(uow: UnitOfWork) -> None:
    """The multi-worker reclaim check, unused today but wrong-answer-proof.

    The sequence comparison is inside the UPDATE, so a heartbeat landing between
    observation and reclaim wins the race.
    """
    seed_run(uow)
    add_jobs(uow, "run1", 1)
    with uow as tx:
        job = jobs_store.claim_next(tx, "worker-2")
        assert job is not None
        observed = jobs_store.stalled_candidates(tx, WORKER)[0][1]
        jobs_store.heartbeat(tx, job.id, "worker-2")
        assert jobs_store.reclaim_if_unchanged(tx, job.id, observed) is False


def test_a_silent_worker_is_reclaimed(uow: UnitOfWork) -> None:
    seed_run(uow)
    add_jobs(uow, "run1", 1)
    with uow as tx:
        job = jobs_store.claim_next(tx, "worker-2")
        assert job is not None
        observed = jobs_store.stalled_candidates(tx, WORKER)[0][1]
        assert jobs_store.reclaim_if_unchanged(tx, job.id, observed) is True


# --- gate: run status transitions (16.4) ------------------------------------


def test_a_run_is_complete_only_when_nothing_is_in_flight(uow: UnitOfWork) -> None:
    seed_run(uow)
    add_jobs(uow, "run1", 2)

    with uow as tx:
        assert jobs_store.progress(tx, "run1").is_complete is False
        job = jobs_store.claim_next(tx, WORKER)
        assert job is not None
        jobs_store.complete(tx, job.id)
        # One done, one pending — not complete.
        assert jobs_store.progress(tx, "run1").is_complete is False

    with uow as tx:
        job = jobs_store.claim_next(tx, WORKER)
        assert job is not None
        jobs_store.complete(tx, job.id)
        assert jobs_store.progress(tx, "run1").is_complete is True


def test_a_run_with_failures_still_completes(uow: UnitOfWork) -> None:
    """Failed jobs are finished work. A run that never completes because one CV
    was corrupt would leave the other 999 results unreachable."""
    seed_run(uow)
    add_jobs(uow, "run1", 2)

    with uow as tx:
        first = jobs_store.claim_next(tx, WORKER)
        second = jobs_store.claim_next(tx, WORKER)
        assert first is not None and second is not None
        jobs_store.complete(tx, first.id)
        jobs_store.fail(tx, second.id, "corrupt", retryable=False)
        progress = jobs_store.progress(tx, "run1")

    assert progress.is_complete is True
    assert progress.done == 1
    assert progress.failed == 1


def test_an_empty_run_is_not_reported_complete(uow: UnitOfWork) -> None:
    """Zero jobs means the folder was empty or the snapshot never ran.

    Reporting that as "completed" would show a reviewer an empty result set with
    no indication anything was wrong.
    """
    seed_run(uow)
    with uow as tx:
        assert jobs_store.progress(tx, "run1").is_complete is False


def test_aborting_a_run_returns_in_flight_jobs_to_pending(uow: UnitOfWork) -> None:
    """Aborted runs are resumable (16.4)."""
    seed_run(uow)
    add_jobs(uow, "run1", 3)
    with uow as tx:
        jobs_store.claim_next(tx, WORKER)

    with uow as tx:
        assert jobs_store.abort_run_jobs(tx, "run1") == 1
        assert jobs_store.progress(tx, "run1").pending == 3


def test_count_unscreened_ignores_jobs_from_an_aborted_run(uow: UnitOfWork) -> None:
    """Regression. Aborted runs keep their jobs so they stay resumable (16.4),
    but nobody is ever going to resume most of them — an aborted test run over a
    two-file folder used to read as "2 more waiting to be screened" forever, on
    a dashboard where `runs_in_progress` correctly showed zero.
    """
    seed_run(uow, "run1")
    add_jobs(uow, "run1", 2)
    with uow as tx:
        assert jobs_store.count_unscreened(tx) == 2
        runs_store.set_status(tx, "run1", "aborted")

    with uow as tx:
        assert jobs_store.count_unscreened(tx) == 0


def test_queue_depth_counts_only_work_served_earlier(uow: UnitOfWork) -> None:
    seed_run(uow, "first", position_id="p1", reference="A", created_at=minutes_ago(60))
    seed_run(uow, "second", position_id="p2", reference="B", created_at=minutes_ago(30))
    add_jobs(uow, "first", 4, prefix="f")
    add_jobs(uow, "second", 2, prefix="s")

    with uow as tx:
        assert jobs_store.queue_depth_ahead(tx, "second") == 4
        assert jobs_store.queue_depth_ahead(tx, "first") == 0


# --- gate: two-phase execution (17.4) and phase-2 idempotence (12.9) ---------


def save_candidate(uow: UnitOfWork, run_id: str, sha: str, *, scoreable: bool = True) -> int:
    """A judged candidate, so phase 2 has something to claim."""
    from screener.models import Candidate
    from screener.ports import CacheKey
    from screener.storage import results_store

    with uow as tx:
        return results_store.save(
            tx,
            run_id,
            Candidate(
                run_id=run_id,
                filename=f"{sha}.pdf",
                file_sha256=sha,
                sent_text="Senior Backend Engineer, 2019-2024.",
                score=7.8 if scoreable else None,
                band="A" if scoreable else None,
                must_haves_met=scoreable,
                scoreable=scoreable,
            ),
            CacheKey(
                file_sha256=sha,
                position_id="p1",
                rubric_hash="r" * 64,
                judge_digest="sha256:aaa",
                prompt_hash="p" * 64,
                redaction_on=True,
                num_ctx=8192,
                app_version="v0.1.0",
            ),
        )


def test_a_verify_job_does_not_collide_with_the_judge_job_for_the_same_file(
    uow: UnitOfWork,
) -> None:
    """`UNIQUE(run_id, phase, file_path)`. Without the phase, phase 2 enqueues nothing.

    `INSERT OR IGNORE` makes that failure completely silent: the insert is
    ignored, the count comes back zero, and the run advances to `done` having
    verified nobody.
    """
    seed_run(uow)
    add_jobs(uow, "run1", 1)
    save_candidate(uow, "run1", "sha-a")

    with uow as tx:
        enqueued = jobs_store.enqueue_verify_jobs(tx, "run1")

    assert enqueued == 1


def test_only_scoreable_candidates_are_verified(uow: UnitOfWork) -> None:
    """An unscoreable candidate is already going to a human for a stronger reason.

    Spending ~5 s of GPU per resume to confirm it would be the review queue
    paying for work that changes nothing.
    """
    seed_run(uow)
    add_jobs(uow, "run1", 2)
    save_candidate(uow, "run1", "sha-ok", scoreable=True)
    save_candidate(uow, "run1", "sha-bad", scoreable=False)

    with uow as tx:
        assert jobs_store.enqueue_verify_jobs(tx, "run1") == 1


def test_resuming_mid_verification_re_verifies_nothing(uow: UnitOfWork) -> None:
    """**The gate.** 12.9: the phase-2 claim excludes candidates already done.

    Without it a run resumed after a crash re-runs the verifier over every
    candidate it had already checked — hours of GPU, and a non-deterministic
    second opinion re-rolled over settled candidates, which can change who is in
    the review queue on what was supposed to be a resumption.
    """
    seed_run(uow)
    add_jobs(uow, "run1", 2)
    first = save_candidate(uow, "run1", "sha-a")
    save_candidate(uow, "run1", "sha-b")
    with uow as tx:
        jobs_store.enqueue_verify_jobs(tx, "run1")
        # `sha-a` was verified before the crash.
        tx.execute("UPDATE candidates SET verification_status = 'done' WHERE id = ?", (first,))

    claimed = []
    for _ in range(3):
        with uow as tx:
            job = jobs_store.claim_next(tx, WORKER, phase="verify")
        if job is None:
            break
        claimed.append(job)
        with uow as tx:
            jobs_store.complete(tx, job.id)

    assert len(claimed) == 1, "a verified candidate was claimed again"
    assert claimed[0].candidate_id != first


def test_close_verified_jobs_retires_a_job_claim_next_will_never_offer_again(
    uow: UnitOfWork,
) -> None:
    """A cache-hit candidate arrives already `verification_status='done'`.

    `enqueue_verify_jobs` queues phase 2 for it anyway (it only checks
    `scoreable`), and `claim_next` then refuses it forever (12.9) — a job stuck
    `pending` with nothing that will ever claim it. Without a way to close it,
    the phase never drains and the run never reaches `completed`.
    """
    seed_run(uow)
    add_jobs(uow, "run1", 1)
    cached = save_candidate(uow, "run1", "sha-a")
    with uow as tx:
        tx.execute("UPDATE candidates SET verification_status = 'done' WHERE id = ?", (cached,))
        jobs_store.enqueue_verify_jobs(tx, "run1")
        assert jobs_store.no_pending(tx, "run1", "verify") is False

        closed = jobs_store.close_verified_jobs(tx, "run1")
        assert closed == 1
        assert jobs_store.no_pending(tx, "run1", "verify") is True


def test_close_verified_jobs_leaves_a_claimed_job_alone(uow: UnitOfWork) -> None:
    """A job mid-flight is not orphaned — `complete_verify_job` will close it.

    Force-closing a `claimed` row here would race the worker that owns it: if
    that worker's `save_verification` lands between this check and its own
    `complete_verify_job`, forcing status to `done` a second time is harmless,
    but doing it *before* the worker has finished is not — the job would read
    complete while a resume is still being judged against it.
    """
    seed_run(uow)
    add_jobs(uow, "run1", 1)
    cached = save_candidate(uow, "run1", "sha-a")
    with uow as tx:
        jobs_store.enqueue_verify_jobs(tx, "run1")
        job = jobs_store.claim_next(tx, WORKER, phase="verify")
        assert job is not None
        # The worker's `save_verification` landed; `complete_verify_job` has not.
        tx.execute("UPDATE candidates SET verification_status = 'done' WHERE id = ?", (cached,))

        assert jobs_store.close_verified_jobs(tx, "run1") == 0
        assert jobs_store.no_pending(tx, "run1", "verify") is False


def test_enqueueing_twice_adds_nothing(uow: UnitOfWork) -> None:
    """A worker that dies between enqueueing and advancing the phase must not double up."""
    seed_run(uow)
    add_jobs(uow, "run1", 1)
    save_candidate(uow, "run1", "sha-a")

    with uow as tx:
        assert jobs_store.enqueue_verify_jobs(tx, "run1") == 1
        assert jobs_store.enqueue_verify_jobs(tx, "run1") == 0


def test_a_phase_claims_only_its_own_work(uow: UnitOfWork) -> None:
    """Phase 1 must not hand a verify job to a worker holding the judge model."""
    seed_run(uow)
    add_jobs(uow, "run1", 1)
    save_candidate(uow, "run1", "sha-a")
    with uow as tx:
        jobs_store.enqueue_verify_jobs(tx, "run1")

    with uow as tx:
        judged = jobs_store.claim_next(tx, WORKER, phase="judge")
    assert judged is not None and judged.phase == "judge"

    with uow as tx:
        verify = jobs_store.claim_next(tx, WORKER, phase="verify")
    assert verify is not None and verify.phase == "verify"
    assert verify.candidate_id is not None


def test_a_phase_is_drained_only_when_nothing_is_in_flight(uow: UnitOfWork) -> None:
    """Advancing under a claimed job would unload the model it is mid-call against."""
    seed_run(uow)
    add_jobs(uow, "run1", 1)

    with uow as tx:
        assert jobs_store.no_pending(tx, "run1", "judge") is False
        job = jobs_store.claim_next(tx, WORKER, phase="judge")
        assert job is not None
        assert jobs_store.no_pending(tx, "run1", "judge") is False
        jobs_store.complete(tx, job.id)
        assert jobs_store.no_pending(tx, "run1", "judge") is True
