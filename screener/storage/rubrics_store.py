"""Rubric versions and approval (spec 12.4).

Rubrics are **versioned, never edited in place**. `UNIQUE(position_id, version)`
enforces it. A run records the `rubric_id` it used and candidates carry the
`rubric_hash`, so editing a rubric a run had already been scored against would
make those stored decisions unexplainable — the reviewer would see verdicts
against criteria that no longer exist.

`approved_by` / `approved_at` are the human gate in front of an LLM-generated
rubric (9.1). A rubric is not usable for a run until they are set.
"""

import json
from typing import Any

from screener.models import Actor, Criterion, Rubric, now
from screener.storage.uow import Tx


def latest_version(tx: Tx, position_id: str) -> int:
    """The highest stored version, or 0 when the position has no rubric yet.

    0 rather than `None` so a first save can pass `base_version=0` and be checked
    by the same comparison as every later one — otherwise "no rubric existed when
    I started" is the one concurrent case with no way to express it.
    """
    row = tx.execute(
        "SELECT COALESCE(MAX(version), 0) AS v FROM rubrics WHERE position_id = ?",
        (position_id,),
    ).fetchone()
    return int(row["v"])


def next_version(tx: Tx, position_id: str) -> int:
    return latest_version(tx, position_id) + 1


def create(tx: Tx, rubric: Rubric) -> None:
    """Store a version. `rubric_hash` is computed here, from the criteria.

    Derived rather than accepted from the caller: it is a cache-key field, and a
    hash that does not match the criteria it labels would serve one rubric's
    judgments under another's identity.
    """
    tx.execute(
        "INSERT INTO rubrics (id, position_id, version, criteria_json, rubric_hash, "
        "created_by, created_at, approved_by, approved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            rubric.id,
            rubric.position_id,
            rubric.version,
            json.dumps([c.model_dump() for c in rubric.criteria], ensure_ascii=False),
            rubric.content_hash,
            rubric.created_by,
            now().isoformat(),
            rubric.approved_by,
            rubric.approved_at.isoformat() if rubric.approved_at else None,
        ),
    )


def get(tx: Tx, rubric_id: str) -> Rubric | None:
    row = tx.execute("SELECT * FROM rubrics WHERE id = ?", (rubric_id,)).fetchone()
    return _to_rubric(row) if row else None


def latest_for_position(tx: Tx, position_id: str) -> Rubric | None:
    row = tx.execute(
        "SELECT * FROM rubrics WHERE position_id = ? ORDER BY version DESC LIMIT 1",
        (position_id,),
    ).fetchone()
    return _to_rubric(row) if row else None


def approved_for_position(tx: Tx, position_id: str) -> Rubric | None:
    row = tx.execute(
        "SELECT * FROM rubrics WHERE position_id = ? AND approved_at IS NOT NULL "
        "ORDER BY version DESC LIMIT 1",
        (position_id,),
    ).fetchone()

    return _to_rubric(row) if row else None



def approve(tx: Tx, rubric_id: str, actor: Actor) -> None:
    tx.execute(
        "UPDATE rubrics SET approved_by = ?, approved_at = ? WHERE id = ?",
        (actor.id, now().isoformat(), rubric_id),
    )


def is_approved(tx: Tx, rubric_id: str) -> bool:
    row = tx.execute("SELECT approved_at FROM rubrics WHERE id = ?", (rubric_id,)).fetchone()
    return bool(row and row["approved_at"])


def hash_for(tx: Tx, rubric_id: str) -> str | None:
    row = tx.execute("SELECT rubric_hash FROM rubrics WHERE id = ?", (rubric_id,)).fetchone()
    return str(row["rubric_hash"]) if row else None


def _to_rubric(row: Any) -> Rubric:  # noqa: ANN401 — sqlite3.Row
    return Rubric(
        id=row["id"],
        position_id=row["position_id"],
        version=row["version"],
        criteria=[Criterion(**c) for c in json.loads(row["criteria_json"])],
        created_by=row["created_by"],
        approved_by=row["approved_by"],
        approved_at=row["approved_at"],
    )
