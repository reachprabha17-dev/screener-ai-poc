"""Dialect hygiene in the stores' SQL (spec 12.2).

**This file is the cheap check, not the real one.** The real one is that every
storage and service test now runs against a live Postgres — which is what
finally caught three dialect bugs that had shipped: `MAX(a, b)` (a scalar in
SQLite, an aggregate here), `cacheable = 0` against a real boolean column, and a
claim statement that handed one job to two workers.

An earlier version of this file was a six-token denylist and it passed
throughout, because a denylist only ever catches the differences somebody already
thought of. It could not see any of those three. Keeping a lexical check is still
worth the twenty lines — it fails in milliseconds and names the file — but it is
a smoke alarm, not the fire brigade, and nothing should be added here in
preference to a test that executes the statement.
"""

import ast
import re
from pathlib import Path

import pytest

from screener.storage.uow import to_named


def sql_literals(path: Path) -> list[str]:
    """Every string constant in a module that is not a docstring.

    Parsed rather than grepped. The prose in this codebase quotes broken SQL on
    purpose when explaining why it was broken — the comment above
    `purge_candidate` says exactly what `cacheable = 0` did — and a line-based
    scan cannot tell that from the statement itself.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    docstrings = {
        node.body[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
        and isinstance(node.body[0].value.value, str)
    }
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and node not in docstrings
    ]


STORE_DIR = Path(__file__).resolve().parent.parent / "screener" / "storage"
STORES = sorted(STORE_DIR.glob("*_store.py"))

# Every boolean column in the schema. Postgres has a real boolean type and will
# not compare it to an integer; SQLite stored 0/1 and accepted either, so this
# reads as correct right up until it runs.
BOOLEAN_COLUMNS = (
    "active",
    "verification_enabled",
    "redaction_on",
    "must_haves_met",
    "scoreable",
    "review_required",
    "cacheable",
    "verified",
    "negation_suspected",
    "absence_confirmed",
    "evidence_irrelevant",
)

INTEGER_BOOLEAN = re.compile(
    rf"\b({'|'.join(BOOLEAN_COLUMNS)})\s*(?:=|!=|<>)\s*[01]\b",
)


class TestPlaceholderTranslation:
    """The stores write `?`; psycopg wants `%s`. `to_named` is the bridge."""

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


class TestNoDialectSlipsRemain:
    """Read from the store sources, because that is where the SQL lives."""

    @pytest.mark.parametrize("path", STORES, ids=lambda p: p.name)
    def test_no_integer_boolean_comparisons(self, path: Path) -> None:
        """`cacheable = 0` is a type error here, and it broke the erasure path.

        The statement aborted, so `purge_candidate` erased nothing at all — while
        the caller went on reporting a successful purge. Bind a Python `bool`, or
        write `TRUE`/`FALSE`.
        """
        offenders = [text for text in sql_literals(path) if INTEGER_BOOLEAN.search(text)]
        assert not offenders, f"{path.name}: bind a bool instead — {offenders}"

    @pytest.mark.parametrize("path", STORES, ids=lambda p: p.name)
    def test_no_sqlite_only_constructs(self, path: Path) -> None:
        """Each of these has a Postgres spelling that is already in use."""
        source = "\n".join(sql_literals(path))
        forbidden = {
            "INSERT OR IGNORE": "use ON CONFLICT DO NOTHING",
            "INSERT OR REPLACE": "use ON CONFLICT DO UPDATE",
            "last_insert_rowid": "use RETURNING id",
            "AUTOINCREMENT": "Postgres has no such keyword",
            "julianday": "not a Postgres function",
            "GROUP_CONCAT": "Postgres spells it string_agg",
            "sqlite_master": "read pg_tables or information_schema",
            "PRAGMA": "not a Postgres statement",
        }
        found = [f"{token} ({why})" for token, why in forbidden.items() if token in source]
        assert not found, f"{path.name}: {found}"

    @pytest.mark.parametrize("path", STORES, ids=lambda p: p.name)
    def test_max_is_not_used_as_a_two_argument_function(self, path: Path) -> None:
        """`MAX(a, b)` is scalar in SQLite and an aggregate in Postgres.

        It raised `function max(integer, integer) does not exist` from
        `jobs_store.release`, and nothing noticed because nothing called that
        function. `GREATEST`/`LEAST` are the scalar forms here.
        """
        source = "\n".join(sql_literals(path))
        two_arg = re.findall(r"\b(?:MAX|MIN)\s*\([^)\n]*,[^)\n]*\)", source)
        assert not two_arg, f"{path.name}: use GREATEST/LEAST — {two_arg}"

    def test_no_store_reaches_for_a_driver(self) -> None:
        """The driver is `connection.py`'s business and nothing else's (4)."""
        offenders = [
            p.name
            for p in STORES
            if re.search(
                r"^import (sqlite3|psycopg)|^from (sqlite3|psycopg)",
                p.read_text(encoding="utf-8"),
                re.M,
            )
        ]
        assert not offenders
