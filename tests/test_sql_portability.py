"""The stores' SQL runs on Postgres as well as SQLite (spec 12.2).

The backend is `sqlite` today and `postgres` later, and the failure mode of
getting this wrong is not a compile error — it is a query that works on the
developer's machine and fails in the environment that matters. These tests
answer the two questions that decide it: are the placeholders portable, and is
there any SQLite-only syntax left.
"""

import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.dialects import postgresql, sqlite

from screener.storage.uow import to_named

STORE_DIR = Path(__file__).resolve().parent.parent / "screener" / "storage"
STORES = sorted(STORE_DIR.glob("*_store.py"))


class TestPlaceholderTranslation:
    """`?` is SQLite's paramstyle; SQLAlchemy needs named binds to emit any other."""

    def test_positional_markers_become_numbered_binds(self) -> None:
        assert to_named("SELECT * FROM t WHERE a = ? AND b = ?") == (
            "SELECT * FROM t WHERE a = :p0 AND b = :p1",
            2,
        )

    def test_a_statement_with_no_parameters_is_untouched(self) -> None:
        assert to_named("SELECT 1") == ("SELECT 1", 0)

    def test_a_question_mark_inside_a_literal_is_not_a_placeholder(self) -> None:
        # No store does this today. The translator handles it anyway, because
        # the day one does, the corruption would be silent.
        statement, count = to_named("SELECT * FROM t WHERE msg = 'why? ok' AND a = ?")
        assert statement == "SELECT * FROM t WHERE msg = 'why? ok' AND a = :p0"
        assert count == 1

    def test_the_same_translation_compiles_on_both_dialects(self) -> None:
        """The actual portability claim, checked rather than asserted."""
        statement, _ = to_named("SELECT * FROM candidates WHERE run_id = ? AND scoreable = ?")
        compiled_sqlite = str(text(statement).compile(dialect=sqlite.dialect()))
        compiled_postgres = str(text(statement).compile(dialect=postgresql.dialect()))
        assert "run_id" in compiled_sqlite
        assert "run_id" in compiled_postgres


class TestNoDialectOnlySyntaxRemains:
    """Read from the store sources, because that is where the SQL lives."""

    @pytest.mark.parametrize("path", STORES, ids=lambda p: p.name)
    def test_no_sqlite_only_constructs(self, path: Path) -> None:
        """Each of these has a portable equivalent that is already in use.

        `INSERT OR IGNORE` → `ON CONFLICT DO NOTHING` (SQLite 3.24+, Postgres).
        `last_insert_rowid()` → `RETURNING id` (SQLite 3.35+, Postgres).
        """
        source = path.read_text(encoding="utf-8")
        forbidden = {
            "INSERT OR IGNORE": "use ON CONFLICT DO NOTHING",
            "INSERT OR REPLACE": "use ON CONFLICT DO UPDATE",
            "last_insert_rowid": "use RETURNING id",
            "AUTOINCREMENT": "Postgres has no such keyword",
            "julianday": "not a Postgres function",
            "GROUP_CONCAT": "Postgres spells it string_agg",
        }
        found = [f"{token} ({why})" for token, why in forbidden.items() if token in source]
        assert not found, f"{path.name}: {found}"

    def test_no_store_reaches_for_the_driver(self) -> None:
        """The driver is `connection.py`'s business and nothing else's (4)."""
        offenders = [
            p.name
            for p in STORES
            if re.search(r"^import sqlite3|^from sqlite3", p.read_text(encoding="utf-8"), re.M)
        ]
        assert not offenders
