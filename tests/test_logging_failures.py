"""Structured logging and failure capture (spec 18, build gate 20 step 17).

The gate is blunt and it moved: **purge leaves no copy of the resume anywhere**.
In v4 "anywhere" meant the trace directory. In v6 traces are gone and the same
text lives in `candidates.resume_text` and `candidates.sent_text`, so the gate
searches the database columns *and* the failure-capture directory, by content,
for the candidate's name.

Searching by content rather than checking that a particular column went NULL is
the point. A `purge_candidate` that clears the columns it was written against and
misses the one added last month reports success while a complete copy of the
person's CV sits in the row next to the redacted one — which is worse than not
having the control, because it gets reported as done.
"""

import json
import stat
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from helpers_storage import make_rubric_row
from sqlalchemy import text

from config.settings import settings
from screener.logging import (
    DIR_MODE,
    FILE_MODE,
    bind_job,
    clear_context,
    failure_paths_for,
    write_failure,
)
from screener.models import Actor, Candidate, Position, ScoredCriterion, now
from screener.ports import CacheKey
from screener.service import ScreenerService
from screener.storage import positions_store, results_store, runs_store
from screener.storage.connection import engine
from screener.storage.uow import UnitOfWork

ACTOR = Actor(id="poc-operator", display_name="PoC Operator")

CANDIDATE_NAME = "Asha Nair"
RESUME_TEXT = (
    f"{CANDIDATE_NAME}. Senior Backend Engineer with 7 years of experience. "
    "Contact: asha.nair@example.com. Owned the Kubernetes platform for 12 services."
)
SENT_TEXT = RESUME_TEXT.replace("asha.nair@example.com", "[EMAIL]")


class FakeLLM:
    def chat_json(
        self, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        if "support_checks" in schema.get("properties", {}):
            # A phase-2 call. Empty is a valid `VerifyOutput`: the verifier
            # agreed with everything and found nothing for the `none` criteria.
            return {"support_checks": [], "absence_checks": []}
        return {
            "criteria": [
                {"text": "5+ years backend engineering", "must_have": True, "weight": 5},
                {"text": "Kubernetes in production", "must_have": True, "weight": 4},
                {"text": "Python and Django", "must_have": False, "weight": 3},
                {"text": "Migration to microservices", "must_have": False, "weight": 1},
            ]
        }

    def count_tokens(self, model: str, text: str) -> int:
        return len(text) // 4

    def count_prompt_tokens(self, model: str, system: str, user: str) -> int:
        return 900

    def health(self) -> bool:
        return True

    def digest(self, model: str) -> str:
        return "sha256:aaa"

    def ensure_loaded(self, model: str) -> None:
        self.loaded = model

    def unload(self, model: str) -> None:
        self.loaded = None


@pytest.fixture
def workspace(db: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(settings, "failure_dir", str(tmp_path / "failures"))
    monkeypatch.setattr(settings, "log_dir", str(tmp_path / "logs"))
    monkeypatch.setattr(settings, "resumes_dir", str(tmp_path / "resumes"))
    monkeypatch.setattr(settings, "capture_raw_on_failure", True)
    return tmp_path


@pytest.fixture
def uow_factory(workspace: Path) -> Iterator[Callable[[], UnitOfWork]]:
    yield UnitOfWork


@pytest.fixture
def service(uow_factory: Callable[[], UnitOfWork]) -> ScreenerService:
    return ScreenerService(llm=FakeLLM(), uow_factory=uow_factory)  # type: ignore[arg-type]


def capture(sha: str = "sha-asha") -> Path:
    """One failure capture, holding model output derived from the resume."""
    path = write_failure(
        file_sha256=sha,
        prompt_hash="p" * 64,
        raw_output=f'Here is the JSON you asked for: {{"criteria": [{{"evidence": "{RESUME_TEXT}"',
        error="unterminated object at position 812",
        job_id=7,
    )
    assert path is not None
    return path


def everything_under(root: Path) -> str:
    """Every byte of every file under a directory, concatenated."""
    if not root.is_dir():
        return ""
    return "".join(
        p.read_text(encoding="utf-8", errors="replace") for p in root.rglob("*") if p.is_file()
    )


def everything_in(_db: str) -> str:
    """Every value in every table of the test schema. The content search the gate runs.

    Deliberately catalogue-driven rather than a list of columns to check: the
    point of this gate is to catch the column somebody adds and forgets to add to
    `purge_candidate`, which a hand-maintained list would miss by construction.
    """
    with engine().connect() as connection:
        tables = [
            row[0]
            for row in connection.execute(
                text("SELECT tablename FROM pg_tables WHERE schemaname = current_schema()")
            )
            if not row[0].startswith(("yoyo", "_yoyo"))
        ]
        return "".join(
            str(value)
            for table in tables
            # noqa: S608 — table names come from the catalogue, never from input
            for row in connection.execute(text(f'SELECT * FROM "{table}"'))  # noqa: S608
            for value in row
            if value is not None
        )


# --- failure capture ---------------------------------------------------------


def test_a_schema_failure_captures_the_output_that_caused_it(workspace: Path) -> None:
    """A `SCHEMA_INVALID` with the offending text discarded explains nothing.

    The usual causes — prose wrapped around the JSON, or `num_predict` cutting an
    object in half — are indistinguishable from the exception alone, which is
    what made removing continuous tracing a regression until this replaced it.
    """
    path = capture()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["file_sha256"] == "sha-asha"
    assert payload["prompt_hash"] == "p" * 64
    assert payload["job_id"] == 7
    assert "unterminated object" in payload["error"]
    assert payload["raw_output"].startswith("Here is the JSON")
    assert payload["num_predict"] == settings.num_predict


def test_nothing_is_written_when_capture_is_disabled(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "capture_raw_on_failure", False)

    assert write_failure(file_sha256="s", prompt_hash="p", raw_output="x", error="e") is None
    assert everything_under(Path(settings.failure_dir)) == ""


def test_captures_are_not_world_readable(workspace: Path) -> None:
    """`data/` holds candidate PII in plaintext and this is part of that (20)."""
    path = capture()

    assert stat.S_IMODE(path.stat().st_mode) == FILE_MODE
    assert stat.S_IMODE(path.parent.stat().st_mode) == DIR_MODE


def test_captures_are_found_by_hash_without_an_index(workspace: Path) -> None:
    """The glob is the lookup. Needing an index is what made tracing expensive."""
    mine = capture("sha-asha")
    capture("sha-ben")

    assert failure_paths_for("sha-asha") == [mine]


def test_two_failures_for_one_candidate_do_not_overwrite_each_other(workspace: Path) -> None:
    """A retry that fails differently is a second data point, not a correction."""
    first = capture()
    second = capture()

    assert first != second
    assert len(failure_paths_for("sha-asha")) == 2


# --- the gate: purge leaves no copy anywhere ---------------------------------


def test_purge_leaves_no_copy_of_the_resume_anywhere(
    workspace: Path, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """**The gate.** Searches by content, across the database and the disk.

    v6 moved the resume into `candidates`, so the database is now the primary
    place a copy can survive a purge — and `sent_text`, `redaction_map_json` and
    `verdicts.absence_evidence` all arrived after the original purge statement
    was written. Asserting on columns would only prove the columns someone
    remembered are cleared.
    """
    path = capture()
    _seed_candidate(service, uow_factory, sha="sha-asha")
    db = workspace / "screener.db"

    # Precondition: the name really is in both places before we purge.
    assert CANDIDATE_NAME in everything_in(db)
    assert CANDIDATE_NAME in everything_under(Path(settings.failure_dir))

    erased, removed = service.purge_candidate("sha-asha", ACTOR)

    assert (erased, removed) == (1, 1)
    assert CANDIDATE_NAME not in everything_in(db)
    assert "asha.nair@example.com" not in everything_in(db)
    assert CANDIDATE_NAME not in everything_under(Path(settings.failure_dir))
    assert not path.exists()


def test_purge_keeps_the_non_identifying_statistics(
    workspace: Path, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """A purged candidate stays countable in an escalation rate (12.10)."""
    _seed_candidate(service, uow_factory, sha="sha-asha")

    service.purge_candidate("sha-asha", ACTOR)

    with uow_factory() as tx:
        row = tx.execute(
            "SELECT score, band, cacheable FROM candidates WHERE file_sha256 = ?", ("sha-asha",)
        ).fetchone()

    assert row["score"] == 7.5
    assert row["band"] == "A"
    # Never served from cache again: the text it was judged against is gone.
    assert row["cacheable"] == 0


def test_purge_does_not_touch_another_candidate(
    workspace: Path, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Erasure is per-candidate. Taking a neighbour's data with it is its own incident."""
    mine = capture("sha-asha")
    theirs = capture("sha-ben")
    _seed_candidate(service, uow_factory, sha="sha-asha")
    _seed_candidate(service, uow_factory, sha="sha-ben", filename="Ben Okoro.pdf")

    service.purge_candidate("sha-asha", ACTOR)

    assert not mine.exists()
    assert theirs.exists()
    with uow_factory() as tx:
        row = tx.execute(
            "SELECT resume_text FROM candidates WHERE file_sha256 = ?", ("sha-ben",)
        ).fetchone()
    assert row["resume_text"] == RESUME_TEXT


# --- the JSONL event stream --------------------------------------------------


def test_events_are_json_and_carry_their_context(workspace: Path) -> None:
    """Free-text log lines are unsearchable at batch scale.

    "How many parses crashed last night" is a question you cannot ask of prose,
    and it is exactly the question that matters.

    Read from the file rather than captured stdout: `data/logs/screener.jsonl`
    is where 18 says these go, and it is what an operator will actually grep.
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
    service: ScreenerService,
    uow_factory: Callable[[], UnitOfWork],
    *,
    sha: str,
    filename: str = f"{CANDIDATE_NAME}.pdf",
) -> None:
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

        results_store.save(
            tx,
            "run1",
            Candidate(
                run_id="run1",
                filename=filename,
                file_sha256=sha,
                # Both stored text versions, because both are what the gate is
                # actually searching for.
                resume_text=RESUME_TEXT,
                sent_text=SENT_TEXT,
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
                        absence_evidence="",
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


def test_the_jsonl_stream_carries_only_screener_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`screener.jsonl` is machine-readable, so every line has to be JSON.

    The file handler used to be attached to the **root** logger, which collected
    every INFO line any dependency emitted. httpx logs one per Ollama health
    poll: the real file reached 15.7 MB of which 99.8% was
    `HTTP Request: GET /api/tags` — 229,769 noise lines against 395 real events,
    none of the noise being JSON, in a file named `.jsonl`.

    Silencing httpx by name fixed the loudest library. This asserts the property
    that matters instead: the handler is scoped to the `screener` logger, so a
    dependency added next year cannot reintroduce the problem.
    """
    import logging as stdlib_logging

    import structlog

    from screener.logging import configure_logging, get_logger

    monkeypatch.setattr(settings, "log_dir", str(tmp_path))
    # Same reset the neighbouring test performs: `configure_logging` is
    # idempotent by design, so a test that wants its own stream must clear the
    # previous one first.
    structlog.reset_defaults()
    screener_logger = stdlib_logging.getLogger("screener")
    for handler in list(screener_logger.handlers):
        screener_logger.removeHandler(handler)

    configure_logging()

    get_logger("screener").info("screened", band="A")
    get_logger("screener.worker_loop").info("job_failed", error="LLMError")
    for library in ("httpx", "httpcore", "urllib3", "uvicorn", "watchdog", "yoyo", "streamlit"):
        stdlib_logging.getLogger(library).info("chatter that is not JSON")

    lines = (tmp_path / "screener.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]  # raises if any line is not JSON

    assert {e["event"] for e in events} == {"screened", "job_failed"}
