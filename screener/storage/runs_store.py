"""Runs — the reproducibility record (spec 12.4, 16.4).

A run row is not bookkeeping. It freezes every input that determined the output:
model name and digest, prompt hash, `num_ctx`, `num_predict`, seed,
`redaction_on`, `app_version`. Months later, "why did this candidate score 6.2"
is answerable only if those values were captured at the time — the model tag will
have moved, the prompt will have been edited, and neither leaves a trace anywhere
else.

Status transitions follow 16.4: pending → running → completed, with aborted
(resumable) and failed (needs investigation, not blind resumption) as terminal
alternatives.
"""

from typing import Any

from screener.models import Run, now
from screener.storage.uow import Tx

TERMINAL = frozenset({"completed", "failed", "aborted"})


def create(
    tx: Tx,
    *,
    run_id: str,
    position_id: str,
    rubric_id: str,
    folder: str,
    created_by: str,
    judge_model: str,
    judge_digest: str,
    prompt_hash: str,
    redaction_on: bool,
    num_ctx: int,
    num_predict: int,
    seed: int,
    app_version: str,
    verifier_model: str | None = None,
    verifier_digest: str | None = None,
    verification_enabled: bool = True,
    file_count: int = 0,
    status: str = "pending",
) -> None:
    tx.execute(
        "INSERT INTO runs (id, position_id, rubric_id, folder, judge_model, judge_digest, "
        "verifier_model, verifier_digest, verification_enabled, prompt_hash, redaction_on, "
        "num_ctx, num_predict, seed, app_version, file_count, status, "
        "created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            run_id,
            position_id,
            rubric_id,
            folder,
            judge_model,
            judge_digest,
            verifier_model,
            verifier_digest,
            int(verification_enabled),
            prompt_hash,
            int(redaction_on),
            num_ctx,
            num_predict,
            seed,
            app_version,
            file_count,
            status,
            created_by,
            now().isoformat(),
        ),
    )


def set_phase(tx: Tx, run_id: str, phase: str) -> None:
    """Advance a run between the judge and verify passes (17.4).

    Separate from `set_status` because the two answer different questions: the
    status is whether the run is alive, the phase is which model is loaded and
    which queue is being drained. Collapsing them would make "running" ambiguous
    at exactly the moment a reviewer wants to know how far along it is.
    """
    tx.execute("UPDATE runs SET phase = ? WHERE id = ?", (phase, run_id))


def get(tx: Tx, run_id: str) -> Run | None:
    row = tx.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    return _to_record(row) if row else None


def set_status(tx: Tx, run_id: str, status: str) -> None:
    """Move a run's status, stamping the matching timestamp.

    `started_at` is set only on the first transition to running — a run resumed
    after an abort keeps its original start time, because the ETA and the
    duration should describe the run, not the latest attempt at it.
    """
    if status == "running":
        tx.execute(
            "UPDATE runs SET status = ?, started_at = COALESCE(started_at, ?) WHERE id = ?",
            (status, now().isoformat(), run_id),
        )
    elif status in TERMINAL:
        tx.execute(
            "UPDATE runs SET status = ?, finished_at = ? WHERE id = ?",
            (status, now().isoformat(), run_id),
        )
    else:
        tx.execute("UPDATE runs SET status = ? WHERE id = ?", (status, run_id))


def set_rates(
    tx: Tx, run_id: str, *, escalation_rate: float | None, reproducibility_rate: float | None
) -> None:
    """Surfaced, not buried (18.2)."""
    tx.execute(
        "UPDATE runs SET escalation_rate = ?, reproducibility_rate = ? WHERE id = ?",
        (escalation_rate, reproducibility_rate, run_id),
    )


def sign_off(tx: Tx, run_id: str, actor_id: str) -> None:
    tx.execute(
        "UPDATE runs SET reviewed_by = ?, reviewed_at = ? WHERE id = ?",
        (actor_id, now().isoformat(), run_id),
    )


def list_active(tx: Tx) -> list[Run]:
    rows = tx.execute(
        "SELECT * FROM runs WHERE status IN ('pending','running') ORDER BY created_at"
    ).fetchall()
    return [_to_record(row) for row in rows]


def count_active(tx: Tx) -> int:
    """Runs that can still change, matching `list_active`'s definition."""
    row = tx.execute(
        "SELECT COUNT(*) AS n FROM runs WHERE status IN ('pending','running')"
    ).fetchone()
    return int(row["n"])


def list_all(tx: Tx) -> list[Run]:
    rows = tx.execute("SELECT * FROM runs ORDER BY created_at DESC").fetchall()
    return [_to_record(row) for row in rows]


def _to_record(row: Any) -> Run:  # noqa: ANN401 — sqlite3.Row
    return Run(
        id=row["id"],
        position_id=row["position_id"],
        rubric_id=row["rubric_id"],
        folder=row["folder"],
        status=row["status"],
        phase=row["phase"],
        judge_digest=row["judge_digest"],
        verifier_model=row["verifier_model"],
        verifier_digest=row["verifier_digest"],
        verification_enabled=bool(row["verification_enabled"]),
        prompt_hash=row["prompt_hash"],
        redaction_on=bool(row["redaction_on"]),
        num_ctx=row["num_ctx"],
        app_version=row["app_version"],
        file_count=row["file_count"],
        escalation_rate=row["escalation_rate"],
        created_at=row["created_at"],
        created_by=row["created_by"],
        reviewed_by=row["reviewed_by"],
        reproducibility_rate=row["reproducibility_rate"],
    )
