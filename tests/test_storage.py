"""Persistence, cache, transactions and erasure (spec 12, build gate 20 step 10).

The gate names four things: cache hit/miss across all 8 key fields, transient
flags not cached, audit triggers rejecting UPDATE/DELETE, and override+audit
atomicity. Each has its own section below.

Every test runs against a real SQLite file with the real migration applied. An
in-memory fake would prove nothing about the triggers, the partial index, or WAL
— which is where all the behaviour under test actually lives.
"""

import os
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from yoyo.exceptions import LockTimeout

from config.settings import settings
from screener.models import (
    Actor,
    Candidate,
    Criterion,
    EscalationReason,
    Flag,
    MatchBlock,
    Position,
    RedFlag,
    Rubric,
    ScoredCriterion,
    Span,
    now,
)
from screener.ports import CacheKey
from screener.storage import (
    audit_store,
    positions_store,
    results_store,
    rubrics_store,
    runs_store,
)
from screener.storage.connection import (
    MIGRATIONS_ROOT,
    PendingMigrationsError,
    apply_migrations,
    connect,
    migrations_dir,
    pending_migrations,
    require_current_schema,
)
from screener.storage.uow import UnitOfWork

ACTOR = Actor(id="poc-operator", display_name="PoC Operator", roles=frozenset({"admin"}))


@pytest.fixture
def db(tmp_path: Path) -> Iterator[Path]:
    path = tmp_path / "screener.db"
    apply_migrations(path)
    yield path


@pytest.fixture
def uow(db: Path) -> Iterator[UnitOfWork]:
    connection = connect(db)
    try:
        yield UnitOfWork(connection)
    finally:
        connection.close()


def seed(uow: UnitOfWork) -> None:
    """A user, a position and a rubric — the FK chain everything else needs."""
    with uow as tx:
        positions_store.seed_user(tx, ACTOR.id, ACTOR.display_name)
        positions_store.create(
            tx,
            Position(
                id="p1",
                reference="REQ-1",
                title="Senior Backend Engineer",
                jd_text="We need a backend engineer.",
                created_by=ACTOR.id,
                created_at=now(),
            ),
        )
        rubrics_store.create(tx, rubric())


def rubric(version: int = 1, rubric_id: str = "r1") -> Rubric:
    return Rubric(
        id=rubric_id,
        position_id="p1",
        version=version,
        created_by=ACTOR.id,
        criteria=[
            Criterion(id="C1", text="5+ years backend", must_have=True, weight=3),
            Criterion(id="C2", text="Kubernetes", weight=2),
            Criterion(id="C3", text="Go", weight=1),
            Criterion(id="C4", text="Mentoring", weight=1),
        ],
    )


def make_run(uow: UnitOfWork, run_id: str = "run1") -> None:
    with uow as tx:
        runs_store.create(
            tx,
            run_id=run_id,
            position_id="p1",
            rubric_id="r1",
            folder="data/resumes/REQ-1",
            created_by=ACTOR.id,
            judge_model="granite4.1:8b",
            judge_digest="sha256:aaa",
            prompt_hash="p" * 64,
            redaction_on=True,
            num_ctx=8192,
            num_predict=1536,
            seed=42,
            app_version="v0.1.0",
        )


def key(**overrides: object) -> CacheKey:
    base = {
        "file_sha256": "abc123",
        "position_id": "p1",
        "rubric_hash": "r" * 64,
        "judge_digest": "sha256:aaa",
        "prompt_hash": "p" * 64,
        "redaction_on": True,
        "num_ctx": 8192,
        "app_version": "v0.1.0",
    }
    base.update(overrides)
    return CacheKey(**base)  # type: ignore[arg-type]


def candidate(*, flags: list[Flag] | None = None, score: float | None = 7.8) -> Candidate:
    return Candidate(
        run_id="run1",
        filename="asha_nair.pdf",
        file_sha256="abc123",
        score=score,
        band="A" if score else None,
        must_haves_met=score is not None,
        criteria=[
            ScoredCriterion(
                id="C1",
                verdict="strong",
                model_verdict="strong",
                evidence="7 years backend engineer",
                verified=True,
                match_ratio=0.95,
                longest_span=4,
                weight=3,
                must_have=True,
            )
        ],
        notable_strengths=["Payments at scale"],
        red_flags=[RedFlag.UNVERIFIABLE_CLAIM],
        summary="Seven years of backend engineering.",
        flags=flags or [],
        scoreable=score is not None,
        scored_at=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
    )


# --- migrations as a startup gate --------------------------------------------


def test_migrations_apply_and_then_report_nothing_pending(tmp_path: Path) -> None:
    path = tmp_path / "fresh.db"

    assert apply_migrations(path) == [
        "0001.initial-schema",
        "0002.judge-digest-rename",
        "0003.v6-schema",
        "0004.position-reference-unique-while-open",
        "0005.backfill-run-file-counts",
        "0006.evidence-irrelevant-column",
    ]
    assert pending_migrations(path) == []
    require_current_schema(path)


def _dead_pid() -> int:
    """A pid guaranteed to belong to no running process."""
    child = subprocess.Popen([sys.executable, "-c", "pass"])  # noqa: S603
    child.wait(timeout=10)
    return child.pid


def test_a_lock_row_left_by_a_dead_process_is_cleared_automatically(tmp_path: Path) -> None:
    """Regression. `yoyo` acquires its advisory lock with a retrying connection
    but releases it with a single unretried `DELETE`
    (`DatabaseBackend._delete_lock_row`), over a connection that — unlike ours —
    carries no `busy_timeout`. Api and worker each call `require_current_schema`
    independently at their own startup, so two of those landing within the same
    instant is enough for one release to hit "database is locked" and leave the
    row behind — reporting a current schema as permanently pending until a
    human runs `yoyo break-lock` by hand.

    Self-healed here: the row is cleared the next time anything touches
    migrations, as long as the pid that left it is provably no longer running.
    """
    path = tmp_path / "fresh.db"
    apply_migrations(path)

    with connect(path) as connection:
        connection.execute(
            "INSERT INTO yoyo_lock (locked, ctime, pid) VALUES (1, datetime('now'), ?)",
            (_dead_pid(),),
        )

    # Would raise LockTimeout without the self-heal.
    assert pending_migrations(path) == []
    require_current_schema(path)


def test_a_lock_row_held_by_a_live_process_is_left_alone(tmp_path: Path) -> None:
    """The other half: a lock genuinely in use must still block (12.1) — the
    self-heal only clears rows nobody could possibly still be holding.
    """
    from screener.storage.connection import _backend  # noqa: PLC0415 — test-only reach-in

    path = tmp_path / "fresh.db"
    apply_migrations(path)

    with connect(path) as connection:
        connection.execute(
            "INSERT INTO yoyo_lock (locked, ctime, pid) VALUES (1, datetime('now'), ?)",
            (os.getpid(),),
        )

    with pytest.raises(LockTimeout):
        with _backend(path).lock(timeout=0.5):
            pass


def test_every_migration_lives_where_yoyo_will_read_it() -> None:
    """`read_migrations` globs one directory and does not recurse (12.3).

    A migration left in `migrations/` rather than `migrations/<backend>/` is not
    an error and produces no warning — it is simply never read. The database
    then reports itself current while the code runs against the previous schema,
    which is the one failure mode the startup gate cannot catch, because the gate
    asks the same question of the same directory.
    """
    stray = sorted(p.name for p in MIGRATIONS_ROOT.glob("*.sql"))
    assert stray == [], f"invisible to yoyo — move under migrations/<backend>/: {stray}"

    assert migrations_dir() == MIGRATIONS_ROOT / settings.db_backend
    assert sorted(p.name for p in migrations_dir().glob("*.sql"))


def test_an_unmigrated_database_refuses_to_start(tmp_path: Path) -> None:
    """A schema/code mismatch on candidate data is an integrity incident (12.1).

    Refusing beats warning, and refusing beats auto-migrating: applying a schema
    change as a side effect of process start runs it at an unplanned time on a
    database nobody has backed up.
    """
    with pytest.raises(PendingMigrationsError, match="pending"):
        require_current_schema(tmp_path / "empty.db")


def test_pragmas_are_set_per_connection(db: Path) -> None:
    """`foreign_keys` is OFF by default and is per-connection, not per-database.

    A connection that forgets it silently accepts verdict rows pointing at
    candidates that do not exist.
    """
    connection = connect(db)
    try:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    finally:
        connection.close()


def test_foreign_keys_are_actually_enforced(uow: UnitOfWork) -> None:
    seed(uow)
    with pytest.raises(sqlite3.IntegrityError), uow as tx:
        runs_store.create(
            tx,
            run_id="bad",
            position_id="does-not-exist",
            rubric_id="r1",
            folder="f",
            created_by=ACTOR.id,
            judge_model="m",
            judge_digest="d",
            prompt_hash="h",
            redaction_on=True,
            num_ctx=8192,
            num_predict=1536,
            seed=42,
            app_version="v",
        )


# --- gate: cache hit/miss across all 8 key fields ----------------------------


def test_cache_hit_on_an_identical_key(uow: UnitOfWork) -> None:
    seed(uow)
    make_run(uow)
    with uow as tx:
        results_store.save(tx, "run1", candidate(), key())

    with uow as tx:
        hit = results_store.get_cached(tx, key())

    assert hit is not None
    assert hit.score == 7.8
    assert hit.criteria[0].evidence == "7 years backend engineer"
    assert hit.red_flags == [RedFlag.UNVERIFIABLE_CLAIM]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("file_sha256", "different"),
        ("position_id", "p2"),
        ("rubric_hash", "z" * 64),
        ("judge_digest", "sha256:bbb"),
        ("verifier_digest", "sha256:ccc"),
        ("prompt_hash", "q" * 64),
        ("redaction_on", False),
        ("num_ctx", 4096),
        ("app_version", "v0.2.0"),
    ],
)
def test_every_key_field_causes_a_miss(uow: UnitOfWork, field: str, value: object) -> None:
    """All nine, individually.

    Dropping any one means a change that alters the output — a re-pulled model,
    an edited prompt, redaction toggled off, a swapped verifier — silently serves
    the old verdict. `verifier_digest` joined the key in v6 because verification
    became part of the stored result: without it, changing the verifier serves
    back the previous one's flags and escalations.
    """
    seed(uow)
    make_run(uow)
    with uow as tx:
        results_store.save(tx, "run1", candidate(), key())

    with uow as tx:
        assert results_store.get_cached(tx, key(**{field: value})) is None


def test_cache_ignores_run_id_which_is_what_makes_resumption_work(uow: UnitOfWork) -> None:
    """A batch killed at 50% resumes instead of re-judging 500 resumes."""
    seed(uow)
    make_run(uow, "run1")
    make_run(uow, "run2")
    with uow as tx:
        results_store.save(tx, "run1", candidate(), key())

    with uow as tx:
        assert results_store.get_cached(tx, key()) is not None


def test_position_id_scoping_stops_verdicts_leaking_across_requisitions(
    uow: UnitOfWork,
) -> None:
    """Two recruiters screening the same person for different roles.

    They must not share a judgment — the criteria differ, so the verdict means
    something different.
    """
    seed(uow)
    make_run(uow)
    with uow as tx:
        results_store.save(tx, "run1", candidate(), key(position_id="p1"))

    with uow as tx:
        assert results_store.get_cached(tx, key(position_id="p-other")) is None


def test_cache_lookup_uses_the_partial_index(uow: UnitOfWork) -> None:
    """Not a performance test — a correctness one.

    Transient rows are excluded by `WHERE cacheable = 1` *in the index itself*.
    If the planner fell back to a table scan, exclusion would depend on the SQL
    predicate alone, and the structural guarantee 12.5 describes would quietly
    become an ordinary filter that a future query could omit.
    """
    where = " AND ".join(f"{column} = ?" for column in results_store._CACHE_COLUMNS)  # noqa: SLF001
    with uow as tx:
        plan = tx.execute(
            f"EXPLAIN QUERY PLAN SELECT * FROM candidates WHERE {where} AND cacheable = 1",  # noqa: S608
            tuple("x" for _ in results_store._CACHE_COLUMNS),  # noqa: SLF001
        ).fetchall()

    detail = " ".join(row["detail"] for row in plan)
    assert "idx_cache" in detail
    assert "SCAN" not in detail


def test_the_cache_index_is_declared_partial(uow: UnitOfWork) -> None:
    with uow as tx:
        rows = tx.execute("SELECT name, partial FROM pragma_index_list('candidates')").fetchall()

    partial = {row["name"]: row["partial"] for row in rows}
    assert partial["idx_cache"] == 1


# --- gate: transient flags are never cached ----------------------------------


@pytest.mark.parametrize(
    "flag",
    [Flag.LLM_ERROR, Flag.SCHEMA_INVALID, Flag.PARSER_TIMEOUT, Flag.PARSER_CRASHED],
)
def test_transient_failures_are_invisible_to_the_cache(uow: UnitOfWork, flag: Flag) -> None:
    """The 12.5 trap.

    Cached, a single network blip comes back on resume as a permanent verdict —
    a real applicant sidelined forever by something that looks like a legitimate
    result.
    """
    seed(uow)
    make_run(uow)
    with uow as tx:
        results_store.save(tx, "run1", candidate(flags=[flag], score=None), key())

    with uow as tx:
        assert results_store.get_cached(tx, key()) is None


def test_transient_rows_are_still_stored_for_review(uow: UnitOfWork) -> None:
    """Invisible to *lookup*, not absent.

    A reviewer must still see that the file was attempted and failed; silently
    dropping it is how someone vanishes from a run.
    """
    seed(uow)
    make_run(uow)
    with uow as tx:
        results_store.save(tx, "run1", candidate(flags=[Flag.LLM_ERROR], score=None), key())

    with uow as tx:
        stored = results_store.list_for_run(tx, "run1")

    assert len(stored) == 1
    assert stored[0].flags == [Flag.LLM_ERROR]
    assert stored[0].score is None  # never 0.0


@pytest.mark.parametrize(
    "flag", [Flag.BUDGET_EXCEEDED, Flag.EVIDENCE_UNVERIFIED, Flag.INPUT_REJECTED]
)
def test_deterministic_failures_are_cacheable(uow: UnitOfWork, flag: Flag) -> None:
    """These describe the document and will be true next time too."""
    seed(uow)
    make_run(uow)
    with uow as tx:
        results_store.save(tx, "run1", candidate(flags=[flag], score=None), key())

    with uow as tx:
        assert results_store.get_cached(tx, key()) is not None


# --- gate: audit is append-only, enforced by the database --------------------


def test_audit_rows_cannot_be_updated(uow: UnitOfWork) -> None:
    """A convention survives exactly until someone writes a cleanup script."""
    seed(uow)
    with uow as tx:
        audit_store.append_for(tx, ACTOR, "override", "candidate", "1")

    with pytest.raises(sqlite3.IntegrityError, match="append-only"), uow as tx:
        tx.execute("UPDATE audit_log SET action = 'nothing happened'")


def test_audit_rows_cannot_be_deleted(uow: UnitOfWork) -> None:
    seed(uow)
    with uow as tx:
        audit_store.append_for(tx, ACTOR, "purge", "candidate", "1")

    with pytest.raises(sqlite3.IntegrityError, match="append-only"), uow as tx:
        tx.execute("DELETE FROM audit_log")


def test_audit_records_the_actor_and_detail(uow: UnitOfWork) -> None:
    seed(uow)
    with uow as tx:
        audit_store.append_for(tx, ACTOR, "override", "candidate", "7", {"decision": "advance"})

    with uow as tx:
        entries = audit_store.list_for_entity(tx, "candidate", "7")

    assert entries[0]["actor_id"] == ACTOR.id
    assert entries[0]["detail"] == {"decision": "advance"}


def test_system_events_may_have_no_actor(uow: UnitOfWork) -> None:
    """A worker reclaiming an orphaned job has no human behind it.

    Inventing a synthetic user to satisfy NOT NULL would make those
    indistinguishable from real decisions.
    """
    seed(uow)
    with uow as tx:
        audit_store.append(tx, None, "reclaim_orphaned", "job", "12")

    with uow as tx:
        assert audit_store.recent(tx)[0]["actor_id"] is None


# --- gate: multi-table writes are atomic -------------------------------------


def test_override_and_audit_commit_together(uow: UnitOfWork) -> None:
    seed(uow)
    make_run(uow)
    with uow as tx:
        candidate_id = results_store.save(tx, "run1", candidate(), key())

    with uow as tx:
        tx.execute(
            "INSERT INTO overrides (candidate_id, actor_id, old_score, old_band, "
            "new_decision, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                candidate_id,
                ACTOR.id,
                7.8,
                "A",
                "advance",
                "strong payments background",
                now().isoformat(),
            ),
        )
        audit_store.append_for(tx, ACTOR, "override", "candidate", str(candidate_id))

    with uow as tx:
        assert tx.execute("SELECT COUNT(*) AS n FROM overrides").fetchone()["n"] == 1
        assert len(audit_store.list_for_entity(tx, "candidate", str(candidate_id))) == 1


def test_a_failure_rolls_back_both_writes(uow: UnitOfWork) -> None:
    """Without one boundary you eventually hold an override with no audit trail.

    A record of a decision affecting a candidate, with no record of who made it.
    """
    seed(uow)
    make_run(uow)
    with uow as tx:
        candidate_id = results_store.save(tx, "run1", candidate(), key())

    with pytest.raises(sqlite3.IntegrityError), uow as tx:
        tx.execute(
            "INSERT INTO overrides (candidate_id, actor_id, old_score, old_band, "
            "new_decision, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, ACTOR.id, 7.8, "A", "advance", "reason", now().isoformat()),
        )
        audit_store.append_for(tx, ACTOR, "override", "candidate", str(candidate_id))
        # Violates the CHECK constraint, aborting the whole transaction.
        tx.execute(
            "INSERT INTO overrides (candidate_id, actor_id, new_decision, reason, created_at) "
            "VALUES (?, ?, 'teleport', 'nope', ?)",
            (candidate_id, ACTOR.id, now().isoformat()),
        )

    with uow as tx:
        assert tx.execute("SELECT COUNT(*) AS n FROM overrides").fetchone()["n"] == 0
        assert audit_store.list_for_entity(tx, "candidate", str(candidate_id)) == []


def test_unit_of_work_refuses_to_nest(uow: UnitOfWork) -> None:
    """SQLite has no nested transactions.

    An inner `with` that appeared to commit would be committing the outer one's
    partial work.
    """
    with uow as _tx, pytest.raises(RuntimeError, match="not re-entrant"):
        with uow:
            pass


# --- erasure -----------------------------------------------------------------


def test_purge_clears_identifying_content_but_keeps_statistics(uow: UnitOfWork) -> None:
    seed(uow)
    make_run(uow)
    with uow as tx:
        results_store.save(tx, "run1", candidate(), key())

    with uow as tx:
        results_store.purge_candidate(tx, "abc123")

    with uow as tx:
        purged = results_store.list_for_run(tx, "run1")[0]

    assert "asha" not in purged.filename.casefold()
    assert purged.summary == ""
    assert purged.notable_strengths == []
    assert purged.criteria[0].evidence == ""
    # Retained: a purged candidate is still countable in an escalation rate.
    assert purged.score == 7.8
    assert purged.band == "A"


def test_a_purged_candidate_is_removed_from_the_cache(uow: UnitOfWork) -> None:
    """Otherwise the next run serves the erased judgment straight back."""
    seed(uow)
    make_run(uow)
    with uow as tx:
        results_store.save(tx, "run1", candidate(), key())
    with uow as tx:
        results_store.purge_candidate(tx, "abc123")

    with uow as tx:
        assert results_store.get_cached(tx, key()) is None


# --- rubric versioning -------------------------------------------------------


def test_rubric_hash_covers_content_but_not_approval(uow: UnitOfWork) -> None:
    """Re-approving an unchanged rubric must not invalidate every judgment."""
    original = rubric()
    approved = original.model_copy(update={"approved_by": ACTOR.id, "approved_at": now()})

    assert original.content_hash == approved.content_hash


def test_editing_a_criterion_changes_the_hash() -> None:
    edited = rubric()
    edited.criteria[0].text = "8+ years backend"

    assert edited.content_hash != rubric().content_hash


def test_reordering_criteria_changes_the_hash() -> None:
    """Order is what the model sees, and reordering can change output."""
    reordered = rubric()
    reordered.criteria.reverse()

    assert reordered.content_hash != rubric().content_hash


def test_rubric_versions_are_unique_per_position(uow: UnitOfWork) -> None:
    seed(uow)
    with pytest.raises(sqlite3.IntegrityError), uow as tx:
        rubrics_store.create(tx, rubric(version=1, rubric_id="r2"))


def test_approval_is_recorded_and_gates_use(uow: UnitOfWork) -> None:
    seed(uow)
    with uow as tx:
        assert rubrics_store.is_approved(tx, "r1") is False
        rubrics_store.approve(tx, "r1", ACTOR)

    with uow as tx:
        assert rubrics_store.is_approved(tx, "r1") is True


def test_stored_rubric_round_trips(uow: UnitOfWork) -> None:
    seed(uow)
    with uow as tx:
        loaded = rubrics_store.get(tx, "r1")

    assert loaded is not None
    assert loaded.content_hash == rubric().content_hash
    assert loaded.total_weight == 7


# --- runs --------------------------------------------------------------------


def test_run_freezes_its_reproducibility_inputs(uow: UnitOfWork) -> None:
    """Months later, "why did this score 6.2" is answerable only from these."""
    seed(uow)
    make_run(uow)

    with uow as tx:
        record = runs_store.get(tx, "run1")

    assert record is not None
    assert record.judge_digest == "sha256:aaa"
    assert record.num_ctx == 8192
    assert record.redaction_on is True
    assert record.status == "pending"


def test_started_at_survives_a_resume(uow: UnitOfWork) -> None:
    """The ETA describes the run, not the latest attempt at it."""
    seed(uow)
    make_run(uow)
    with uow as tx:
        runs_store.set_status(tx, "run1", "running")
        first = tx.execute("SELECT started_at FROM runs WHERE id = 'run1'").fetchone()[0]
        runs_store.set_status(tx, "run1", "aborted")
        runs_store.set_status(tx, "run1", "running")
        second = tx.execute("SELECT started_at FROM runs WHERE id = 'run1'").fetchone()[0]

    assert first == second


def test_duplicate_detection_is_exact_hash_only(uow: UnitOfWork) -> None:
    """Byte-identical files only — documented as weak rather than implying more."""
    seed(uow)
    make_run(uow)
    with uow as tx:
        results_store.save(tx, "run1", candidate(), key())

    with uow as tx:
        assert results_store.find_duplicates(tx, "run1", "abc123") == 1
        assert results_store.find_duplicates(tx, "run1", "different") == 0


# --- v6: stored text, offsets, verification, decisions -----------------------


def test_the_stored_text_versions_survive_a_round_trip(uow: UnitOfWork) -> None:
    """12.6: `candidates` is a full-text store now, not a results table.

    Verification is not recomputable without it. Scoring is — change a weight and
    recompute from stored verdicts in milliseconds — but asking whether
    `evidence_match_ratio = 0.55` would cut the escalation rate requires the exact
    document each quote was matched against.
    """
    seed(uow)
    make_run(uow)
    stored = candidate().model_copy(
        update={
            "resume_text": "Asha Nair. Senior Backend Engineer.",
            "sent_text": "[REDACTED]. Senior Backend Engineer.",
            "redaction_map": [Span(src_start=10, src_end=35, dst_start=10, dst_end=35)],
        }
    )

    with uow as tx:
        results_store.save(tx, "run1", stored, key())

    with uow as tx:
        loaded = results_store.get_cached(tx, key())

    assert loaded is not None
    assert loaded.resume_text == stored.resume_text
    assert loaded.sent_text == stored.sent_text
    assert loaded.redaction_map == stored.redaction_map


def test_match_blocks_survive_a_round_trip(uow: UnitOfWork) -> None:
    """Without them a `match_ratio` of 0.42 is a number nobody can interrogate."""
    seed(uow)
    make_run(uow)
    blocks = [MatchBlock(ev_start=0, ev_end=7, doc_start=12, doc_end=19)]
    stored = candidate()
    stored.criteria[0] = stored.criteria[0].model_copy(
        update={"match_blocks": blocks, "negation_suspected": True}
    )

    with uow as tx:
        results_store.save(tx, "run1", stored, key())

    with uow as tx:
        loaded = results_store.get_cached(tx, key())

    assert loaded is not None
    assert loaded.criteria[0].match_blocks == blocks
    assert loaded.criteria[0].negation_suspected is True


def test_evidence_irrelevant_survives_a_round_trip(uow: UnitOfWork) -> None:
    """Without a real column this silently reverts to its Pydantic default
    (False) on every load — indistinguishable from a criterion that was never
    flagged, for one that was and is only pending a follow-up opinion."""
    seed(uow)
    make_run(uow)
    stored = candidate()
    stored.criteria[0] = stored.criteria[0].model_copy(update={"evidence_irrelevant": True})

    with uow as tx:
        results_store.save(tx, "run1", stored, key())

    with uow as tx:
        loaded = results_store.get_cached(tx, key())

    assert loaded is not None
    assert loaded.criteria[0].evidence_irrelevant is True


def test_save_verification_writes_phase_two_without_touching_the_score(uow: UnitOfWork) -> None:
    """1.9 enforced at the storage layer, not only in `reconcile_judge`.

    The column list in `save_verification` is what makes "the verifier never
    overrules" a property of the SQL that runs rather than of a pure function
    nobody re-checks.
    """
    seed(uow)
    make_run(uow)
    with uow as tx:
        candidate_id = results_store.save(tx, "run1", candidate(), key())

    verified = candidate().model_copy(
        update={
            "id": candidate_id,
            "score": 0.0,  # a deliberately wrong value: it must not be written
            "verification_status": "done",
            "review_required": True,
            "escalation_reasons": [EscalationReason.JUDGE_DISAGREEMENT],
            "flags": [Flag.JUDGE_DISAGREES],
        }
    )
    verified.criteria[0] = verified.criteria[0].model_copy(
        update={
            "verdict": "none",  # likewise
            "support": "insufficient",
            "suggested_verdict": "partial",
            "verifier_rationale": "Skills-section mention only.",
        }
    )

    with uow as tx:
        results_store.save_verification(tx, candidate_id, verified)

    with uow as tx:
        loaded = results_store.get(tx, candidate_id)

    assert loaded is not None
    assert loaded.score == 7.8, "the verifier moved the score"
    assert loaded.criteria[0].verdict == "strong", "the verifier moved the verdict"
    assert loaded.criteria[0].support == "insufficient"
    assert loaded.criteria[0].suggested_verdict == "partial"
    assert loaded.verification_status == "done"
    assert loaded.review_required is True
    assert loaded.escalation_reasons == [EscalationReason.JUDGE_DISAGREEMENT]


def test_a_fresh_candidate_is_pending_verification(uow: UnitOfWork) -> None:
    """`pending` is not `skipped`: one is displayed as provisional (17.6)."""
    seed(uow)
    make_run(uow)
    with uow as tx:
        candidate_id = results_store.save(tx, "run1", candidate(), key())

    with uow as tx:
        loaded = results_store.get(tx, candidate_id)

    assert loaded is not None
    assert loaded.verification_status == "pending"
    assert loaded.decision == "undecided"
    assert loaded.id == candidate_id
