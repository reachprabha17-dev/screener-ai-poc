"""Connection management and migration state (spec 12.1, 12.3).

**Every connection is created in the thread that uses it.** `sqlite3` sets
`check_same_thread=True`, so a connection made on one thread raises the moment
another touches it. FastAPI runs sync handlers in a threadpool, so this is not a
style preference — a module-level connection object works perfectly in
development and fails under the second concurrent request.

**There are two writers**: the API and the worker, in separate processes. WAL
makes that safe — one writer, many readers, readers never blocked — but only if
transactions stay short. `busy_timeout` covers the brief windows where the
writer lock is held; it is not a substitute for keeping the LLM call outside the
transaction (12.2).

**Migrations are a startup gate, not a warning.** A schema/code mismatch on a
database holding candidate decisions is a data-integrity incident: the process
refuses to start rather than writing rows that half-match the schema it expects.
"""

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from config.settings import settings

MIGRATIONS_ROOT = Path(__file__).resolve().parent / "migrations"

_local = threading.local()


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


def _configure(connection: sqlite3.Connection) -> None:
    """PRAGMAs that carry real weight.

    `foreign_keys` is OFF by default in SQLite and must be set **per connection**
    — not once at creation. A connection that forgets it silently accepts
    verdict rows pointing at candidates that do not exist.
    """
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA synchronous = NORMAL")


def db_path() -> Path:
    return Path(settings.db_path)


def connect(path: Path | None = None) -> sqlite3.Connection:
    """A new configured connection. Caller owns closing it."""
    target = path or db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(
        target,
        isolation_level=None,  # explicit BEGIN/COMMIT — the UoW owns transactions
        timeout=5.0,
    )
    connection.row_factory = sqlite3.Row
    _configure(connection)
    return connection


def get_connection() -> sqlite3.Connection:
    """The calling thread's connection, created on first use.

    Held in `threading.local()` so no connection ever crosses a thread boundary
    or gets stored on a shared object.
    """
    existing: sqlite3.Connection | None = getattr(_local, "connection", None)
    if existing is None:
        existing = connect()
        _local.connection = existing
    return existing


def close_connection() -> None:
    """Close and forget this thread's connection. Safe to call twice."""
    existing: sqlite3.Connection | None = getattr(_local, "connection", None)
    if existing is not None:
        existing.close()
        _local.connection = None


@contextmanager
def temporary_connection(path: Path) -> Iterator[sqlite3.Connection]:
    """A standalone connection to a specific file, for tests and migrations."""
    connection = connect(path)
    try:
        yield connection
    finally:
        connection.close()


# --- migrations --------------------------------------------------------------


def _backend(path: Path) -> Any:  # noqa: ANN401 — yoyo ships no type information
    from yoyo import get_backend  # type: ignore[import-untyped]

    path.parent.mkdir(parents=True, exist_ok=True)
    return get_backend(f"sqlite:///{path.resolve()}")


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
            f"Run: yoyo apply --database sqlite:///{(path or db_path())} {migrations_dir()}"
        )
