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
from screener.models import Actor, EscalationReason, Flag, ParsedResume, ParseResult
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
        self.verified = 0
        self.loads: list[str] = []
        self.verify_reply: dict[str, Any] | None = None

    def chat_json(
        self, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        if "support_checks" in schema.get("properties", {}):
            # A phase-2 call. Empty is a valid `VerifyOutput`: the verifier
            # agreed with everything and found nothing for the `none` criteria.
            self.verified += 1
            return self.verify_reply or {"support_checks": [], "absence_checks": []}
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

    def count_tokens(self, model: str, text: str) -> int:
        return len(text) // 4

    def count_prompt_tokens(self, model: str, system: str, user: str) -> int:
        return 900

    def health(self) -> bool:
        return True

    def digest(self, model: str) -> str:
        return self._digest

    def ensure_loaded(self, model: str) -> None:
        # Recorded, not just stored: the two-phase gate is about how *many*
        # times a model is loaded, not which one is resident.
        if not self.loads or self.loads[-1] != model:
            self.loads.append(model)
        self.loaded = model

    def unload(self, model: str) -> None:
        self.loaded = None


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
    """Two passes over four resumes: four judge jobs, then four verify jobs (17.4).

    `drain` counts units of work, so eight is the two-phase shape. `llm.judged`
    counting four is the part that matters — each candidate is judged once, and
    the second pass is a different model asking a different question.
    """
    run_id = seed_run(service, count=4)

    assert drain(worker) == 8

    status = service.run_status(run_id)
    assert status.status == "completed"
    assert status.phase == "done"
    assert status.done == 4, "`total` and `done` count candidates, not jobs"
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
    # One judge job for the new file, then one verify job for the candidate it
    # produced. The two already-verified candidates are not re-verified (12.9).
    assert drain(worker) == 2
    assert service.run_status(run_id).total == 3


# --- failure handling --------------------------------------------------------


def test_an_infrastructure_failure_returns_the_job_for_retry(
    worker: Worker, service: ScreenerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = seed_run(service, count=1)

    def explode(*args: object, **kwargs: object) -> None:
        raise ConnectionError("ollama went away")

    monkeypatch.setattr("screener.worker_loop.judge_one", explode)
    worker.run_once()

    assert service.run_status(run_id).pending == 1  # back in the queue


def test_a_persistently_failing_job_stops_at_the_attempt_cap(
    worker: Worker, service: ScreenerService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file that reliably kills the worker must not loop forever (16.5)."""
    run_id = seed_run(service, count=1)
    monkeypatch.setattr(
        "screener.worker_loop.judge_one",
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


# --- gate: two-phase execution (17.4) ----------------------------------------


def test_a_run_loads_two_models_not_two_thousand(
    worker: Worker, service: ScreenerService, llm: FakeLLM
) -> None:
    """**The gate.** One model, loaded once per run, not once per resume.

    Historically two loads: 12 GB of VRAM did not hold `granite4.1:8b` and
    `gemma4:12b` together, so the two passes were phased over the whole run to
    avoid a 10-20s model load per candidate. Judge and verifier now share one
    model (`config/settings.py`), so there is nothing left to swap between —
    the gate this protects is "not once per resume", not "exactly two loads".

    `llm.verified` stays 0: the shipped `verify_scope` default is `"none"`
    (support verification off), and this run's candidates have no `none`
    verdicts for `confirm_absence` to check either — see
    `test_the_verifier_records_a_disagreement_without_moving_the_score` for
    phase 2 actually doing something.
    """
    seed_run(service, count=5)

    drain(worker)

    assert llm.judged == 5
    assert llm.verified == 0
    assert llm.loads == [settings.judge_model], llm.loads


def test_the_verifier_records_a_disagreement_without_moving_the_score(
    worker: Worker, service: ScreenerService, llm: FakeLLM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1.9 end to end: the second model escalates and proposes, never overrules.

    `verify_scope="all"` explicitly: the shipped default is `"none"` (support
    verification is off by default this session — see `config/settings.py`),
    which would make this scenario unreachable end to end.
    """
    monkeypatch.setattr(settings, "verify_scope", "all")
    run_id = seed_run(service, count=1)
    llm.verify_reply = {
        "support_checks": [
            {
                "id": "C1",
                "support": "insufficient",
                "suggested_verdict": "partial",
                "rationale": "Skills-section mention only; no production context.",
            }
        ],
        "absence_checks": [],
    }

    drain(worker)

    result = service.list_candidates(run_id)
    candidate = (result.meets_must_haves + result.needs_review)[0]
    c1 = next(c for c in candidate.criteria if c.id == "C1")

    assert c1.verdict == "strong", "the verifier is not allowed to move a verdict"
    assert c1.support == "insufficient"
    assert c1.suggested_verdict == "partial"
    assert Flag.JUDGE_DISAGREES in candidate.flags
    assert EscalationReason.JUDGE_DISAGREEMENT in candidate.escalation_reasons
    assert candidate.review_required is True
    assert candidate.verification_status == "done"


def test_a_run_is_not_complete_until_both_phases_are(
    worker: Worker, service: ScreenerService
) -> None:
    """Completing after phase 1 would mark a run reviewable with nothing verified.

    Every candidate would still read `pending`, which 17.6 says must display as
    provisional — so a run reported complete would be one nobody should sign off.
    """
    run_id = seed_run(service, count=2)

    # Judge both, and stop before the verify pass drains.
    worker.run_once()
    worker.run_once()

    status = service.run_status(run_id)
    assert status.phase == "verify"
    assert status.status != "completed"

    drain(worker)

    assert service.run_status(run_id).status == "completed"
    assert service.run_status(run_id).phase == "done"


def test_an_unscoreable_candidate_is_never_verified(
    worker: Worker, service: ScreenerService, llm: FakeLLM
) -> None:
    """Already escalating for a stronger reason; ~5 s of GPU would change nothing."""
    seed_run(service, count=1)
    settings_backup = settings.evidence_match_ratio
    try:
        # Force stage B to fail: nothing can match at a ratio above 1.0.
        settings.evidence_match_ratio = 1.5
        drain(worker)
    finally:
        settings.evidence_match_ratio = settings_backup

    assert llm.judged == 1
    assert llm.verified == 0, "an unscoreable candidate reached the verifier"


# --- the identity recovery depends on (16.5) --------------------------------

WORKER_ID_CHILD = """
import sys
sys.path.insert(0, {repo!r})
from config.settings import settings
print(settings.worker_id)
"""


def _worker_id_of_a_fresh_process() -> str:
    """The name a newly started worker would claim jobs under.

    Read from a real child process, because the thing under test is precisely
    what changes between one process and the next.
    """
    result = subprocess.run(  # noqa: S603 — fixed argv, test-local script
        [sys.executable, "-c", WORKER_ID_CHILD.format(repo=str(Path.cwd()))],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def test_a_restarted_worker_answers_to_the_same_name() -> None:
    """Startup reclaim recovers jobs `claimed` by *this* worker_id (16.5).

    That is the whole recovery mechanism, and it rests on one assumption: the
    name survives a restart. It did not. The default was
    `f"{hostname}-{os.getpid()}"`, and the pid is reissued every start — so a
    worker that died holding a job came back under a new name, found nothing
    wearing the old one, and left the job `claimed` forever. `no_pending` counts
    `claimed`, so the phase never drained and the run never completed. Silently.

    `deploy/screener-worker.service` sets `Restart=on-failure`, so the restart
    that triggers this is the one the deployment performs automatically.

    The existing SIGKILL tests could not catch it: they pin `WORKER_ID` and hand
    the same constant to the restarted worker, which proves the mechanism works
    while stepping over the default that breaks its precondition.
    """
    first = _worker_id_of_a_fresh_process()
    second = _worker_id_of_a_fresh_process()

    assert first == second, (
        f"a restarted worker calls itself {second!r} but its stranded jobs are "
        f"claimed by {first!r} — reclaim_orphaned matches on this name and will "
        "find nothing, leaving the run wedged"
    )


# A worker that only starts up and reports what it recovered. It takes its name
# the way a deployment does — from `settings.worker_id` — rather than being
# handed one, which is the whole point of the test below.
RESTART_CHILD = """
import sys
sys.path.insert(0, {repo!r})
sys.path.insert(0, {tests!r})

from config.settings import settings
settings.db_path = {db!r}
settings.resumes_dir = {resumes!r}
settings.min_free_disk_gb = 0

from test_worker import FakeLLM, FakeParser
from screener.pipeline import Deps
from screener.service import ScreenerService
from worker import Worker

llm = FakeLLM()
worker = Worker(
    service=ScreenerService(llm=llm),
    deps=Deps(parser=FakeParser(), llm=llm),
    poll_interval_s=0.01,
)
print(worker.worker_id, flush=True)
print(worker.startup(), flush=True)
"""


def test_a_restarted_worker_reclaims_the_job_its_previous_life_was_holding(
    db: Path, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Crash recovery through a real restart, with no worker_id handed in.

    `test_a_batch_killed_midway_resumes_without_redoing_work` pins `WORKER_ID`
    and gives the restarted worker the same constant, so it proves the reclaim
    *mechanism* while stepping over whether a real restart still answers to the
    same name. It did not: the default carried `os.getpid()`, so the restarted
    process searched for a name nothing wore and the job stayed `claimed` for
    good. `no_pending` counts `claimed`, so the phase never drained and the run
    never finished — silently, with `Restart=on-failure` performing exactly this
    restart.

    Set up as a crash rather than acted out as one: a job left `claimed` by the
    name a fresh process reports is precisely what a killed worker leaves behind,
    and asserting on it does not depend on winning a race against the kill.
    """
    seed_run(service, count=1)
    previous_life = _worker_id_of_a_fresh_process()

    with uow_factory() as tx:
        claimed = jobs_store.claim_next(tx, previous_life)
    assert claimed is not None, "expected a job to claim"

    # A second process, with its own pid — the restart systemd performs.
    result = subprocess.run(  # noqa: S603 — fixed argv, test-local script
        [
            sys.executable,
            "-c",
            RESTART_CHILD.format(
                repo=str(Path.cwd()),
                tests=str(Path.cwd() / "tests"),
                db=str(db),
                resumes=settings.resumes_dir,
            ),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    restarted_as, reclaimed = result.stdout.split()

    assert restarted_as == previous_life, (
        "the restarted worker must answer to the same name, or reclaim matches nothing"
    )
    assert int(reclaimed) == 1, "the job the previous life was holding was not recovered"

    with uow_factory() as tx:
        assert jobs_store.progress(tx, claimed.run_id).pending == 1, (
            "the stranded job should be back in the queue, not still claimed"
        )


def test_the_lease_sweep_is_rate_limited_on_a_monotonic_clock(
    db: Path, service: ScreenerService, worker: Worker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The idle loop turns over every couple of seconds; the sweep is a write.

    Rate-limited on `time.monotonic`, deliberately — the wall clock is the thing
    the lease comparison already has to tolerate stepping, and the *scheduling*
    of the check should not be exposed to it as well.
    """
    calls: list[str] = []
    monkeypatch.setattr(
        service, "reclaim_expired_leases", lambda worker_id: calls.append(worker_id) or 0
    )
    monkeypatch.setattr(settings, "lease_sweep_interval_s", 600)

    worker.sweep_expired_leases()
    worker.sweep_expired_leases()
    worker.sweep_expired_leases()
    assert len(calls) == 1, "swept more than once inside the interval"

    # Far enough on, and it runs again.
    monkeypatch.setattr(worker, "_last_sweep", float("-inf"))
    worker.sweep_expired_leases()
    assert len(calls) == 2
