"""Shared fixtures and builders.

**The suite runs against a real Postgres, in a schema of its own.**

It used to run against a fresh SQLite file per test — `tmp_path / "screener.db"`
— and that stopped working the moment the backend changed, silently and in the
worst direction: `database_url()` ignores a path argument once the backend is not
sqlite, so every one of those fixtures pointed at the *deployment* database. Tests
shared one database with each other and with the running application.

Isolation is a schema rather than a database because the `screener` role owns the
database and has `CREATE` on it, but has no `CREATEDB` privilege — a schema is the
largest unit the suite can create without an administrator being involved. Both
SQLAlchemy and yoyo reach it through the same `options=-csearch_path=…` parameter
on the URL, so the migration runner and the application land in the same place.

Tests are separated from each other by `TRUNCATE … RESTART IDENTITY CASCADE`
between cases. Not `DELETE`: the `audit_log` append-only triggers are `FOR EACH
ROW` on UPDATE and DELETE, and they correctly refuse it. `TRUNCATE` fires only
statement-level triggers, so it passes — which is also a standing check that the
triggers are doing their job, because the day `DELETE` starts working there, the
control is gone.
"""

import os
from collections.abc import Callable, Iterator

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from config.settings import settings
from screener.models import Candidate, Criterion, Rubric, ScoredCriterion, Verdict
from screener.storage.connection import apply_migrations, dispose_engine, engine
from screener.storage.uow import UnitOfWork

# yoyo's own bookkeeping. Truncating it would report every migration as pending
# on the next test, and the schema gate would refuse to start.
_YOYO_PREFIXES = ("yoyo", "_yoyo")


def _schema_url(base: str, schema: str) -> str:
    """`base`, pinned to `schema` for every connection made through it.

    libpq's `options` is the one setting that both SQLAlchemy's psycopg dialect
    and yoyo's carry through unchanged, which is what keeps the migration runner
    and the application in the same schema without either being told separately.
    """
    scoped = make_url(base).update_query_dict({"options": f"-csearch_path={schema}"})
    return scoped.render_as_string(hide_password=False)


@pytest.fixture(scope="session", autouse=True)
def _test_worker_identity() -> Iterator[None]:
    """Give the suite a worker name of its own.

    A worker now claims its `worker_id` exclusively and refuses to start if
    another live process holds it, which is what stops two workers reclaiming
    each other's in-flight jobs. The default name is the hostname — so without
    this, every test that starts a worker fails whenever the development daemon
    happens to be running, and passes when it is not. Tests must not depend on
    which services are up.

    Set in the environment as well as on `settings`, because several tests drive
    a real subprocess and it has to agree with the parent.
    """
    previous = os.environ.get("WORKER_ID")
    name = f"screener-test-{os.getpid()}"
    os.environ["WORKER_ID"] = name
    settings.worker_id = name
    yield
    if previous is None:
        os.environ.pop("WORKER_ID", None)
    else:
        os.environ["WORKER_ID"] = previous


@pytest.fixture(scope="session")
def db_url() -> Iterator[str]:
    """A migrated schema of this suite's own, torn down at the end of the run.

    Session-scoped: applying the schema is the expensive part and nothing in it
    is test-specific. The pid is in the name so two suites on one host — a watch
    process and a terminal, say — do not share a schema.
    """
    base = settings.db_url
    if not base:
        pytest.skip("DB_URL is not set; the suite needs a Postgres to run against")

    schema = f"screener_test_{os.getpid()}"
    with engine().connect() as connection:
        connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()

    scoped = _schema_url(base, schema)
    settings.db_url = scoped
    dispose_engine()
    apply_migrations()

    try:
        yield scoped
    finally:
        dispose_engine()
        settings.db_url = base
        dispose_engine()
        with engine().connect() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


@pytest.fixture
def db(db_url: str) -> str:
    """An empty database for one test. Returns the URL, for subprocesses.

    Anything that used to receive a `Path` to a SQLite file receives this
    instead — a child process is pointed at the schema by putting it in the
    environment as `DB_URL`, which is the same route the deployment uses.
    """
    with engine().connect() as connection:
        tables = [
            row[0]
            for row in connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
            )
            if not row[0].startswith(_YOYO_PREFIXES)
        ]
        if tables:
            targets = ", ".join(f'"{name}"' for name in tables)
            connection.execute(text(f"TRUNCATE {targets} RESTART IDENTITY CASCADE"))  # noqa: S608
        connection.commit()
    return db_url


@pytest.fixture
def uow(db: str) -> UnitOfWork:
    """One unit of work at a time, off the shared pool — as production does it."""
    return UnitOfWork()


@pytest.fixture
def uow_factory(db: str) -> Iterator[Callable[[], UnitOfWork]]:
    """The factory the service layer takes. Pooled, so it is thread-safe.

    A single pinned connection would test a topology the deployment never runs:
    FastAPI dispatches sync handlers onto a threadpool, and the pool is what makes
    that work.
    """
    yield UnitOfWork
    dispose_engine()


def child_env(db_url: str) -> dict[str, str]:
    """Environment for a subprocess that must reach the same test schema."""
    return {**os.environ, "DB_URL": db_url}


# Criterion text has to be *real*, not a placeholder.
#
# It previously read `f"criterion {i}"`, which broke the moment 10.5(c) started
# checking that evidence is about the criterion it was offered for: a quote about
# payment systems genuinely is not about "criterion C1", so the check fired on
# every fixture. The check was right and the fixture was fiction.
#
# This default shares vocabulary with the resume text used across the suite, so
# a rubric built without explicit text still behaves like a real one. Tests that
# care about the wording pass their own.
DEFAULT_CRITERION_TEXT = "Backend engineering experience"


def make_rubric(*specs: tuple[str, bool, int] | tuple[str, bool, int, str]) -> Rubric:
    """Build a rubric from ``(id, must_have, weight)`` or ``(id, must_have, weight, text)``.

    The contract enforces 4–12 criteria, so callers pass at least four.
    """
    return Rubric(
        id="r1",
        position_id="p1",
        version=1,
        created_by="tester",
        criteria=[
            Criterion(
                id=spec[0],
                text=spec[3] if len(spec) == 4 else DEFAULT_CRITERION_TEXT,
                must_have=spec[1],
                weight=spec[2],
            )
            for spec in specs
        ],
    )


def scored(criterion: Criterion, verdict: Verdict, *, verified: bool = True) -> ScoredCriterion:
    return ScoredCriterion(
        id=criterion.id,
        verdict=verdict,
        model_verdict=verdict,
        evidence="evidence" if verdict != "none" else "not found",
        verified=verified,
        match_ratio=1.0 if verified else 0.0,
        longest_span=5 if verified else 0,
        weight=criterion.weight,
        must_have=criterion.must_have,
    )


def score_all(rubric: Rubric, *verdicts: Verdict) -> list[ScoredCriterion]:
    return [scored(c, v) for c, v in zip(rubric.criteria, verdicts, strict=True)]


def candidate(
    name: str,
    *,
    score: float | None = None,
    must_haves_met: bool = True,
    scoreable: bool = True,
    review_required: bool = False,
) -> Candidate:
    return Candidate(
        run_id="run1",
        filename=name,
        file_sha256=name,
        score=score,
        must_haves_met=must_haves_met,
        scoreable=scoreable,
        review_required=review_required,
    )
