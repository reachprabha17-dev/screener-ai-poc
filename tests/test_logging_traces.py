"""Structured logging and the trace store (spec 17, build gate 20 step 17).

The gate is blunt: **purge deletes traces — assert no trace file contains the
name**. That test is the last section, and it searches the whole trace directory
for the candidate's name rather than checking that a particular row went away.
Checking the index would prove the index was updated; the thing that matters is
whether a copy of the resume is still on disk.

17 calls this out as the step that is easy to forget and the one that would make
the whole control ineffective: a trace directory outside the erasure path defeats
`purge_candidate` **while appearing implemented**, which is worse than having no
traces, because it gets reported as done.
"""

import json
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from config.settings import settings
from screener.logging import (
    DIR_MODE,
    FILE_MODE,
    TraceWriter,
    bind_job,
    clear_context,
    prune_traces,
)
from screener.models import Actor, Candidate, ScoredCriterion
from screener.pipeline import TraceRecord
from screener.ports import CacheKey
from screener.service import ScreenerService
from screener.storage import results_store, traces_store
from screener.storage.connection import apply_migrations, connect
from screener.storage.uow import UnitOfWork

ACTOR = Actor(id="poc-operator", display_name="PoC Operator")

CANDIDATE_NAME = "Asha Nair"
RESUME_TEXT = (
    f"{CANDIDATE_NAME}. Senior Backend Engineer with 7 years of experience. "
    "Contact: asha.nair@example.com. Owned the Kubernetes platform for 12 services."
)


class FakeLLM:
    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        return {
            "criteria": [
                {"text": "5+ years backend engineering", "must_have": True, "weight": 5},
                {"text": "Kubernetes in production", "must_have": True, "weight": 4},
                {"text": "Python and Django", "must_have": False, "weight": 3},
                {"text": "Migration to microservices", "must_have": False, "weight": 1},
            ]
        }

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def count_prompt_tokens(self, system: str, user: str) -> int:
        return 900

    def health(self) -> bool:
        return True

    @property
    def model_digest(self) -> str:
        return "sha256:aaa"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "screener.db"))
    monkeypatch.setattr(settings, "trace_dir", str(tmp_path / "traces"))
    monkeypatch.setattr(settings, "log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "resumes_dir", str(tmp_path / "resumes"))
    monkeypatch.setattr(settings, "trace_enabled", True)
    apply_migrations(tmp_path / "screener.db")
    return tmp_path


@pytest.fixture
def uow_factory(workspace: Path) -> Iterator[Callable[[], UnitOfWork]]:
    connection = connect(workspace / "screener.db")
    try:
        yield lambda: UnitOfWork(connection)
    finally:
        connection.close()


@pytest.fixture
def service(uow_factory: Callable[[], UnitOfWork]) -> ScreenerService:
    return ScreenerService(llm=FakeLLM(), uow_factory=uow_factory)  # type: ignore[arg-type]


def trace_record(sha: str = "sha-asha") -> TraceRecord:
    return TraceRecord(
        file_sha256=sha,
        system="You are a resume screener.",
        # The exact string sent to the model — full resume text.
        user=f"CRITERIA:\nC1: backend\n\n<<<RESUME\n{RESUME_TEXT}\nRESUME>>>",
        output={"criteria": [{"id": "C1", "verdict": "strong", "evidence": "7 years"}]},
        prompt_tokens=900,
        attempts=1,
    )


def everything_under(root: Path) -> str:
    """Every byte of every file under a directory, concatenated."""
    return "".join(
        p.read_text(encoding="utf-8", errors="replace") for p in root.rglob("*") if p.is_file()
    )


# --- the trace record --------------------------------------------------------


def test_a_trace_captures_what_was_actually_sent(workspace: Path) -> None:
    """What makes offline evaluation possible without re-running a GPU batch (18)."""
    path = TraceWriter().write("run1", trace_record())

    assert path is not None
    payload = json.loads(path.read_text(encoding="utf-8"))

    assert payload["file_sha256"] == "sha-asha"
    assert RESUME_TEXT in payload["user"]
    assert payload["model"] == settings.judge_model
    # Everything needed to re-run this exact call offline.
    for key in ("seed", "num_ctx", "num_predict", "temperature", "prompt_tokens"):
        assert key in payload


def test_tracing_can_be_switched_off(workspace: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "trace_enabled", False)

    assert TraceWriter().write("run1", trace_record()) is None


def test_one_file_per_candidate_per_run(workspace: Path) -> None:
    """Not one shared log.

    Purge deletes whole files. Rewriting a shared log to excise one person's
    text is exactly the operation that half-succeeds.
    """
    writer = TraceWriter()
    first = writer.write("run1", trace_record("sha-a"))
    second = writer.write("run1", trace_record("sha-b"))

    assert first != second
    assert first is not None and second is not None
    assert first.parent == second.parent


def test_traces_are_not_world_readable(workspace: Path) -> None:
    """`data/` holds candidate PII in plaintext; traces are part of that (19)."""
    path = TraceWriter().write("run1", trace_record())

    assert path is not None
    assert stat.S_IMODE(path.stat().st_mode) == FILE_MODE
    assert stat.S_IMODE(path.parent.stat().st_mode) == DIR_MODE


# --- the gate: purge leaves no copy on disk ----------------------------------


def test_purge_leaves_no_trace_file_containing_the_name(
    workspace: Path, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """**The gate.** Searches the whole trace directory, not the index.

    Checking that a row disappeared would prove the index was updated. What
    matters is whether a copy of the resume is still on disk — and a trace
    directory outside the erasure path defeats `purge_candidate` while appearing
    implemented, which 17 calls worse than having no traces at all.
    """
    trace_root = Path(settings.trace_dir)
    path = TraceWriter().write("run1", trace_record())
    assert path is not None

    _seed_candidate(service, uow_factory, sha="sha-asha")
    with uow_factory() as tx:
        traces_store.record(tx, "run1", "sha-asha", path)

    # Precondition: the name really is on disk before we purge.
    assert CANDIDATE_NAME in everything_under(trace_root)

    removed = service.purge_candidate("sha-asha", ACTOR)

    assert removed == 1
    assert CANDIDATE_NAME not in everything_under(trace_root)
    assert "asha.nair@example.com" not in everything_under(trace_root)
    assert not path.exists()


def test_purge_clears_the_index_as_well_as_the_disk(
    workspace: Path, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    path = TraceWriter().write("run1", trace_record())
    assert path is not None
    _seed_candidate(service, uow_factory, sha="sha-asha")
    with uow_factory() as tx:
        traces_store.record(tx, "run1", "sha-asha", path)

    service.purge_candidate("sha-asha", ACTOR)

    with uow_factory() as tx:
        assert traces_store.paths_for(tx, "sha-asha") == []


def test_purge_does_not_touch_another_candidates_trace(
    workspace: Path, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Erasure is per-candidate. Taking a neighbour's data with it is its own incident."""
    writer = TraceWriter()
    mine = writer.write("run1", trace_record("sha-asha"))
    theirs = writer.write("run1", trace_record("sha-ben"))
    assert mine is not None and theirs is not None

    _seed_candidate(service, uow_factory, sha="sha-asha")
    _seed_candidate(service, uow_factory, sha="sha-ben")
    with uow_factory() as tx:
        traces_store.record(tx, "run1", "sha-asha", mine)
        traces_store.record(tx, "run1", "sha-ben", theirs)

    service.purge_candidate("sha-asha", ACTOR)

    assert not mine.exists()
    assert theirs.exists()


# --- retention ---------------------------------------------------------------


def test_old_traces_are_prunable(workspace: Path) -> None:
    """Retention shorter than the audit log (17).

    The audit trail records decisions and must outlive the raw text those
    decisions were made from.
    """
    import os
    import time

    path = TraceWriter().write("run1", trace_record())
    assert path is not None
    old = time.time() - 60 * 60 * 24 * 90
    os.utime(path, (old, old))

    removed = prune_traces(older_than_days=30)

    assert path in removed
    assert not path.exists()


def test_pruning_keeps_recent_traces(workspace: Path) -> None:
    path = TraceWriter().write("run1", trace_record())

    assert prune_traces(older_than_days=30) == []
    assert path is not None and path.exists()


# --- the JSONL event stream --------------------------------------------------


def test_events_are_json_and_carry_their_context(workspace: Path) -> None:
    """Free-text log lines are unsearchable at batch scale.

    "How many parses crashed last night" is a question you cannot ask of prose,
    and it is exactly the question that matters.

    Read from the file rather than captured stdout: `data/logs/screener.jsonl`
    is where 17 says these go, and it is what an operator will actually grep.
    """
    import logging as stdlib_logging

    import structlog

    # `configure_logging` is idempotent by design — a process must not reshape a
    # stream something is already parsing. Reset it so this test gets its own.
    structlog.reset_defaults()
    for handler in list(stdlib_logging.getLogger().handlers):
        stdlib_logging.getLogger().removeHandler(handler)

    from screener.logging import configure_logging, get_logger

    configure_logging()
    log = get_logger("test")
    bind_job(run_id="run1", job_id=7, worker_id="worker-1")
    try:
        log.info("screened", stage="complete", file_sha256="sha-asha", duration_ms=4900)
    finally:
        clear_context()
        stdlib_logging.shutdown()

    log_file = Path(settings.log_dir) / "screener.jsonl"
    assert log_file.is_file(), "no JSONL log was written"
    line = next(
        json.loads(entry)
        for entry in log_file.read_text(encoding="utf-8").splitlines()
        if entry.strip().startswith("{")
    )

    assert line["event"] == "screened"
    assert line["run_id"] == "run1"
    assert line["job_id"] == 7
    assert line["worker_id"] == "worker-1"
    assert line["file_sha256"] == "sha-asha"
    assert line["duration_ms"] == 4900
    assert "timestamp" in line


def test_context_does_not_leak_between_jobs(workspace: Path) -> None:
    """A `duration_ms` attributed to the wrong candidate is worse than none."""
    import structlog

    bind_job(run_id="run1", job_id=1, worker_id="w")
    clear_context()

    assert structlog.contextvars.get_contextvars() == {}


# --- helpers -----------------------------------------------------------------


def _seed_candidate(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork], *, sha: str
) -> None:
    from screener.models import Position, now
    from screener.storage import positions_store, rubrics_store, runs_store

    with uow_factory() as tx:
        positions_store.seed_user(tx, ACTOR.id, ACTOR.display_name)
        if positions_store.get(tx, "p1") is None:
            positions_store.create(
                tx,
                Position(
                    id="p1",
                    reference="REQ-1",
                    title="Backend",
                    jd_text="jd",
                    created_by=ACTOR.id,
                    created_at=now(),
                ),
            )
            from helpers_storage import make_rubric_row

            make_rubric_row(tx, "p1", "r1")
            runs_store.create(
                tx,
                run_id="run1",
                position_id="p1",
                rubric_id="r1",
                folder="f",
                created_by=ACTOR.id,
                judge_model="m",
                judge_digest="sha256:aaa",
                prompt_hash="p" * 64,
                redaction_on=True,
                num_ctx=8192,
                num_predict=1536,
                seed=42,
                app_version="v",
            )
            assert rubrics_store.get(tx, "r1") is not None

        results_store.save(
            tx,
            "run1",
            Candidate(
                run_id="run1",
                filename=f"{CANDIDATE_NAME}.pdf",
                file_sha256=sha,
                score=7.5,
                band="A",
                must_haves_met=True,
                criteria=[
                    ScoredCriterion(
                        id="C1",
                        verdict="strong",
                        model_verdict="strong",
                        evidence="Senior Backend Engineer with 7 years of experience",
                        verified=True,
                        match_ratio=1.0,
                        longest_span=8,
                        weight=1,
                        must_have=False,
                    )
                ],
            ),
            CacheKey(
                file_sha256=sha,
                position_id="p1",
                rubric_hash="r" * 64,
                judge_digest="sha256:aaa",
                prompt_hash="p" * 64,
                redaction_on=True,
                num_ctx=8192,
                app_version="v",
            ),
        )
