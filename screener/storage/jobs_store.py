"""The work queue (spec §16.2, §16.3, §16.5). Satisfies ``ports.JobQueue``.

A table, not Celery or Redis. There is one worker; a second daemon and a network
service to replace a `WHERE status = 'pending'` would be cost without benefit,
and this module is the seam if that ever changes (§22.2).

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

**Scheduling is FIFO with a fast lane.** Round-robin across runs was rejected:
ranking is only meaningful over a complete run (§10.6), so a reviewer holding 60%
of their results has nothing they can act on. Round-robin makes everyone late in
exchange for progress nobody can use.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from config.settings import settings
from screener.models import now
from screener.ports import Job
from screener.storage.uow import Tx

# Runs a worker may draw work from. A job belonging to an aborted or failed run
# is left alone rather than deleted — aborted runs are resumable (§16.4), and the
# rows are the record of what was attempted.
_ACTIVE_RUN_STATUSES = ("pending", "running")


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
        """No pending and nothing in flight (§16.4)."""
        return self.total > 0 and self.pending == 0 and self.claimed == 0

    @property
    def eta_seconds(self) -> float:
        """Remaining work × measured per-resume time.

        A known four-hour wait is fine; an unknown one produces duplicate
        submissions and support tickets (§16.3).
        """
        return (self.pending + self.claimed) * settings.seconds_per_resume


# --- snapshot ----------------------------------------------------------------


def snapshot_folder(tx: Tx, run_id: str, folder: Path) -> int:
    """Freeze the folder into jobs. Returns how many *new* rows were inserted.

    Deliberately a snapshot rather than a live view: a run is a defined set of
    candidates at a point in time, which is what makes the ranking meaningful and
    the result reproducible. Files added later need an explicit rescan — nothing
    is ever silently added mid-run.

    `INSERT OR IGNORE` against `UNIQUE(run_id, file_path)` makes this idempotent,
    so rescan is the same call and inserts only what is new.
    """
    timestamp = now().isoformat()
    inserted = 0

    for path in _eligible_files(folder):
        cursor = tx.execute(
            "INSERT OR IGNORE INTO jobs (run_id, file_path, status, created_at, updated_at) "
            "VALUES (?, ?, 'pending', ?, ?)",
            (run_id, str(path), timestamp, timestamp),
        )
        inserted += cursor.rowcount or 0

    return inserted


def _eligible_files(folder: Path) -> list[Path]:
    """Recursive walk, filtered by extension, symlinks not followed.

    Extension filtering here is a *scheduling* decision, not a security one — it
    keeps `.DS_Store` and stray notes out of the queue. `validate_file` (§8.2)
    re-derives the real type from magic bytes and does not consult the extension
    at all, because a file's name is not evidence of its contents.

    Paths that resolve outside the folder are dropped rather than queued: a
    symlinked directory could otherwise pull an unrelated tree into a run.
    """
    if not folder.is_dir():
        return []

    root = folder.resolve()
    allowed = {ext.casefold() for ext in settings.allowed_extensions}
    found: list[Path] = []

    for path in sorted(folder.rglob("*")):
        if path.is_symlink() or not path.is_file():
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


def claim_next(tx: Tx, worker_id: str) -> Job | None:
    """Take ownership of exactly one job, or return None.

    Selection order implements §16.3: runs under `fast_lane_max_files` first
    (shortest-job-first where it is cheap, so a small specialist role does not
    sit behind two 1,000-CV mass postings), then FIFO by run creation, then job
    id within a run.

    Aging is folded into the same ORDER BY: a large run whose wait has exceeded
    `run_aging_hours` is promoted into the fast lane, so a stream of small runs
    cannot starve it indefinitely.
    """
    timestamp = now().isoformat()
    placeholders = ",".join("?" for _ in _ACTIVE_RUN_STATUSES)

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
            JOIN (
                SELECT run_id, COUNT(*) AS n FROM jobs WHERE status = 'pending' GROUP BY run_id
            ) q ON q.run_id = j.run_id
            WHERE j.status = 'pending'
              AND r.status IN ({placeholders})
            ORDER BY
                CASE WHEN q.n <= ? THEN 0
                     WHEN (julianday(?) - julianday(r.created_at)) * 24.0 >= ? THEN 0
                     ELSE 1 END,
                r.created_at,
                j.id
            LIMIT 1
        )
        RETURNING id, run_id, file_path, file_sha256, attempts, claimed_by, claimed_at
        """,  # noqa: S608 — placeholders are generated from a module constant, not input
        (
            worker_id,
            timestamp,
            timestamp,
            timestamp,
            *_ACTIVE_RUN_STATUSES,
            settings.fast_lane_max_files,
            timestamp,
            settings.run_aging_hours,
        ),
    ).fetchone()

    return _to_job(row) if row else None


def heartbeat(tx: Tx, job_id: int, worker_id: str) -> None:
    """Advance the liveness counter.

    A monotonic sequence, not a timestamp. A reclaimer compares the sequence
    across two of its own poll cycles and acts only if it has not moved —
    relative, so a clock step cannot make live work look dead (§16.5).
    """
    tx.execute(
        "UPDATE jobs SET heartbeat_seq = heartbeat_seq + 1, heartbeat_at = ?, updated_at = ? "
        "WHERE id = ? AND claimed_by = ?",
        (now().isoformat(), now().isoformat(), job_id, worker_id),
    )


def complete(tx: Tx, job_id: int, file_sha256: str | None = None) -> None:
    """Mark done. `file_sha256` is recorded at completion, not at scan time.

    A file replaced between snapshot and processing is hashed as what was
    actually read, so the cache key describes the bytes that were judged (§16.2).
    """
    tx.execute(
        "UPDATE jobs SET status = 'done', file_sha256 = COALESCE(?, file_sha256), "
        "last_error = NULL, updated_at = ? WHERE id = ?",
        (file_sha256, now().isoformat(), job_id),
    )


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
    """Startup reclaim. Returns how many jobs were recovered (§16.5).

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


def stalled_candidates(tx: Tx, worker_id: str) -> list[tuple[int, int]]:
    """`(job_id, heartbeat_seq)` for jobs claimed by *other* workers.

    The multi-worker half of §16.5, unused while there is one worker. A reclaimer
    samples this twice across its own poll cycles and reclaims only where the
    sequence has not advanced — relative progress, never wall-clock age.
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


def progress(tx: Tx, run_id: str) -> RunProgress:
    rows = tx.execute(
        "SELECT status, COUNT(*) AS n FROM jobs WHERE run_id = ? GROUP BY status",
        (run_id,),
    ).fetchall()
    counts = {row["status"]: int(row["n"]) for row in rows}
    return RunProgress(
        pending=counts.get("pending", 0),
        claimed=counts.get("claimed", 0),
        done=counts.get("done", 0),
        failed=counts.get("failed", 0),
    )


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
    """Return in-flight jobs to pending so an aborted run stays resumable (§16.4)."""
    cursor = tx.execute(
        "UPDATE jobs SET status = 'pending', claimed_by = NULL, claimed_at = NULL, "
        "updated_at = ? WHERE run_id = ? AND status = 'claimed'",
        (now().isoformat(), run_id),
    )
    return cursor.rowcount or 0


def _to_job(row: Any) -> Job:  # noqa: ANN401 — sqlite3.Row
    return Job(
        id=int(row["id"]),
        run_id=row["run_id"],
        file_path=Path(row["file_path"]),
        file_sha256=row["file_sha256"],
        attempts=int(row["attempts"]),
        claimed_by=row["claimed_by"],
        claimed_at=row["claimed_at"],
    )
