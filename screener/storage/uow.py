"""Transaction boundary (spec 12.2). Satisfies ``ports.UnitOfWork``.

**Stores receive a transaction; they never open one.** That single rule is what
keeps a multi-table operation atomic. Without it, `record_override` writes the
override, then writes the audit row, and a crash between the two leaves an
override with no audit trail — a record of a decision affecting a candidate, with
no record of who made it. The same applies to sign-off and to purge, which spans
four tables plus the filesystem (12.6).

**Never held open across an LLM call.** A judge call is ~5 s; a transaction
around it holds SQLite's write lock for that entire time and blocks the API's
writer in another process. The worker's pattern is claim (tx) → screen (no tx) →
save (tx), and the shape of this class is what makes that the natural way to
write it: you cannot accidentally wrap `screen_one` in a `with uow()` and have it
look correct.
"""

import sqlite3
from types import TracebackType

from screener.storage.connection import get_connection


class Tx:
    """A cursor inside an open transaction, handed to stores.

    Deliberately thin. It exists so store signatures say `tx: Tx` rather than
    `connection: sqlite3.Connection` — which would let a store call `commit()`
    and quietly take ownership of a boundary it does not own.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> sqlite3.Cursor:
        return self._connection.execute(sql, params)

    def executemany(self, sql: str, seq: list[tuple[object, ...]]) -> sqlite3.Cursor:
        return self._connection.executemany(sql, seq)

    @property
    def lastrowid(self) -> int | None:
        cursor = self._connection.execute("SELECT last_insert_rowid() AS id")
        row = cursor.fetchone()
        return int(row["id"]) if row else None


class UnitOfWork:
    """Context manager owning one transaction.

    Commits on clean exit, rolls back on any exception. Re-entry is refused
    rather than silently nested: SQLite has no nested transactions, so an inner
    `with` that appeared to commit would actually be committing the outer one's
    partial work.
    """

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self._connection = connection or get_connection()
        self._depth = 0

    def __enter__(self) -> Tx:
        if self._depth:
            raise RuntimeError("UnitOfWork is not re-entrant; SQLite has no nested transactions")
        self._depth = 1
        self._connection.execute("BEGIN IMMEDIATE")
        return Tx(self._connection)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._depth = 0
        if exc_type is None:
            self._connection.execute("COMMIT")
        else:
            self._connection.execute("ROLLBACK")


def unit_of_work() -> UnitOfWork:
    """Factory used by the service layer. One call, one transaction.

    ``BEGIN IMMEDIATE`` rather than the default deferred begin: it takes the
    write lock up front, so two writers contend at the start of a transaction —
    where `busy_timeout` handles it — instead of at the first write, where SQLite
    would raise `SQLITE_BUSY` on a transaction that has already done work.
    """
    return UnitOfWork()
