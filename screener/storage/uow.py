"""Transaction boundary (spec 12.2). Satisfies ``ports.UnitOfWork``.

**Stores receive a transaction; they never open one.** That single rule is what
keeps a multi-table operation atomic. Without it, `record_override` writes the
override, then writes the audit row, and a crash between the two leaves an
override with no audit trail — a record of a decision affecting a candidate, with
no record of who made it. The same applies to sign-off and to purge, which spans
four tables plus the filesystem (12.6).

**Never held open across an LLM call.** A judge call is ~5 s; a transaction
around it holds the write lock for that entire time and blocks the API's writer
in another process. The worker's pattern is claim (tx) → screen (no tx) → save
(tx), and the shape of this class is what makes that the natural way to write
it: you cannot accidentally wrap `screen_one` in a `with uow()` and have it look
correct.

**One unit of work borrows one pooled connection and returns it on exit.** The
connection is acquired in `__enter__` and released in `__exit__`, so there is no
release for a caller to forget — which is what the previous thread-local cache
depended on, and never got (see `connection.py`).
"""

from types import TracebackType
from typing import Any

from sqlalchemy import Connection, text

from screener.storage.connection import engine


def to_named(sql: str) -> tuple[str, int]:
    """Rewrite `?` placeholders as `:p0, :p1, …`. Returns the SQL and the count.

    **Why this is not done in the stores.** `?` is SQLite's placeholder and
    Postgres wants `%s`; SQLAlchemy will emit whichever the driver needs, but
    only for a statement whose binds it can see — which means named ones. The
    obvious fix is to rewrite all 73 statements by hand, and it does not work:
    several are assembled at runtime (`",".join("?" for …)` in `results_store`
    and `jobs_store`), so the placeholders do not exist until the query is
    built. Translating here catches those too, in one tested place.

    Quoted regions are skipped. No statement in the repo currently contains a
    literal `?` inside quotes — this was checked — but a translator that would
    corrupt one the day somebody adds it is not worth the four lines it saves.
    """
    out: list[str] = []
    count = 0
    quote: str | None = None
    for char in sql:
        if quote:
            if char == quote:
                quote = None
            out.append(char)
        elif char in ("'", '"'):
            quote = char
            out.append(char)
        elif char == "?":
            out.append(f":p{count}")
            count += 1
        else:
            out.append(char)
    return "".join(out), count


class Cursor:
    """The little of a DBAPI cursor the stores actually use.

    Rows come back as SQLAlchemy `RowMapping`, which supports `row["column"]`,
    `dict(row)` and `.keys()` exactly as `sqlite3.Row` did — so the ~105 lookups
    across the stores did not have to change when the driver did. That is the
    point of putting the seam here rather than in each store.
    """

    def __init__(self, result: Any) -> None:  # noqa: ANN401 — SQLAlchemy CursorResult
        self._result = result

    def fetchone(self) -> Any:  # noqa: ANN401 — a row, shaped by the query
        return self._result.mappings().fetchone()

    def fetchall(self) -> list[Any]:
        return list(self._result.mappings().fetchall())

    @property
    def rowcount(self) -> int:
        return int(self._result.rowcount)

    @property
    def lastrowid(self) -> int | None:
        # SQLite-specific, and one of the few things that does not survive the
        # move to Postgres: there the insert has to carry `RETURNING id`.
        return self._result.lastrowid  # type: ignore[no-any-return]


class Tx:
    """A connection inside an open transaction, handed to stores.

    Deliberately thin. It exists so store signatures say `tx: Tx` rather than
    naming the driver — which would let a store call `commit()` and quietly take
    ownership of a boundary it does not own.

    Statements keep their `?` placeholders and are translated to named binds on
    the way through (`to_named`), so SQLAlchemy emits whatever paramstyle the
    driver wants. That is what makes the same store code run on SQLite and
    Postgres without a rewrite.
    """

    def __init__(self, connection: Connection) -> None:
        self._connection = connection

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> Cursor:
        statement, count = to_named(sql)
        if count != len(params):
            # Caught here rather than as a driver error three frames down, which
            # reports the mismatch without naming the statement that caused it.
            raise ValueError(
                f"statement has {count} placeholder(s) but {len(params)} parameter(s) given: {sql}"
            )
        binds = {f"p{i}": value for i, value in enumerate(params)}
        return Cursor(self._connection.execute(text(statement), binds))

    def executemany(self, sql: str, seq: list[tuple[object, ...]]) -> Cursor:
        statement, count = to_named(sql)
        if not seq:
            # An empty sequence is "nothing to do" in some dialects and an error
            # in others. Deciding it here keeps callers from each guarding it
            # differently — and `executemany` with no rows is a normal case
            # (a run whose folder held no new files).
            return Cursor(_EmptyResult())
        binds = [{f"p{i}": value for i, value in enumerate(row)} for row in seq]
        if any(len(row) != count for row in seq):
            raise ValueError(f"statement has {count} placeholder(s); a row did not match: {sql}")
        return Cursor(self._connection.execute(text(statement), binds))


class _EmptyResult:
    """Stands in for a result set nobody produced, so `executemany([])` is a no-op."""

    rowcount = 0
    lastrowid = None

    def mappings(self) -> "_EmptyResult":
        return self

    def fetchone(self) -> None:
        return None

    def fetchall(self) -> list[Any]:
        return []


class UnitOfWork:
    """Context manager owning one transaction and the connection under it.

    Commits on clean exit, rolls back on any exception, and returns the
    connection to the pool either way. Re-entry is refused rather than silently
    nested: SQLite has no nested transactions, so an inner `with` that appeared
    to commit would actually be committing the outer one's partial work.
    """

    def __init__(self, connection: Connection | None = None, *, read_only: bool = False) -> None:
        # A caller-supplied connection is borrowed, not owned — tests pass one in
        # and close it themselves. Only a connection this object opened is
        # returned to the pool by this object.
        self._connection = connection
        self._owns_connection = connection is None
        self._depth = 0
        self._read_only = read_only

    def read_only(self) -> "UnitOfWork":
        """A transaction that takes no write reservation.

        `BEGIN DEFERRED` instead of `IMMEDIATE` on SQLite: it takes no lock up
        front, so a poller using this cannot contend with the worker's writes
        for the single write-reservation slot the way `run_status` did (12.2).
        Safe only because nothing in the block may write — a write here would
        upgrade to the write lock mid-transaction, which can itself fail after
        work has already been done, exactly what `IMMEDIATE` avoids elsewhere.
        """
        return UnitOfWork(self._connection, read_only=True)

    def __enter__(self) -> Tx:
        if self._depth:
            raise RuntimeError("UnitOfWork is not re-entrant; SQLite has no nested transactions")
        self._depth = 1
        if self._connection is None:
            self._connection = engine().connect()
        # Read by the `begin` event in `connection.py`, which is the only thing
        # that opens a SQLite transaction. Ignored by dialects that do not need
        # to choose (Postgres opens its own).
        self._connection = self._connection.execution_options(
            begin_mode="DEFERRED" if self._read_only else "IMMEDIATE"
        )
        self._transaction = self._connection.begin()
        return Tx(self._connection)

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._depth = 0
        try:
            if exc_type is None:
                self._transaction.commit()
            else:
                self._transaction.rollback()
        finally:
            # Back to the pool. This is the half the hand-rolled thread-local
            # cache never had, and the reason the leak cannot recur.
            if self._owns_connection and self._connection is not None:
                self._connection.close()
                self._connection = None


def unit_of_work() -> UnitOfWork:
    """Factory used by the service layer. One call, one transaction."""
    return UnitOfWork()
