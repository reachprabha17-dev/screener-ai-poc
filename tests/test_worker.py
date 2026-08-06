"""The screening daemon (spec 16, build gate 20 step 14).

The gate names four properties: **resumes a batch killed at 50%**, **no duplicate
work**, **no lost jobs**, and **folder rescan**.

"No lost jobs" is the one worth stating plainly, because its failure is silent.
A dropped job leaves a real applicant unscored in a run that reports as complete;
nobody gets an error, and the only symptom is a person who applied and never
appeared. Several tests below assert `done + failed == total` for exactly that
reason.

The resume test kills a real worker process mid-batch with SIGKILL. A worker that
dies holding claimed jobs is not hypothetical — it is every deploy, every OOM,
every power cut.
"""

import os
import signal
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from config.settings import settings
from screener.logging import TraceWriter
from screener.models import Actor, Flag, ParsedResume, ParseResult
from screener.pipeline import Deps
from screener.service import ScreenerService
from screener.storage import jobs_store, results_store
from screener.storage.connection import apply_migrations, connect
from screener.storage.uow import UnitOfWork
from worker import Worker

ACTOR = Actor(id="poc-operator", display_name="PoC Operator")
WORKER_ID = "worker-1"

JD = "Senior Backend Engineer. Required: 5+ years backend. Kubernetes essential."

RESUME = (
    "Asha Nair. Senior Backend Engineer with 7 years of experience. "
    "Led the migration of a payments monolith to microservices in Go. "
    "Owned the Kubernetes platform for 12 services. "
    "Built REST APIs in Python and Django with PostgreSQL."
)

# One quote per criterion, each genuinely about that criterion and present in
# RESUME. A single generic quote reused across all four is exactly the
# verified-but-irrelevant pattern 10.5(c) exists to catch.
EVIDENCE_BY_CRITERION = {
    "C1": "Asha Nair. Senior Backend Engineer with 7 years of experience",
    "C2": "Owned the Kubernetes platform for 12 services",
    "C3": "Built REST APIs in Python and Django with PostgreSQL",
    "C4": "Led the migration of a payments monolith to microservices in Go",
}


class FakeLLM:
    def __init__(self, *, digest: str = "sha256:aaa") -> None:
        self._digest = digest
        self.judged = 0

    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        if "CRITERIA:" not in user:  # the rubric-extraction call
            return {
                "criteria": [
                    {"text": "5+ years backend engineering", "must_have": True, "weight": 5},
                    {"text": "Kubernetes in production", "must_have": True, "weight": 4},
                    {"text": "Python and Django", "must_have": False, "weight": 3},
                    {"text": "Migration to microservices", "must_have": False, "weight": 1},
                ]
            }
        self.judged += 1
        return {
            "criteria": [
                {"id": cid, "verdict": "strong", "evidence": quote}
                for cid, quote in EVIDENCE_BY_CRITERION.items()
            ],
            "summary": "",
            "notable_strengths": [],
            "red_flags": [],
        }

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def count_prompt_tokens(self, system: str, user: str) -> int:
        return 900

    def health(self) -> bool:
        return True

    @property
    def model_digest(self) -> str:
        return self._digest


class FakeParser:
    def parse(self, path: Path) -> ParseResult:
        return ParseResult(
            parsed=ParsedResume(
                text=RESUME, page_count=1, ocr_used=False, parser_version="fake/1.0"
            )
        )


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "screener.db"
    apply_migrations(path)
    monkeypatch.setattr(settings, "db_path", str(path))
    monkeypatch.setattr(settings, "resumes_dir", str(tmp_path / "resumes"))
    return path


@pytest.fixture
def uow_factory(db: Path) -> Iterator[Callable[[], UnitOfWork]]:
    connection = connect(db)
    try:
        yield lambda: UnitOfWork(connection)
    finally:
        connection.close()


@pytest.fixture
def llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def service(llm: FakeLLM, uow_factory: Callable[[], UnitOfWork]) -> ScreenerService:
    return ScreenerService(llm=llm, uow_factory=uow_factory)  # type: ignore[arg-type]


@pytest.fixture
def worker(service: ScreenerService, llm: FakeLLM) -> Worker:
    return Worker(
        service=service,
        deps=Deps(parser=FakeParser(), llm=llm),  # type: ignore[arg-type]
        worker_id=WORKER_ID,
        poll_interval_s=0.01,
    )


def pdf_bytes(marker: int) -> bytes:
    """A valid, *distinct* PDF — distinct so each file gets its own sha256."""
    return (
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R>>endobj\n"
        + f"% candidate {marker}\n".encode()
        + b"trailer<</Root 1 0 R>>\n%%EOF\n"
    )


def seed_run(service: ScreenerService, count: int = 4, reference: str = "REQ-1") -> str:
    position = service.create_position(
        reference=reference, title="Backend Engineer", jd_text=JD, actor=ACTOR
    )
    rubric = service.extract_rubric(position.id, ACTOR)
    service.approve_rubric(rubric.id, ACTOR)

    folder = Path(settings.resumes_dir) / reference
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (folder / f"cv{i}.pdf").write_bytes(pdf_bytes(i))

    run = service.create_run(position.id, rubric.id, ACTOR)
    service.start_run(run.id, ACTOR)
    return run.id


def drain(worker: Worker, limit: int = 100) -> int:
    processed = 0
    while processed < limit and worker.run_once():
        processed += 1
    return processed


# --- the whole batch ---------------------------------------------------------


def test_a_batch_screens_end_to_end(worker: Worker, service: ScreenerService, llm: FakeLLM) -> None:
    run_id = seed_run(service, count=4)

    assert drain(worker) == 4

    status = service.run_status(run_id)
    assert status.status == "completed"
    assert status.done == 4
    assert status.failed == 0
    assert llm.judged == 4

    result = service.list_candidates(run_id)
    assert len(result.meets_must_haves) == 4


def test_no_jobs_are_lost(worker: Worker, service: ScreenerService) -> None:
    """The silent failure. A dropped job leaves a real applicant unscored in a
    run that reports complete — no error, no symptom except a person who applied
    and never appeared."""
    run_id = seed_run(service, count=6)

    drain(worker)
    status = service.run_status(run_id)

    assert status.done + status.failed == status.total == 6
    assert status.pending == 0
    assert status.claimed == 0


def test_every_file_produces_exactly_one_candidate(
    worker: Worker, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    run_id = seed_run(service, count=5)
    drain(worker)

    with uow_factory() as tx:
        candidates = results_store.list_for_run(tx, run_id)

    assert len(candidates) == 5
    assert len({c.file_sha256 for c in candidates}) == 5  # no file screened twice


def test_the_run_completes_only_when_the_queue_empties(
    worker: Worker, service: ScreenerService
) -> None:
    run_id = seed_run(service, count=3)

    worker.run_once()
    assert service.run_status(run_id).status == "running"

    drain(worker)
    assert service.run_status(run_id).status == "completed"


def test_the_completed_run_records_its_escalation_rate(
    worker: Worker, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Written at completion so nobody discovers the queue candidate by candidate."""
    from screener.storage import runs_store

    run_id = seed_run(service, count=3)
    drain(worker)

    with uow_factory() as tx:
        run = runs_store.get(tx, run_id)

    assert run is not None
    assert run.escalation_rate == 0.0


# --- gate: no duplicate work -------------------------------------------------


def test_a_second_pass_over_a_finished_run_does_nothing(
    worker: Worker, service: ScreenerService, llm: FakeLLM
) -> None:
    seed_run(service, count=3)
    drain(worker)
    judged_after_first = llm.judged

    assert drain(worker) == 0
    assert llm.judged == judged_after_first


def test_a_rerun_over_the_same_folder_hits_the_cache(
    worker: Worker, service: ScreenerService, llm: FakeLLM
) -> None:
    """The point of excluding `run_id` from the cache key (12.5).

    Same files, same rubric, same model, same prompt — the judgments are already
    known, so a second run costs nothing but bookkeeping.
    """
    first = seed_run(service, count=4, reference="REQ-1")
    drain(worker)
    assert llm.judged == 4

    # A second run over the identical folder, same position and rubric.
    from screener.storage import runs_store

    with worker.service.uow_factory() as tx:
        run = runs_store.get(tx, first)
    assert run is not None
    second = service.create_run(run.position_id, run.rubric_id, ACTOR)
    service.start_run(second.id, ACTOR)

    assert drain(worker) == 4
    assert llm.judged == 4  # not one extra inference call

    assert len(service.list_candidates(second.id).meets_must_haves) == 4


def test_a_changed_rubric_defeats_the_cache(
    worker: Worker, service: ScreenerService, llm: FakeLLM
) -> None:
    """Different criteria mean the stored verdicts answer a different question."""
    from screener.models import Criterion
    from screener.storage import runs_store

    first = seed_run(service, count=2, reference="REQ-1")
    drain(worker)
    assert llm.judged == 2

    with worker.service.uow_factory() as tx:
        run = runs_store.get(tx, first)
    assert run is not None

    edited = service.save_rubric(
        run.position_id,
        [
            Criterion(id="C1", text="8+ years backend", must_have=True, weight=5),
            Criterion(id="C2", text="Kubernetes", must_have=True, weight=4),
            Criterion(id="C3", text="Rust", weight=2),
            Criterion(id="C4", text="Mentoring", weight=1),
        ],
        ACTOR,
    )
    service.approve_rubric(edited.id, ACTOR)
    second = service.create_run(run.position_id, edited.id, ACTOR)
    service.start_run(second.id, ACTOR)

    drain(worker)
    assert llm.judged == 4  # re-judged, because the question changed


# --- gate: folder rescan -----------------------------------------------------


def test_files_added_mid_run_are_picked_up_by_rescan(
    worker: Worker, service: ScreenerService
) -> None:
    """Never silently. A ranking over a set that changed underneath it describes
    nothing (16.2)."""
    run_id = seed_run(service, count=2)
    drain(worker)
    assert service.run_status(run_id).status == "completed"

    folder = Path(settings.resumes_dir) / "REQ-1"
    (folder / "late.pdf").write_bytes(pdf_bytes(99))

    assert service.rescan_run(run_id, ACTOR) == 1

    # A completed run stops being claimable, so the operator restarts it. That is
    # deliberate: work is never silently resumed under a run reported as done.
    service.start_run(run_id, ACTOR)
    assert drain(worker) == 1
    assert service.run_status(run_id).total == 3


# --- failure handling --------------------------------------------------------


def test_an_infrastructure_failure_returns_the_job_for_retry(
    worker: Worker, service: ScreenerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = seed_run(service, count=1)

    def explode(*args: object, **kwargs: object) -> None:
        raise ConnectionError("ollama went away")

    monkeypatch.setattr("screener.worker_loop.screen_one", explode)
    worker.run_once()

    assert service.run_status(run_id).pending == 1  # back in the queue


def test_a_persistently_failing_job_stops_at_the_attempt_cap(
    worker: Worker, service: ScreenerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that reliably kills the worker must not loop forever (16.5)."""
    run_id = seed_run(service, count=1)
    monkeypatch.setattr(
        "screener.worker_loop.screen_one",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )

    for _ in range(settings.job_max_attempts + 2):
        worker.run_once()

    status = service.run_status(run_id)
    assert status.failed == 1
    assert status.pending == 0


def test_one_bad_file_does_not_stop_the_batch(worker: Worker, service: ScreenerService) -> None:
    """Failure isolation. The other 999 candidates still get results."""
    seed_run(service, count=3)
    folder = Path(settings.resumes_dir) / "REQ-1"
    (folder / "broken.pdf").write_bytes(b"not a pdf at all")
    run_id = _only_run(service)
    service.rescan_run(run_id, ACTOR)

    drain(worker)

    status = service.run_status(run_id)
    assert status.done + status.failed == 4
    result = service.list_candidates(run_id)
    assert len(result.meets_must_haves) == 3
    assert len(result.needs_review) == 1  # rejected at intake, still visible
    assert Flag.INPUT_REJECTED in result.needs_review[0].flags


def test_a_full_disk_stops_the_worker_claiming(
    worker: Worker, service: ScreenerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A full disk mid-batch is recoverable only if the worker stopped first (17)."""
    run_id = seed_run(service, count=2)
    monkeypatch.setattr(settings, "min_free_disk_gb", 10**9)

    assert worker.run_once() is False
    assert service.run_status(run_id).pending == 2


# --- shutdown ----------------------------------------------------------------


def test_a_stop_signal_lets_the_loop_exit(worker: Worker, service: ScreenerService) -> None:
    seed_run(service, count=2)
    worker.request_stop()

    worker.run_forever()  # returns rather than hanging

    assert service.run_status(_only_run(service)).pending == 2


def test_startup_reclaims_this_workers_orphans(
    worker: Worker, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    seed_run(service, count=3)
    with uow_factory() as tx:
        jobs_store.claim_next(tx, WORKER_ID)  # simulate a previous life

    assert worker.startup() == 1
    assert service.run_status(_only_run(service)).pending == 3


# --- gate: resume a batch killed at 50% --------------------------------------


KILL_CHILD = """
import sys, pathlib
sys.path.insert(0, {repo!r})
sys.path.insert(0, {tests!r})

from config.settings import settings
settings.db_path = {db!r}
settings.resumes_dir = {resumes!r}
settings.min_free_disk_gb = 0

from test_worker import FakeLLM, FakeParser
from screener.pipeline import Deps
from screener.service import ScreenerService
from screener.logging import TraceWriter
from worker import Worker

llm = FakeLLM()
worker = Worker(
    service=ScreenerService(llm=llm),
    deps=Deps(parser=FakeParser(), llm=llm),
    worker_id={worker_id!r},
    poll_interval_s=0.01,
)
worker.startup()
done = 0
while worker.run_once():
    done += 1
    print(done, flush=True)
"""


def test_a_batch_killed_midway_resumes_without_redoing_work(
    db: Path, service: ScreenerService, worker: Worker, tmp_path: Path
) -> None:
    """The gate, with a real SIGKILL against a real process.

    Two properties matter on restart, and they pull in opposite directions:
    nothing already screened may be screened again (duplicate work, and a
    `UNIQUE(file_sha256, run_id)` violation), and nothing in flight when the
    process died may be lost. Startup reclaim is what makes both true at once.
    """
    run_id = seed_run(service, count=8)

    script = KILL_CHILD.format(
        repo=str(Path.cwd()),
        tests=str(Path.cwd() / "tests"),
        db=str(db),
        resumes=settings.resumes_dir,
        worker_id=WORKER_ID,
    )
    child = subprocess.Popen(  # noqa: S603 — fixed argv, test-local script
        [sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout is not None
        # Let it finish roughly half the batch, then kill it uncleanly.
        for _ in range(4):
            assert child.stdout.readline().strip()
    finally:
        os.kill(child.pid, signal.SIGKILL)
        child.wait(timeout=15)

    assert child.returncode == -signal.SIGKILL

    midway = service.run_status(run_id)
    assert 0 < midway.done < 8
    completed_before = midway.done

    # Restart in-process. Startup reclaim recovers whatever was in flight.
    reclaimed = worker.startup()
    assert reclaimed <= 1  # at most the one job it was holding

    drain(worker)

    final = service.run_status(run_id)
    assert final.done + final.failed == 8
    assert final.status == "completed"

    candidates = service.list_candidates(run_id)
    total = (
        len(candidates.meets_must_haves)
        + len(candidates.missing_must_have)
        + len(candidates.needs_review)
    )
    assert total == 8
    assert completed_before > 0  # the first process really did do work


def _only_run(service: ScreenerService) -> str:
    from screener.storage import runs_store

    with service.uow_factory() as tx:
        runs = tx.execute("SELECT id FROM runs ORDER BY created_at").fetchall()
    assert runs_store is not None
    return str(runs[0]["id"])


# --- traces (17) ------------------------------------------------------------


def test_the_worker_writes_and_indexes_a_trace(
    worker: Worker, service: ScreenerService, uow_factory: Callable[[], UnitOfWork], tmp_path: Path
) -> None:
    """Both, or neither is useful.

    A trace on disk with no index row is resume text nothing knows about —
    `purge_candidate` would report success and leave a full copy behind (17).
    """
    from screener.storage import traces_store

    settings.trace_dir = str(tmp_path / "traces")
    settings.trace_enabled = True
    worker.traces = TraceWriter()

    run_id = seed_run(service, count=2)
    drain(worker)

    written = list((tmp_path / "traces").rglob("*.jsonl"))
    assert len(written) == 2

    with uow_factory() as tx:
        indexed = traces_store.paths_for_run(tx, run_id)
    assert sorted(indexed) == sorted(written)


def test_a_cache_hit_writes_no_trace(
    worker: Worker, service: ScreenerService, tmp_path: Path
) -> None:
    """Nothing was sent to the model, so there is nothing to trace.

    A trace implying an inference that never happened would corrupt exactly the
    offline evaluation traces exist for.
    """
    from screener.storage import runs_store

    settings.trace_dir = str(tmp_path / "traces")
    settings.trace_enabled = True
    worker.traces = TraceWriter()

    first = seed_run(service, count=2)
    drain(worker)
    assert len(list((tmp_path / "traces").rglob("*.jsonl"))) == 2

    with service.uow_factory() as tx:
        run = runs_store.get(tx, first)
    assert run is not None
    second = service.create_run(run.position_id, run.rubric_id, ACTOR)
    service.start_run(second.id, ACTOR)
    drain(worker)

    # Still two — the re-run was served entirely from the cache.
    assert len(list((tmp_path / "traces").rglob("*.jsonl"))) == 2
