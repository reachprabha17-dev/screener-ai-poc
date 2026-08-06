"""Audit log (spec 17). Append-only, enforced by database triggers.

This is the "who did what" stream, deliberately separate from the "what the
system did" JSONL log. Different purposes, different mutability: this one records
decisions — overrides, sign-offs, purges, rejections — and cannot be rewritten,
while the operational log rotates.

**Every append happens in the caller's transaction.** An audit row written in its
own transaction can commit while the write it describes rolls back, or vice
versa. The whole point of 12.2 is that the decision and the record of it land
together or not at all.

There is deliberately no `update` or `delete` here, and adding one would fail at
runtime anyway — the triggers abort. That is the intended behaviour: the API
surface and the database agree, so neither is the only thing standing between an
auditor and a rewritten history.
"""

import json
from typing import Any

from screener.models import Actor, now
from screener.storage.uow import Tx


def append(
    tx: Tx,
    actor_id: str | None,
    action: str,
    entity: str | None = None,
    entity_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Record one action. Never raises on a missing actor.

    `actor_id` is nullable because system-initiated events (a worker reclaiming
    an orphaned job) have no human behind them, and inventing a synthetic user
    to satisfy a NOT NULL would make those indistinguishable from real ones.
    """
    tx.execute(
        "INSERT INTO audit_log (ts, actor_id, action, entity, entity_id, detail_json) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (
            now().isoformat(),
            actor_id,
            action,
            entity,
            entity_id,
            json.dumps(detail, ensure_ascii=False) if detail is not None else None,
        ),
    )


def append_for(
    tx: Tx,
    actor: Actor,
    action: str,
    entity: str | None = None,
    entity_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Convenience wrapper for the common case of a real actor."""
    append(tx, actor.id, action, entity, entity_id, detail)


def list_for_entity(tx: Tx, entity: str, entity_id: str) -> list[dict[str, Any]]:
    rows = tx.execute(
        "SELECT ts, actor_id, action, entity, entity_id, detail_json FROM audit_log "
        "WHERE entity = ? AND entity_id = ? ORDER BY id",
        (entity, entity_id),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def recent(tx: Tx, limit: int = 100) -> list[dict[str, Any]]:
    rows = tx.execute(
        "SELECT ts, actor_id, action, entity, entity_id, detail_json FROM audit_log "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [_row_to_dict(row) for row in rows]


def _row_to_dict(row: Any) -> dict[str, Any]:  # noqa: ANN401 — sqlite3.Row
    detail = row["detail_json"]
    return {
        "ts": row["ts"],
        "actor_id": row["actor_id"],
        "action": row["action"],
        "entity": row["entity"],
        "entity_id": row["entity_id"],
        "detail": json.loads(detail) if detail else None,
    }
