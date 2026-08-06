"""Trace index (spec 17, 12.6).

**Traces contain full resume text.** That makes `trace_dir` a second store of
candidate data, and the reason this table exists at all: the files must be
findable by `file_sha256` so `purge_candidate` can delete them.

An unindexed trace directory would leave erasure looking implemented while a
complete copy of the candidate's resume sits on disk — worse than having no
traces, because the control would be reported as working.

The row records a path; the file is written and deleted by the caller. Keeping
`unlink` out of the store means filesystem effects happen outside the
transaction, where they cannot be rolled back to a state the disk does not share.
"""

from pathlib import Path
from typing import Any

from screener.models import now
from screener.storage.uow import Tx


def record(tx: Tx, run_id: str, file_sha256: str, trace_path: Path) -> None:
    tx.execute(
        "INSERT INTO traces (run_id, file_sha256, trace_path, created_at) VALUES (?, ?, ?, ?)",
        (run_id, file_sha256, str(trace_path), now().isoformat()),
    )


def paths_for(tx: Tx, file_sha256: str) -> list[Path]:
    rows = tx.execute(
        "SELECT trace_path FROM traces WHERE file_sha256 = ?", (file_sha256,)
    ).fetchall()
    return [Path(row["trace_path"]) for row in rows]


def paths_for_run(tx: Tx, run_id: str) -> list[Path]:
    rows = tx.execute("SELECT trace_path FROM traces WHERE run_id = ?", (run_id,)).fetchall()
    return [Path(row["trace_path"]) for row in rows]


def count(tx: Tx) -> int:
    row: Any = tx.execute("SELECT COUNT(*) AS n FROM traces").fetchone()
    return int(row["n"])
