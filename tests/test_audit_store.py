"""Audit log search (spec 15.4).

**Runs against a throwaway schema, like every other store test.** An earlier
version of this file used `unit_of_work()` directly, which resolved the *real*
database. `audit_log` is append-only by database trigger, so those rows could
never be cleaned up: each run added five permanent entries to the working
database and the assertions had to be weakened to `>=` to tolerate the ones left
by previous runs. Isolating the schema is what lets the counts below be exact,
which is the whole point of asserting on them.

The `db` and `uow_factory` fixtures come from `conftest.py` — see its header for
why `TRUNCATE` rather than `DELETE` is what resets these rows between tests.
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from screener.storage import audit_store
from screener.storage.uow import UnitOfWork


def test_search_filters_by_each_field(uow_factory: Callable[[], UnitOfWork]) -> None:
    with uow_factory() as tx:
        audit_store.append(tx, "u-1", "create_run", "run", "r-1", {"queued": 3})
        audit_store.append(tx, "u-2", "sign_off_run", "run", "r-2", None)
        audit_store.append(tx, "u-1", "approve_rubric", "rubric", "rub-1", {"v": 2})

    with uow_factory() as tx:
        rows, total = audit_store.search(tx, actor_id="u-1")
        assert total == 2
        assert {r["action"] for r in rows} == {"create_run", "approve_rubric"}

        rows, total = audit_store.search(tx, action="sign_off_run")
        assert total == 1
        assert rows[0]["actor_id"] == "u-2"

        rows, total = audit_store.search(tx, entity="run")
        assert total == 2

        rows, total = audit_store.search(tx, entity_id="rub-1")
        assert total == 1

        # Filters combine as AND, not OR.
        rows, total = audit_store.search(tx, actor_id="u-1", entity="run")
        assert total == 1
        assert rows[0]["entity_id"] == "r-1"

        # No filters is everything.
        _, total = audit_store.search(tx)
        assert total == 3


def test_search_returns_newest_first(uow_factory: Callable[[], UnitOfWork]) -> None:
    """An auditor opens the log to see what just happened, not what happened first."""
    with uow_factory() as tx:
        for i in range(5):
            audit_store.append(tx, "u-1", f"action_{i}", "run", f"r-{i}", None)

    with uow_factory() as tx:
        rows, _ = audit_store.search(tx)
    assert [r["action"] for r in rows] == [f"action_{i}" for i in reversed(range(5))]


def test_search_paginates_with_a_stable_total(uow_factory: Callable[[], UnitOfWork]) -> None:
    """`total` is what tells a client the last page is short rather than empty."""
    with uow_factory() as tx:
        for i in range(12):
            audit_store.append(tx, "u-1", f"action_{i:02d}", "run", "r-1", None)

    with uow_factory() as tx:
        first, total = audit_store.search(tx, limit=5, offset=0)
        second, total_2 = audit_store.search(tx, limit=5, offset=5)
        last, total_3 = audit_store.search(tx, limit=5, offset=10)

    assert total == total_2 == total_3 == 12
    assert len(first) == len(second) == 5
    assert len(last) == 2
    ids = [r["action"] for r in first + second + last]
    assert len(set(ids)) == 12, "pages overlapped or skipped rows"


def test_search_bounds_by_timestamp(uow_factory: Callable[[], UnitOfWork]) -> None:
    with uow_factory() as tx:
        audit_store.append(tx, "u-1", "create_run", "run", "r-1", None)

    past = (datetime.now(UTC) - timedelta(days=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()

    with uow_factory() as tx:
        _, total = audit_store.search(tx, since=past, until=future)
        assert total == 1
        _, total = audit_store.search(tx, since=future)
        assert total == 0
        _, total = audit_store.search(tx, until=past)
        assert total == 0


def test_a_null_detail_round_trips_as_none(uow_factory: Callable[[], UnitOfWork]) -> None:
    """Several actions are audited with no detail; reading them back must work.

    Regression test: `AuditEntry.detail` was a required dict, so `GET /audit`
    returned 500 on any row whose `detail_json` was NULL — 15 of 90 rows in a
    working database. Every existing fixture happened to supply a detail, so
    nothing caught it until the page was opened against real data.
    """
    with uow_factory() as tx:
        audit_store.append(tx, "u-1", "sign_off_run", "run", "r-1", None)

    with uow_factory() as tx:
        rows, _ = audit_store.search(tx)
    assert rows[0]["detail"] is None


def test_search_treats_filter_values_as_data_never_as_sql(
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    """The `where` clause is assembled from fragments; the values are bound.

    `search` builds its SQL with an f-string and carries a `# noqa: S608`, so the
    property that makes that safe — no caller value ever reaching the string —
    needs a test rather than a comment.
    """
    with uow_factory() as tx:
        audit_store.append(tx, "u-1", "create_run", "run", "r-1", None)
        audit_store.append(tx, "u-2", "create_run", "run", "r-2", None)

    attacks = [
        "' OR '1'='1",
        "x'; DROP TABLE audit_log; --",
        "' UNION SELECT ts,actor_id,action,entity,entity_id,detail_json FROM audit_log --",
        "%",  # a LIKE wildcard must stay literal
        "_",
    ]
    for attack in attacks:
        with uow_factory() as tx:
            rows, total = audit_store.search(tx, actor_id=attack)
        assert (rows, total) == ([], 0), attack

    with uow_factory() as tx:
        _, total = audit_store.search(tx)
    assert total == 2, "the table was modified by a filter value"
