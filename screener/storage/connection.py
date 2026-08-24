"""Engine, connection pool and migration state (spec 12.1, 12.3).

**The pool is SQLAlchemy's, not ours.** This module used to hand out one raw
connection per thread from a `threading.local()` cache and rely on callers to
give it back. Nothing ever did, so a long-lived API process accumulated one
permanently-open connection per threadpool thread it had served — measured at
23 → 68 descriptors over 300 requests — until it exhausted the database and
brought the worker down.

A pool fixes that structurally rather than by discipline: a connection is
checked out for one unit of work and returned by the context manager on the way
out, success or exception. There is no release for a caller to forget.

**Postgres, and only Postgres.** SQLite was the proof-of-concept backend and is
gone — not deprecated, removed. Keeping both was not free: the two disagree on
things that do not announce themselves. `MAX(a, b)` is a scalar function in one
and an aggregate in the other; `0` is a boolean in one and a type error in the
other; and, worst of the three, SQLite's `BEGIN IMMEDIATE` serialises writers
across the whole database while Postgres locks rows, so read-then-write sequences
that were safe by accident stopped being safe without a line of them changing.
Each of those shipped. The dual-backend abstraction is what let them.

`ports.py` still names the storage seam, so a second backend is a new adapter
rather than a rewrite — but nothing here branches on a dialect any more, and
adding a branch back is the thing to argue about rather than do.

**Migrations are a startup gate, not a warning.** A schema/code mismatch on a
database holding candidate decisions is a data-integrity incident: the process
refuses to start rather than writing rows that half-match the schema it expects.
"""

import hashlib
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool

from config.settings import settings

MIGRATIONS_ROOT = Path(__file__).resolve().parent / "migrations"


def migrations_dir() -> Path:
    """Where yoyo reads migrations from (12.2, 12.3).

    Still a subdirectory rather than the root, and the reason survives having one
    backend: `yoyo.read_migrations` globs a single directory and does **not**
    recurse. A migration filed one level down is not an error — it is simply never
    read, so `pending_migrations()` reports the schema current while the code runs
    against the previous one. Pointing this at the root would silently disable
    every migration under it.
    """
    return MIGRATIONS_ROOT / "postgres"


class PendingMigrationsError(RuntimeError):
    """The database schema is behind the code. Fatal at startup (12.1)."""


class DatabaseUnavailableError(RuntimeError):
    """The database could not be reached. Distinct from a stale schema (12.1)."""


class WorkerIdentityTakenError(RuntimeError):
    """Another live worker already answers to this ``worker_id`` (16.5)."""


def _identity_key(worker_id: str) -> int:
    """A stable 64-bit key for `worker_id`, for Postgres' advisory lock space.

    Hashed rather than enumerated so the key needs no registry and no migration:
    the same name always produces the same key, on any host, forever.
    """
    digest = hashlib.blake2b(worker_id.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big", signed=True)


def acquire_worker_identity(worker_id: str) -> Connection:
    """Claim exclusive use of `worker_id`, or refuse. Returns the holding connection.

    **This turns 16.5's central assumption into something the database enforces.**
    `reclaim_orphaned` resets every job still `claimed` by this worker's name, on
    the reasoning that a process which has claimed nothing yet can only be seeing
    its own previous life. That reasoning is airtight — provided the name belongs
    to exactly one worker. Nothing checked that. It was a comment in
    `config/settings.py` asking an operator not to get it wrong, and getting it
    wrong is silent: the second worker's startup reclaims the first one's
    *in-flight* job, both then screen the same resume, and two results are written
    for one candidate.

    That is not hypothetical. It happened on the development host, where `.env`
    pinned `WORKER_ID=worker-1` and a foreground `screener work` was run beside
    the daemon.

    A **session-scoped advisory lock** is the right instrument because its
    lifetime is exactly the thing being asked about. It is held by the connection,
    so it survives commits — and it is released by the server the moment the
    connection drops, whether that is a clean exit, a SIGKILL, or the machine
    losing power. A worker that died is therefore not still holding its name, and
    the restart that follows acquires it and reclaims normally. No timeout, no
    heartbeat, and no clock — which matters here for the same reason it matters
    everywhere else in 16.5.

    The connection is dedicated and unpooled: a pooled one would be handed back
    after the statement and take the lock with it.

    **The caller must keep the returned connection alive for as long as it wants
    the name.** Dropping it closes the session and releases the lock, so a caller
    that ignores the return value gets no protection at all and no error saying
    so. `Worker` keeps it on `self._identity` for the life of the process.
    """
    connection = connect()
    try:
        held = connection.execute(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": _identity_key(worker_id)}
        ).scalar()
        # Session locks outlive the transaction, so commit rather than sitting
        # `idle in transaction` for the worker's entire life.
        connection.commit()
    except Exception:
        connection.close()
        raise

    if not held:
        connection.close()
        raise WorkerIdentityTakenError(
            f"another worker is already running as {worker_id!r}. Starting a second "
            f"one under the same name would reclaim the first one's in-flight job "
            f"and screen the same resume twice. Stop the running worker, or give "
            f"this one its own name with WORKER_ID."
        )
    return connection


def release_worker_identity(connection: Connection | None) -> None:
    """Give the name back. Safe to call twice, and on a connection already gone."""
    if connection is None:
        return
    # Suppressed rather than handled: this runs on the way out, and the lock is
    # already gone in every case that could raise here — a connection that
    # cannot be closed is one the server has stopped counting.
    with suppress(Exception):
        connection.close()  # ends the session, which releases the lock


def redacted_url() -> str:
    """The configured database, safe to print. **Never interpolate the raw DSN.**

    The pending-migrations error used to embed a copy-pasteable `yoyo apply
    --database <dsn>`, and the dsn carries the password. Both systemd units call
    `require_current_schema` at startup, so a schema mismatch — exactly the moment
    someone pastes the error into a ticket — wrote the credential to the journal.

    SQLAlchemy masks the password in its own reprs; an f-string does not, which is
    the whole reason this exists rather than being done at each call site.
    """
    try:
        return make_url(settings.db_url).render_as_string(hide_password=True)
    except Exception:  # noqa: BLE001 — a malformed URL must not mask the real error
        return "<unparseable db_url>"


def database_url() -> str:
    """The SQLAlchemy URL. Always from the environment, never assembled here.

    It carries a password, which is why there is no repository-tracked default to
    fall back to — an absent `DB_URL` has to fail loudly rather than quietly
    connect somewhere else.
    """
    if not settings.db_url:
        raise RuntimeError(
            "DB_URL is not set. Point it at the Postgres instance, e.g. "
            "postgresql+psycopg://screener:<password>@localhost:5432/screener"
        )
    return settings.db_url


@lru_cache(maxsize=1)
def engine() -> Engine:
    """One pooled engine per process, built on first use.

    Cached rather than created per call: an `Engine` *is* the pool, so building
    a second one silently doubles the connection ceiling and defeats the point.
    """
    return create_engine(
        database_url(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle_s,
        # Verify a connection is alive before handing it out. Cheap, and it is
        # the difference between one failed request and a stampede of them after
        # a database restart or an idle-timeout cull.
        pool_pre_ping=True,
    )


def dispose_engine() -> None:
    """Close every pooled connection and forget the engine.

    For tests, which repoint `db_url` at a schema of their own, and for a
    deliberate shutdown. Not part of request handling — the pool is the thing
    that makes per-request release unnecessary.
    """
    if engine.cache_info().currsize:
        engine().dispose()
    engine.cache_clear()


def connect() -> Connection:
    """One unpooled connection. Caller owns closing it.

    `NullPool` because there is no reuse to gain and a pool left behind by a test
    would hold connections open past the end of it.
    """
    return create_engine(database_url(), poolclass=NullPool).connect()


@contextmanager
def temporary_connection() -> Iterator[Connection]:
    """A standalone connection, for tests and one-off maintenance."""
    connection = connect()
    try:
        yield connection
    finally:
        connection.close()


def wait_for_database(timeout_s: float = 30.0, interval_s: float = 1.0) -> None:
    """Block until the database answers, or raise.

    **New with Postgres, and not optional.** Under SQLite the database was a file
    that was there or was not; now it is a service that starts on its own
    schedule. Both units call `require_current_schema` as their first act, so
    without this a boot where Postgres is a few seconds behind produces a process
    that exits, gets restarted by `Restart=on-failure`, exits again — a loop whose
    log says "connection refused" rather than "not up yet".

    Bounded rather than infinite: a database that is genuinely misconfigured
    should fail the unit, not hang it.
    """
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            with engine().connect():
                return
        except OperationalError as err:
            if time.monotonic() >= deadline:
                raise DatabaseUnavailableError(
                    f"{redacted_url()} did not accept a connection within {timeout_s:.0f}s"
                ) from err
            # A refused connection during startup is expected; the pool caches
            # nothing useful from a failed attempt, so just retry.
            time.sleep(interval_s)


# --- migrations --------------------------------------------------------------


def _backend() -> Any:  # noqa: ANN401 — yoyo ships no type information
    """yoyo's own backend, over the same DSN SQLAlchemy uses.

    The URL keeps its `+psycopg` suffix rather than being stripped to bare
    `postgresql://`. yoyo 9.0 registers both: `postgresql` selects its psycopg2
    backend and `postgresql+psycopg` selects the psycopg3 one. Stripping the
    suffix would quietly put migrations on psycopg2 while SQLAlchemy runs on
    psycopg3 — two drivers to install, and a failure at the first migration on a
    host that only has the one this project actually declares.
    """
    from yoyo import get_backend  # type: ignore[import-untyped]

    return get_backend(database_url())


def _migrations() -> Any:  # noqa: ANN401 — yoyo ships no type information
    from yoyo import read_migrations

    return read_migrations(str(migrations_dir()))


def pending_migrations() -> list[str]:
    """Ids of migrations not yet applied to this database."""
    backend = _backend()
    migrations = _migrations()
    with backend.lock():
        return [m.id for m in backend.to_apply(migrations)]


def apply_migrations() -> list[str]:
    """Apply everything outstanding. Returns what was applied."""
    backend = _backend()
    migrations = _migrations()
    with backend.lock():
        outstanding = backend.to_apply(migrations)
        applied = [m.id for m in outstanding]
        backend.apply_migrations(outstanding)
    return applied


def require_current_schema() -> None:
    """Startup gate for the API and the worker (12.1).

    Raises rather than warns, and does not auto-migrate. Applying a schema change
    as a side effect of starting a process means the migration runs at an
    unplanned time, on a database nobody has backed up, possibly from two
    processes at once.

    Waits for the database first so "not up yet" and "schema is stale" are
    different errors. They have different fixes and only one of them is urgent.
    """
    wait_for_database()
    outstanding = pending_migrations()
    if outstanding:
        raise PendingMigrationsError(
            f"{len(outstanding)} migration(s) pending on {redacted_url()}: "
            f"{', '.join(outstanding)}. Run: screener migrate"
        )
