"""The work queue (spec 16.2, 16.3, 16.5). Satisfies ``ports.JobQueue``.

A table, not Celery or Redis. There is one worker; a second daemon and a network
service to replace a `WHERE status = 'pending'` would be cost without benefit,
and this module is the seam if that ever changes (22.2).

Three things here are subtler than they look.

**The claim is one statement.** `UPDATE ... WHERE id = (SELECT ...) RETURNING *`
runs atomically inside the transaction, so there is no window between choosing a
job and owning it. A SELECT-then-UPDATE would work today — one worker, and
`BEGIN IMMEDIATE` holds the write lock — but it would be a latent double-claim
the day a second worker starts, and that failure hands the same candidate to two
workers and writes the result twice.

**Recovery is clock-free.** Absolute lease expiry was rejected because an
air-gapped box has no NTP: a clock step either reclaims work that is still
running or strands work that died. On startup a worker instead reclaims jobs
`claimed` by *its own* worker_id — those are unambiguously from a previous life,
because this process has claimed nothing yet. No arithmetic on time at all.

**Scheduling is strict FIFO.** Round-robin across runs was rejected: ranking is
only meaningful over a complete run (10.6), so a reviewer holding 60% of their
results has nothing they can act on, and round-robin makes everyone late in
exchange for progress nobody can use. The fast lane went the same way (17.3) —
see `claim_next` for why.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import settings
from screener.models import now
from screener.ports import Job
from screener.storage.uow import Tx

# Runs a worker may draw work from. A job belonging to an aborted or failed run
# is left alone rather than deleted — aborted runs are resumable (16.4), and the
# rows are the record of what was attempted.
_ACTIVE_RUN_STATUSES = ("pending", "running")


@dataclass(frozen=True)
class FailedJob:
    """A file that exhausted its attempts and produced no candidate.

    `filename` rather than the full path: it is what a reviewer recognises, and
    it is what every other candidate-facing surface shows.
    """

    filename: str
    phase: str
    attempts: int
    last_error: str


@dataclass(frozen=True)
class RunProgress:
    """Counts behind `/runs/{id}/status` and the completion check."""

    pending: int
    claimed: int
    done: int
    failed: int

    @property
    def total(self) -> int:
        return self.pending + self.claimed + self.done + self.failed

    @property
    def finished(self) -> int:
        return self.done + self.failed

    @property
    def is_complete(self) -> bool:
        """No pending and nothing in flight (16.4)."""
        return self.total > 0 and self.pending == 0 and self.claimed == 0

    @property
    def eta_seconds(self) -> float:
        """Remaining work × measured per-resume time.

        A known four-hour wait is fine; an unknown one produces duplicate
        submissions and support tickets (16.3).
        """
        return (self.pending + self.claimed) * settings.seconds_per_resume


# --- snapshot ----------------------------------------------------------------


def snapshot_folder(tx: Tx, run_id: str, folder: Path) -> int:
    """Freeze the folder into jobs. Returns how many *new* rows were inserted.

    Deliberately a snapshot rather than a live view: a run is a defined set of
    candidates at a point in time, which is what makes the ranking meaningful and
    the result reproducible. Files added later need an explicit rescan — nothing
    is ever silently added mid-run.

    `ON CONFLICT DO NOTHING` against `UNIQUE(run_id, phase, file_path)` makes this
    idempotent, so rescan is the same call and inserts only what is new. The
    phase is part of that key: without it, the verify job for a resume collides
    with the judge job that produced the candidate, and phase 2 silently
    enqueues nothing.
    """
    timestamp = now().isoformat()
    inserted = 0

    for path in _eligible_files(folder):
        cursor = tx.execute(
            "INSERT INTO jobs (run_id, phase, file_path, status, created_at, "
            "updated_at) VALUES (?, 'judge', ?, 'pending', ?, ?) "
            "ON CONFLICT DO NOTHING",
            (run_id, str(path), timestamp, timestamp),
        )
        inserted += cursor.rowcount or 0

    return inserted


def enqueue_verify_jobs(tx: Tx, run_id: str) -> int:
    """Queue phase 2 for this run's scoreable candidates. Returns how many.

    **Scoreable only.** An unscoreable candidate is already going to a human for
    a stronger reason than anything the verifier could add, and spending ~5 s of
    GPU per resume to confirm it would be the review queue paying for work that
    changes nothing (10.4).

    Idempotent through the same UNIQUE key as the snapshot, so a worker that
    crashes between enqueueing and advancing the phase re-enqueues nothing.
    """
    timestamp = now().isoformat()
    cursor = tx.execute(
        "INSERT INTO jobs (run_id, phase, file_path, file_sha256, candidate_id, "
        "status, created_at, updated_at) "
        "SELECT c.run_id, 'verify', c.filename, c.file_sha256, c.id, 'pending', ?, ? "
        "FROM candidates c WHERE c.run_id = ? AND c.scoreable = TRUE "
        "ON CONFLICT DO NOTHING",
        (timestamp, timestamp, run_id),
    )
    return cursor.rowcount or 0


def _eligible_files(folder: Path) -> list[Path]:
    """Recursive walk, filtered by extension, symlinks not followed.

    Extension filtering here is a *scheduling* decision, not a security one — it
    keeps `.DS_Store` and stray notes out of the queue. `validate_file` (8.2)
    re-derives the real type from magic bytes and does not consult the extension
    at all, because a file's name is not evidence of its contents.

    Paths that resolve outside the folder are dropped rather than queued: a
    symlinked directory could otherwise pull an unrelated tree into a run.

    **Dot-prefixed files are skipped, extension notwithstanding.** macOS writes
    an AppleDouble sidecar (`._alice.pdf`) beside every file it copies onto an
    SMB or NFS share, and those carry the `.pdf` suffix that would otherwise
    admit them. They are not resumes: `validate_file` rejects them on magic
    bytes and each one becomes an unscoreable candidate a human has to clear.
    On a share written to from a Mac that doubles the review queue with junk.
    `resumes_store.list_folders` applies the same rule, so the count shown when
    a folder is picked is the count that gets queued.
    """
    if not folder.is_dir():
        return []

    root = folder.resolve()
    allowed = {ext.casefold() for ext in settings.allowed_extensions}
    found: list[Path] = []

    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or not path.is_file():
            continue
        if path.name.startswith("."):
            continue
        if path.suffix.casefold() not in allowed:
            continue
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            continue
        if not resolved.is_relative_to(root):
            continue
        found.append(resolved)

    return found


# --- claim / release ---------------------------------------------------------


def claim_next(tx: Tx, worker_id: str, phase: str = "judge") -> Job | None:
    """Take ownership of exactly one job in this phase, or return None.

    **FIFO by run creation, then job id.** The fast lane is gone (17.3): batch
    duration was never the constraint — recruiters currently screen by hand, and
    nobody notices 2.6 hours against 78 minutes. Reviewer time is the constraint,
    and shortest-job-first bought nothing against it while adding a starvation
    problem that then needed an aging rule to fix.

    **Phase 2 skips candidates already verified.** Without that clause a run
    resumed mid-verification re-verifies everything it had already checked, at
    ~5 s of GPU each (12.9) — and re-running a non-deterministic second opinion
    over settled candidates can change who is in the review queue on a rerun that
    was supposed to be a resumption.
    """
    timestamp = now().isoformat()
    placeholders = ",".join("?" for _ in _ACTIVE_RUN_STATUSES)
    # Phase 2 works from stored candidates, so the queue is filtered by their
    # state rather than by the job's alone.
    verify_join = "JOIN candidates c ON c.id = j.candidate_id " if phase == "verify" else ""
    verify_where = (
        "AND c.verification_status = 'pending' AND c.scoreable = TRUE " if phase == "verify" else ""
    )

    row = tx.execute(
        f"""
        UPDATE jobs SET
            status = 'claimed',
            claimed_by = ?,
            claimed_at = ?,
            heartbeat_at = ?,
            heartbeat_seq = heartbeat_seq + 1,
            attempts = attempts + 1,
            updated_at = ?
        WHERE id = (
            SELECT j.id FROM jobs j
            JOIN runs r ON r.id = j.run_id
            {verify_join}
            WHERE j.status = 'pending'
              AND j.phase = ?
              AND r.status IN ({placeholders})
              {verify_where}
            ORDER BY r.created_at, j.id
            LIMIT 1
        )
        RETURNING id, run_id, phase, file_path, file_sha256, candidate_id, attempts,
                  claimed_by, claimed_at
        """,  # noqa: S608 — fragments are module-local constants keyed on `phase`, never caller input
        (worker_id, timestamp, timestamp, timestamp, phase, *_ACTIVE_RUN_STATUSES),
    ).fetchone()

    return _to_job(row) if row else None


def no_pending(tx: Tx, run_id: str, phase: str) -> bool:
    """Is this phase drained? The condition for advancing to the next one.

    Counts `claimed` as well as `pending`: a job in flight on another worker is
    not finished, and advancing the phase underneath it would unload the model
    it is mid-call against.
    """
    row = tx.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE run_id = ? AND phase = ? "
        "AND status IN ('pending','claimed')",
        (run_id, phase),
    ).fetchone()
    return int(row["n"]) == 0


def heartbeat(tx: Tx, job_id: int, worker_id: str) -> None:
    """Advance the liveness counter.

    A monotonic sequence, not a timestamp. A reclaimer compares the sequence
    across two of its own poll cycles and acts only if it has not moved —
    relative, so a clock step cannot make live work look dead (16.5).
    """
    tx.execute(
        "UPDATE jobs SET heartbeat_seq = heartbeat_seq + 1, heartbeat_at = ?, updated_at = ? "
        "WHERE id = ? AND claimed_by = ?",
        (now().isoformat(), now().isoformat(), job_id, worker_id),
    )


def complete(tx: Tx, job_id: int, file_sha256: str | None = None) -> None:
    """Mark done. `file_sha256` is recorded at completion, not at scan time.

    A file replaced between snapshot and processing is hashed as what was
    actually read, so the cache key describes the bytes that were judged (16.2).
    """
    tx.execute(
        "UPDATE jobs SET status = 'done', file_sha256 = COALESCE(?, file_sha256), "
        "last_error = NULL, updated_at = ? WHERE id = ?",
        (file_sha256, now().isoformat(), job_id),
    )


def close_verified_jobs(tx: Tx, run_id: str) -> int:
    """Retire phase-2 jobs whose candidate is verified but the job never will be.

    Two paths leave a verify job `pending` forever with no worker able to claim
    it, because `claim_next` deliberately excludes any candidate whose
    `verification_status` is no longer `'pending'` (12.9):

    - A judge-phase cache hit re-stamps an already-verified candidate from an
      earlier run onto this one (`worker_loop._cached`), and `enqueue_verify_jobs`
      queues phase 2 for it anyway because it only checks `scoreable`.
    - A worker crashes between `save_verification` and `complete_verify_job`
      (`worker_loop._verify`): the candidate reads `done`, but `reclaim_orphaned`
      only knows how to reopen the *job*, which then can never be reclaimed by
      `claim_next` either.

    Without this, `no_pending` never sees the phase drain, the run never leaves
    `verify`, and it never reaches `completed` — stuck exactly where a reviewer
    who already decided every candidate finds it.

    `claimed` jobs are left alone: one may be mid-flight on this exact
    candidate, and `complete_verify_job` will close it normally in a moment.
    """
    cursor = tx.execute(
        "UPDATE jobs SET status = 'done', updated_at = ? "
        "WHERE run_id = ? AND phase = 'verify' AND status = 'pending' "
        "AND candidate_id IN ("
        "  SELECT id FROM candidates WHERE run_id = ? AND verification_status != 'pending'"
        ")",
        (now().isoformat(), run_id, run_id),
    )
    return cursor.rowcount or 0


def fail(tx: Tx, job_id: int, error: str, retryable: bool) -> None:
    """Return a job to the queue, or retire it.

    `attempts` was already incremented at claim time, so a job that keeps killing
    the parser cannot loop forever: past `job_max_attempts` it goes to `failed`
    with its last error, whatever the caller says about retryability.

    Counting at claim rather than at failure is deliberate — a worker killed
    mid-job never reaches this function, and an attempt that dies silently still
    has to count against the cap or a poison file retries indefinitely.
    """
    row = tx.execute("SELECT attempts FROM jobs WHERE id = ?", (job_id,)).fetchone()
    attempts = int(row["attempts"]) if row else 0
    exhausted = attempts >= settings.job_max_attempts

    tx.execute(
        "UPDATE jobs SET status = ?, last_error = ?, claimed_by = NULL, claimed_at = NULL, "
        "updated_at = ? WHERE id = ?",
        (
            "pending" if retryable and not exhausted else "failed",
            error[:2000],
            now().isoformat(),
            job_id,
        ),
    )


def release(tx: Tx, job_id: int) -> None:
    """Hand a job back untouched, without counting a failure.

    Used when the worker is stopping cleanly rather than when the job went wrong.
    `attempts` is decremented because the claim incremented it and no attempt was
    actually made — otherwise a few clean restarts would exhaust the cap on a
    file nothing is wrong with.
    """
    tx.execute(
        "UPDATE jobs SET status = 'pending', claimed_by = NULL, claimed_at = NULL, "
        "attempts = MAX(attempts - 1, 0), updated_at = ? WHERE id = ?",
        (now().isoformat(), job_id),
    )


# --- crash recovery ----------------------------------------------------------


def reclaim_orphaned(tx: Tx, worker_id: str) -> int:
    """Startup reclaim. Returns how many jobs were recovered (16.5).

    Anything still `claimed` by *this* worker_id is from a previous life — this
    process has claimed nothing yet, so there is no ambiguity and no clock
    involved. This is the primary and, for a single worker, the complete recovery
    mechanism.

    `attempts` is deliberately **not** decremented. The previous life may have
    died *because of* this file, and a crash loop that resets its own counter
    never hits the cap.
    """
    cursor = tx.execute(
        "UPDATE jobs SET status = 'pending', claimed_by = NULL, claimed_at = NULL, "
        "updated_at = ? WHERE status = 'claimed' AND claimed_by = ?",
        (now().isoformat(), worker_id),
    )
    return cursor.rowcount or 0


def reclaim_expired(tx: Tx, worker_id: str, cutoff: str) -> int:
    """Reclaim jobs claimed by *another* worker before `cutoff` (16.5).

    The lease sweep — the standard visibility-timeout pattern, and the
    last-resort half of recovery. `reclaim_orphaned` handles a crash by name;
    this handles the case that has no name to match on, where a job was claimed
    as `old-hostname` and nothing will ever answer to that again.

    **`claimed_by != ?` is a safety property, not a filter.** It makes the
    statement structurally incapable of reclaiming the job the calling worker is
    holding right now, however long that job has been running and whatever the
    clock has done. Combined with the startup reclaim — which has already cleared
    this worker's own strays before the loop begins — anything wearing this
    worker's name is live work, and this cannot touch it.

    `cutoff` is passed in rather than computed here so the caller owns the one
    wall-clock reading involved, and tests can hand it an exact boundary.

    `attempts` is deliberately **not** reset, for the same reason as
    `reclaim_orphaned`: the previous life may have died *because of* this file,
    and a crash loop that resets its own counter never hits the cap.
    """
    cursor = tx.execute(
        "UPDATE jobs SET status = 'pending', claimed_by = NULL, claimed_at = NULL, "
        "updated_at = ? WHERE status = 'claimed' AND claimed_by != ? AND claimed_at < ?",
        (now().isoformat(), worker_id, cutoff),
    )
    return cursor.rowcount or 0


def stalled_candidates(tx: Tx, worker_id: str) -> list[tuple[int, int]]:
    """`(job_id, heartbeat_seq)` for jobs claimed by *other* workers.

    The multi-worker half of 16.5, unused while there is one worker. A reclaimer
    samples this twice across its own poll cycles and reclaims only where the
    sequence has not advanced — relative progress, never wall-clock age.

    **Do not connect this without first making the worker heartbeat.** The
    sample-twice test assumes `heartbeat_seq` advances while a job is being
    worked on. It does not: nothing calls `heartbeat` today, so the sequence is
    bumped once at claim and then stays put for the whole job. A healthy worker
    thirty seconds into an inference call is therefore indistinguishable from a
    dead one, and a reclaimer would hand the resume it is holding to a second
    worker — duplicate inference, two results for one candidate, and a
    `UNIQUE(file_sha256, run_id)` violation on save. That is a worse failure than
    the stranding this is meant to prevent.

    The precondition is `Worker` calling `service.heartbeat` on a timer *during*
    `_process`, which needs a thread or a callback into the pipeline — neither of
    which exists, and 16 is deliberate about the worker being single-threaded.
    Until then, `reclaim_orphaned` on a stable `worker_id` is the whole recovery
    mechanism, and it is sufficient for the one-worker deployment.
    """
    rows = tx.execute(
        "SELECT id, heartbeat_seq FROM jobs WHERE status = 'claimed' AND claimed_by != ?",
        (worker_id,),
    ).fetchall()
    return [(int(row["id"]), int(row["heartbeat_seq"])) for row in rows]


def reclaim_if_unchanged(tx: Tx, job_id: int, observed_seq: int) -> bool:
    """Reclaim a job whose heartbeat has not moved since `observed_seq`.

    The sequence check is part of the UPDATE, so a worker that heartbeats between
    the observation and this call keeps its job.
    """
    cursor = tx.execute(
        "UPDATE jobs SET status = 'pending', claimed_by = NULL, claimed_at = NULL, "
        "updated_at = ? WHERE id = ? AND status = 'claimed' AND heartbeat_seq = ?",
        (now().isoformat(), job_id, observed_seq),
    )
    return bool(cursor.rowcount)


# --- progress ----------------------------------------------------------------


def progress(tx: Tx, run_id: str, phase: str = "judge") -> RunProgress:
    """Job counts for one phase of a run.

    **Phase-scoped, defaulting to `judge`.** Counting both phases together would
    double every total the moment verification was enabled — a 6-file run
    reporting 12 — and would make "340 of 1000" mean nothing in particular. The
    judge phase has exactly one job per candidate, which is what everyone means
    by the size of a run.
    """
    rows = tx.execute(
        "SELECT status, COUNT(*) AS n FROM jobs WHERE run_id = ? AND phase = ? GROUP BY status",
        (run_id, phase),
    ).fetchall()
    counts = {row["status"]: int(row["n"]) for row in rows}
    return RunProgress(
        pending=counts.get("pending", 0),
        claimed=counts.get("claimed", 0),
        done=counts.get("done", 0),
        failed=counts.get("failed", 0),
    )


def count_unscreened(tx: Tx) -> int:
    """Snapshotted files not yet judged, across every **active** run.

    Judge phase only, for the reason `progress` gives: one job per candidate is
    what anyone means by "files waiting". It is what makes the applications
    figure legible — a total that is not moving means something different when
    there is nothing left in the queue.

    **Scoped to `_ACTIVE_RUN_STATUSES`, matching `claim_next` and
    `queue_depth_ahead`.** An aborted run keeps its jobs — they are how it stays
    resumable (16.4) — so without this an aborted test run over a two-file folder
    reads as "2 more waiting to be screened" forever, on a dashboard nobody is
    ever going to resume it from.
    """
    placeholders = ",".join("?" for _ in _ACTIVE_RUN_STATUSES)
    row = tx.execute(
        f"""
        SELECT COUNT(*) AS n FROM jobs j
        JOIN runs r ON r.id = j.run_id
        WHERE j.phase = 'judge' AND j.status IN ('pending','claimed')
          AND r.status IN ({placeholders})
        """,  # noqa: S608 — fragment is a module-local constant, never caller input
        _ACTIVE_RUN_STATUSES,
    ).fetchone()
    return int(row["n"])


def failed_jobs(tx: Tx, run_id: str) -> list[FailedJob]:
    """Files that exhausted `job_max_attempts` and were never screened (16.5).

    **These are not candidates and never become one.** `judge_one` turns document
    problems into a flagged `Candidate` with `scoreable=False`, so reaching
    `failed` means infrastructure — the model was unreachable, the database
    errored, or there is a bug. Nothing about that is a statement on the
    applicant, and nothing in the results tables records that they existed.

    Read by `run_status` and by `sign_off_run`, which refuses while any of these
    remain. Without it a run completes and signs off with applicants missing from
    it entirely, which is the one outcome the sign-off gate exists to prevent —
    arriving through the path the gate does not otherwise inspect.
    """
    rows = tx.execute(
        "SELECT file_path, phase, attempts, last_error FROM jobs "
        "WHERE run_id = ? AND status = 'failed' ORDER BY file_path",
        (run_id,),
    ).fetchall()
    return [
        FailedJob(
            filename=Path(row["file_path"]).name,
            phase=row["phase"],
            attempts=int(row["attempts"]),
            last_error=row["last_error"] or "",
        )
        for row in rows
    ]


def queue_depth_ahead(tx: Tx, run_id: str) -> int:
    """Unfinished jobs in runs that will be served before this one.

    Feeds the ETA. Uses the same ordering as `claim_next` so the number a
    reviewer sees matches the order they will actually be served in.
    """
    row = tx.execute(
        """
        SELECT COUNT(*) AS n FROM jobs j
        JOIN runs r ON r.id = j.run_id
        WHERE j.status IN ('pending','claimed')
          AND r.status IN ('pending','running')
          AND r.created_at < (SELECT created_at FROM runs WHERE id = ?)
        """,
        (run_id,),
    ).fetchone()
    return int(row["n"]) if row else 0


def list_for_run(tx: Tx, run_id: str, status: str | None = None) -> list[Job]:
    if status is None:
        rows = tx.execute(
            "SELECT id, run_id, file_path, file_sha256, attempts, claimed_by, claimed_at "
            "FROM jobs WHERE run_id = ? ORDER BY id",
            (run_id,),
        ).fetchall()
    else:
        rows = tx.execute(
            "SELECT id, run_id, file_path, file_sha256, attempts, claimed_by, claimed_at "
            "FROM jobs WHERE run_id = ? AND status = ? ORDER BY id",
            (run_id, status),
        ).fetchall()
    return [_to_job(row) for row in rows]


def abort_run_jobs(tx: Tx, run_id: str) -> int:
    """Return in-flight jobs to pending so an aborted run stays resumable (16.4)."""
    cursor = tx.execute(
        "UPDATE jobs SET status = 'pending', claimed_by = NULL, claimed_at = NULL, "
        "updated_at = ? WHERE run_id = ? AND status = 'claimed'",
        (now().isoformat(), run_id),
    )
    return cursor.rowcount or 0


def _to_job(row: Any) -> Job:  # noqa: ANN401 — sqlite3.Row
    keys = row.keys()
    return Job(
        id=int(row["id"]),
        run_id=row["run_id"],
        phase=row["phase"] if "phase" in keys else "judge",
        file_path=Path(row["file_path"]),
        file_sha256=row["file_sha256"],
        candidate_id=row["candidate_id"] if "candidate_id" in keys else None,
        attempts=int(row["attempts"]),
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
    )
