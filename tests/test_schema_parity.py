"""The Postgres schema and the SQLite one describe the same database (12.2).

Two hand-maintained schemas drift. The failure is not loud: a column added to
one backend and forgotten on the other works perfectly until the day somebody
deploys the other backend, and then it is an `UndefinedColumn` at runtime in the
environment that matters most.

There is no Postgres on the build host, so this cannot check by connecting. It
checks the thing that can be checked without one — that every table and column
in the SQLite end state exists in the Postgres DDL, and vice versa — which is
the part that actually rots.

What this does **not** prove: that the Postgres DDL executes. Only a Postgres
can say that.
"""

import re

import pytest
from sqlalchemy import MetaData, create_engine

from screener.storage.connection import MIGRATIONS_ROOT, apply_migrations

POSTGRES_SCHEMA = MIGRATIONS_ROOT / "postgres" / "0001.initial-schema.sql"

# Lines inside CREATE TABLE that open a constraint rather than declare a column.
_CONSTRAINT = re.compile(r"^\s*(UNIQUE|PRIMARY|FOREIGN|CHECK|CONSTRAINT)\b", re.IGNORECASE)


def postgres_tables() -> dict[str, set[str]]:
    """Table -> column names, parsed from the Postgres migration."""
    source = POSTGRES_SCHEMA.read_text(encoding="utf-8")
    # Strip comments so a commented-out column is not counted as real.
    source = "\n".join(line.split("--")[0] for line in source.splitlines())

    tables: dict[str, set[str]] = {}
    for match in re.finditer(r"CREATE TABLE (\w+)\s*\((.*?)\n\);", source, re.DOTALL):
        name, body = match.group(1), match.group(2)
        columns = set()
        depth = 0
        for line in body.splitlines():
            stripped = line.strip()
            # Only take lines at paren depth 0 — a CHECK (...) spanning lines
            # must not contribute its contents as column names.
            if depth == 0 and stripped and not _CONSTRAINT.match(stripped):
                columns.add(stripped.split()[0].strip(","))
            depth += line.count("(") - line.count(")")
        tables[name] = columns
    return tables


@pytest.fixture(scope="module")
def sqlite_tables(tmp_path_factory: pytest.TempPathFactory) -> dict[str, set[str]]:
    """Table -> column names, from the SQLite migrations actually applied."""
    path = tmp_path_factory.mktemp("parity") / "screener.db"
    apply_migrations(path)
    metadata = MetaData()
    metadata.reflect(bind=create_engine(f"sqlite+pysqlite:///{path}"))
    return {
        name: {c.name for c in table.columns}
        for name, table in metadata.tables.items()
        if "yoyo" not in name
    }


def test_the_postgres_migration_exists() -> None:
    assert POSTGRES_SCHEMA.is_file(), "no Postgres schema — db_backend=postgres cannot migrate"


def test_both_backends_declare_the_same_tables(sqlite_tables: dict[str, set[str]]) -> None:
    assert set(postgres_tables()) == set(sqlite_tables)


@pytest.mark.parametrize(
    "table",
    [
        "users",
        "positions",
        "rubrics",
        "runs",
        "candidates",
        "jobs",
        "verdicts",
        "overrides",
        "audit_log",
    ],
)
def test_each_table_has_the_same_columns(table: str, sqlite_tables: dict[str, set[str]]) -> None:
    postgres = postgres_tables()[table]
    sqlite = sqlite_tables[table]
    assert postgres == sqlite, (
        f"{table}: only in postgres={sorted(postgres - sqlite)}, "
        f"only in sqlite={sorted(sqlite - postgres)}"
    )


def test_the_append_only_audit_trigger_survived_the_translation() -> None:
    """SQLite spells this `RAISE(ABORT)`; Postgres needs a function.

    The guarantee is a control, not hygiene — it is what stops the record of who
    decided what being edited afterwards. A schema rewrite is exactly where it
    would get dropped without anyone noticing, because nothing else fails.
    """
    source = POSTGRES_SCHEMA.read_text(encoding="utf-8")
    assert "RAISE EXCEPTION 'audit_log is append-only'" in source
    assert "BEFORE UPDATE ON audit_log" in source
    assert "BEFORE DELETE ON audit_log" in source


def test_the_cache_index_is_still_partial() -> None:
    """A non-cacheable row must be invisible to cache lookup by construction (12.5)."""
    source = POSTGRES_SCHEMA.read_text(encoding="utf-8")
    assert re.search(r"CREATE INDEX idx_cache ON candidates\(.*?\) WHERE cacheable", source, re.S)


def test_a_reference_is_unique_only_while_a_requisition_is_open() -> None:
    source = POSTGRES_SCHEMA.read_text(encoding="utf-8")
    assert "CREATE UNIQUE INDEX idx_positions_open_reference" in source
    assert "WHERE status = 'open'" in source
