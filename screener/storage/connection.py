"""Engine, connection pool and migration state (spec 12.1, 12.3).

**The pool is SQLAlchemy's, not ours.** This module used to hand out one
`sqlite3` connection per thread from a `threading.local()` cache and rely on
callers to give it back. Nothing ever did, so a long-lived API process
accumulated one permanently-open connection per threadpool thread it had served
— measured at 23 → 68 descriptors over 300 requests — until it exhausted
SQLite's single writer and brought the worker down with `database is locked`.

A pool fixes that structurally rather than by discipline: a connection is
checked out for one unit of work and returned by the context manager on the way
out, success or exception. There is no release for a caller to forget.

**Written for Postgres, running on SQLite.** `db_backend` selects the URL, the
migration directory and the dialect-specific behaviour below. Everything SQLite
needs that Postgres does not — the PRAGMAs, `BEGIN IMMEDIATE` — is registered
only when the dialect is SQLite, so moving to Postgres removes code here rather
than adding branches to it.

**Migrations are a startup gate, not a warning.** A schema/code mismatch on a
database holding candidate decisions is a data-integrity incident: the process
refuses to start rather than writing rows that half-match the schema it expects.
"""

import os
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import URL, Connection, Engine, create_engine, event, text
from sqlalchemy.exc import DatabaseError
from sqlalchemy.pool import NullPool

from config.settings import settings

MIGRATIONS_ROOT = Path(__file__).resolve().parent / "migrations"


def migrations_dir() -> Path:
    """The migration directory for the configured backend (12.2, 12.3).

    Migrations are **per dialect**, so the directory has to be selected rather
    than fixed. The subtlety worth stating: `yoyo.read_migrations` globs a single
    directory and does **not** recurse. A migration filed one level down is not
    an error — it is simply never read, so `pending_migrations()` reports the
    schema current while the code runs against the previous one. Pointing this
    at the root would silently disable every migration under it.
    """
    return MIGRATIONS_ROOT / settings.db_backend


class PendingMigrationsError(RuntimeError):
    """The database schema is behind the code. Fatal at startup (12.1)."""


def db_path() -> Path:
    return Path(settings.db_path)


def is_sqlite() -> bool:
    return settings.db_backend == "sqlite"


def database_url(path: Path | None = None) -> URL | str:
    """The SQLAlchemy URL for the configured backend.

    Postgres carries a password, so it comes from `db_url` (i.e. the
    environment) rather than being assembled from parts kept in the repository.
    """
    if not is_sqlite():
        if not settings.db_url:
            raise RuntimeError(
                f"db_backend={settings.db_backend} requires SCREENER_DB_URL to be set"
            )
        return settings.db_url
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    return URL.create("sqlite+pysqlite", database=str(target.resolve()))


def _install_sqlite_dialect(engine: Engine) -> None:
    """SQLite's two deviations from ordinary SQL, in one place.

    Both are registered as events rather than run per call site, because a
    connection that skips them is not obviously wrong — it just quietly accepts
    verdict rows pointing at candidates that do not exist, or takes its write
    lock too late. Neither has an analogue on Postgres, which is why this is
    guarded by dialect rather than branched inside the caller.
    """

    @event.listens_for(engine, "connect")
    def _configure(dbapi_connection: Any, _record: Any) -> None:  # noqa: ANN401 — DBAPI type
        # `foreign_keys` is OFF by default in SQLite and is **per connection**,
        # not a property of the file. Pooled connections each need it.
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA journal_mode = WAL")
            cursor.execute("PRAGMA busy_timeout = 5000")
            cursor.execute("PRAGMA foreign_keys = ON")
            cursor.execute("PRAGMA synchronous = NORMAL")
        finally:
            cursor.close()
        # Stop pysqlite emitting its own implicit BEGIN, so the `begin` event
        # below is the only thing that opens a transaction and we control which
        # kind. Without this the two fight and the lock is taken at first write.
        dbapi_connection.isolation_level = None

    @event.listens_for(engine, "begin")
    def _begin(connection: Connection) -> None:
        # `IMMEDIATE` takes the write lock up front, so two writers contend at
        # the start of a transaction — where `busy_timeout` handles it — rather
        # than at the first write, where SQLite raises `SQLITE_BUSY` on a
        # transaction that has already done work.
        #
        # `DEFERRED` for readers: it takes no lock at all, so a poller cannot
        # contend with the worker for the single write reservation.
        mode = connection.get_execution_options().get("begin_mode", "IMMEDIATE")
        connection.exec_driver_sql(f"BEGIN {mode}")


@lru_cache(maxsize=1)
def engine() -> Engine:
    """One pooled engine per process, built on first use.

    Cached rather than created per call: an `Engine` *is* the pool, so building
    a second one silently doubles the connection ceiling and defeats the point.
    """
    url = database_url()
    kwargs: dict[str, Any] = {
        "pool_size": settings.db_pool_size,
        "max_overflow": settings.db_max_overflow,
        "pool_timeout": settings.db_pool_timeout,
        "pool_recycle": settings.db_pool_recycle_s,
        # Verify a connection is alive before handing it out. Cheap, and it is
        # the difference between one failed request and a stampede of them after
        # a database restart or an idle-timeout cull.
        "pool_pre_ping": True,
    }
    if is_sqlite():
        # Pooled connections move between threads by design, so pysqlite's
        # same-thread assertion has to be turned off. Safe because a connection
        # is only ever checked out to one unit of work at a time.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 5.0}
    built = create_engine(url, **kwargs)
    if is_sqlite():
        _install_sqlite_dialect(built)
    return built


def dispose_engine() -> None:
    """Close every pooled connection and forget the engine.

    For tests, which point `db_path` at a fresh temporary file per case, and for
    a deliberate shutdown. Not part of request handling — the pool is the thing
    that makes per-request release unnecessary.
    """
    if engine.cache_info().currsize:
        engine().dispose()
    engine.cache_clear()


def connect(path: Path | None = None) -> Connection:
    """One unpooled connection to a specific database. Caller owns closing it.

    For tests and for the migration lock, which both want a connection to a file
    they name rather than whatever the process-wide engine points at. `NullPool`
    because there is no reuse to gain here and a pool left behind by a test would
    hold the file open after the test's temporary directory is gone.
    """
    single = create_engine(database_url(path), poolclass=NullPool)
    if is_sqlite():
        _install_sqlite_dialect(single)
    return single.connect()


@contextmanager
def temporary_connection(path: Path) -> Iterator[Connection]:
    """A standalone connection to a specific file, for tests and migrations."""
    connection = connect(path)
    try:
        yield connection
    finally:
        connection.close()


# --- migrations --------------------------------------------------------------


def _pid_alive(pid: int) -> bool:
    """Best-effort liveness check, the same technique `scripts/dev.sh` uses.

    Can be wrong in the narrow window where the OS has reused `pid` for an
    unrelated process since the lock row was written — accepted here for the
    same reason it is accepted there: the alternative, an absolute expiry, is
    wrong in the other direction on a clock that stepped (16.5's `reclaim_orphaned`
    makes the same trade for job leases).
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    return True


def _clear_dead_migration_lock(path: Path) -> None:
    """Self-heals the false "schema pending" report a crashed lock-holder leaves.

    `yoyo`'s advisory lock (`yoyo_lock`, one row, holding the pid that owns it)
    is acquired with a retrying connection but *released* with a single
    unretried `DELETE` (`DatabaseBackend._delete_lock_row`). Every api and worker
    start calls `require_current_schema` independently, each opening its own
    connection to `yoyo_lock` with **no `busy_timeout`** — SQLite's default is
    0, fail-instantly — so two of those checks landing within the same
    millisecond is enough for one release to hit "database is locked" and
    leave the row behind forever. Nothing was actually pending; the row is bad.

    Checked with our own connection, which carries the busy_timeout set in
    `_install_sqlite_dialect` and so tolerates the contention `yoyo`'s does not.
    Only clears rows whose pid is provably dead — a lock genuinely held by a
    live migration is left alone.
    """
    try:
        with temporary_connection(path) as connection:
            with connection.begin():
                rows = connection.execute(text("SELECT pid FROM yoyo_lock")).mappings().fetchall()
                for row in rows:
                    if not _pid_alive(row["pid"]):
                        connection.execute(
                            text("DELETE FROM yoyo_lock WHERE pid = :pid"), {"pid": row["pid"]}
                        )
    except DatabaseError:
        pass  # No `yoyo_lock` table yet — nothing has ever been migrated.


def _yoyo_url(path: Path) -> str:
    """yoyo takes its own DSN, and it is not quite SQLAlchemy's.

    The Postgres URL is passed through **with** its `+psycopg` suffix, not
    stripped to bare `postgresql://`. yoyo 9.0 registers both: `postgresql`
    selects its psycopg2 backend and `postgresql+psycopg` selects the psycopg3
    one. Stripping the suffix would quietly put migrations on psycopg2 while
    SQLAlchemy runs on psycopg3 — two Postgres drivers to install, and a
    failure at the first migration on a host that only has the one this project
    actually declares.
    """
    if is_sqlite():
        return f"sqlite:///{path.resolve()}"
    return settings.db_url


def _backend(path: Path) -> Any:  # noqa: ANN401 — yoyo ships no type information
    from yoyo import get_backend  # type: ignore[import-untyped]

    if is_sqlite():
        path.parent.mkdir(parents=True, exist_ok=True)
        _clear_dead_migration_lock(path)
    return get_backend(_yoyo_url(path))


def _migrations() -> Any:  # noqa: ANN401 — yoyo ships no type information
    from yoyo import read_migrations

    return read_migrations(str(migrations_dir()))


def pending_migrations(path: Path | None = None) -> list[str]:
    """Ids of migrations not yet applied to this database."""
    target = path or db_path()
    backend = _backend(target)
    migrations = _migrations()
    with backend.lock():
        return [m.id for m in backend.to_apply(migrations)]


def apply_migrations(path: Path | None = None) -> list[str]:
    """Apply everything outstanding. Returns what was applied."""
    target = path or db_path()
    backend = _backend(target)
    migrations = _migrations()
    with backend.lock():
        outstanding = backend.to_apply(migrations)
        applied = [m.id for m in outstanding]
        backend.apply_migrations(outstanding)
    return applied


def require_current_schema(path: Path | None = None) -> None:
    """Startup gate for the API and the worker (12.1).

    Raises rather than warns, and does not auto-migrate. Applying a schema change
    as a side effect of starting a process means the migration runs at an
    unplanned time, on a database nobody has backed up, possibly from two
    processes at once.
    """
    outstanding = pending_migrations(path)
    if outstanding:
        raise PendingMigrationsError(
            f"{len(outstanding)} migration(s) pending: {', '.join(outstanding)}. "
            f"Run: yoyo apply --database {_yoyo_url(path or db_path())} {migrations_dir()}"
        )
