"""Requisitions and their users (spec §12.4).

`positions.reference` is the folder name under `data/resumes/` and is UNIQUE.
That constraint is what stops two requisitions pointing at the same folder and
each snapshotting the other's candidates into their own run.
"""

import json
from typing import Any

from screener.models import Position, now
from screener.storage.uow import Tx


def seed_user(
    tx: Tx, actor_id: str, display_name: str, roles: tuple[str, ...] = ("admin",)
) -> None:
    """Create the PoC operator row if absent.

    Every table with an actor column has a foreign key to `users`, so this row
    has to exist before anything else can be written. Auth is stubbed today
    (§15.2), but the *referential* plumbing is real from day one — that is the
    part that is expensive to retrofit.
    """
    tx.execute(
        "INSERT OR IGNORE INTO users (id, display_name, roles_json, active, created_at) "
        "VALUES (?, ?, ?, 1, ?)",
        (actor_id, display_name, json.dumps(list(roles)), now().isoformat()),
    )


def user_exists(tx: Tx, actor_id: str) -> bool:
    return tx.execute("SELECT 1 FROM users WHERE id = ?", (actor_id,)).fetchone() is not None


def create(tx: Tx, position: Position) -> None:
    tx.execute(
        "INSERT INTO positions (id, reference, title, jd_text, created_by, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            position.id,
            position.reference,
            position.title,
            position.jd_text,
            position.created_by,
            position.created_at.isoformat(),
        ),
    )


def get(tx: Tx, position_id: str) -> Position | None:
    row = tx.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
    return _to_position(row) if row else None


def get_by_reference(tx: Tx, reference: str) -> Position | None:
    row = tx.execute("SELECT * FROM positions WHERE reference = ?", (reference,)).fetchone()
    return _to_position(row) if row else None


def list_open(tx: Tx) -> list[Position]:
    rows = tx.execute(
        "SELECT * FROM positions WHERE status = 'open' ORDER BY created_at DESC"
    ).fetchall()
    return [_to_position(row) for row in rows]


def close(tx: Tx, position_id: str) -> None:
    """No `actor` parameter: stores do not audit, the service layer does (§12.2).

    Threading an actor here that is only used by the caller's audit row would
    suggest this function records it, and someone would eventually rely on that.
    """
    tx.execute(
        "UPDATE positions SET status = 'closed', closed_at = ? WHERE id = ?",
        (now().isoformat(), position_id),
    )


def _to_position(row: Any) -> Position:  # noqa: ANN401 — sqlite3.Row
    return Position(
        id=row["id"],
        reference=row["reference"],
        title=row["title"],
        jd_text=row["jd_text"],
        created_by=row["created_by"],
        created_at=row["created_at"],
    )
