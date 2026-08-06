"""A real batch, end to end (spec §16, build gate §20 step 14).

Real files, real sandbox, real model, real database. Everything below has been
exercised against fakes; this is the only test that proves a folder of resumes
becomes a ranked, banded, signed-off-able result — which is the whole product.

Run with `pytest -m live`.
"""

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from fixtures_docs import real_docx

from config.settings import settings
from screener.clients.ollama_client import OllamaClient
from screener.intake.sandbox import SandboxedParser
from screener.models import Actor
from screener.pipeline import Deps
from screener.service import ScreenerService
from screener.storage.connection import apply_migrations, connect
from screener.storage.uow import UnitOfWork
from worker import Worker

pytestmark = pytest.mark.live

ACTOR = Actor(id="poc-operator", display_name="PoC Operator")

JD = (
    "Senior Backend Engineer\n"
    "Required: 5+ years building production backend services. Strong Python. "
    "Experience operating services on Kubernetes is essential.\n"
    "Preferred: Go, event-driven architectures, mentoring junior engineers.\n"
)

APPLICANTS = {
    "asha": (
        "Asha Nair. Senior Backend Engineer, 7 years. Led the migration of a payments "
        "monolith to microservices in Go, handling 40 million requests per day. Owned "
        "the Kubernetes platform for 12 services and ran the on-call rotation. Built "
        "REST APIs in Python and Django with PostgreSQL schema design."
    ),
    "ben": (
        "Ben Osei. Backend Developer, 3 years. Built internal tools in Python and Flask. "
        "Deployed to Kubernetes using Helm charts maintained by the platform team. "
        "Worked with MySQL for reporting queries."
    ),
    "chi": (
        "Chi Lam. Front-end developer, 1 year. HTML, CSS and React. Built a personal "
        "blog. Completed an online course in JavaScript fundamentals."
    ),
}


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
def worker(uow_factory: Callable[[], UnitOfWork]) -> Worker:
    llm = OllamaClient()
    if not llm.health():
        pytest.skip("ollama unreachable")
    service = ScreenerService(llm=llm, uow_factory=uow_factory)
    return Worker(
        service=service,
        deps=Deps(parser=SandboxedParser(), llm=llm),
        worker_id="worker-live",
        poll_interval_s=0.01,
    )


def seed(service: ScreenerService) -> str:
    position = service.create_position(
        reference="REQ-LIVE", title="Senior Backend Engineer", jd_text=JD, actor=ACTOR
    )
    rubric = service.extract_rubric(position.id, ACTOR)
    service.approve_rubric(rubric.id, ACTOR)

    folder = Path(settings.resumes_dir) / "REQ-LIVE"
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in APPLICANTS.items():
        real_docx(folder / f"{name}.docx", paragraphs=(text,))

    run = service.create_run(position.id, rubric.id, ACTOR)
    service.start_run(run.id, ACTOR)
    return run.id


def test_a_folder_of_resumes_becomes_a_ranked_result(worker: Worker) -> None:
    """The product, in one test.

    JD → rubric → approval → snapshot → screen → rank. Nothing mocked.
    """
    service = worker.service
    run_id = seed(service)

    processed = 0
    while worker.run_once():
        processed += 1

    assert processed == len(APPLICANTS)

    status = service.run_status(run_id)
    assert status.status == "completed"
    assert status.done + status.failed == len(APPLICANTS)
    assert status.failed == 0

    result = service.list_candidates(run_id)
    total = len(result.meets_must_haves) + len(result.missing_must_have) + len(result.needs_review)
    assert total == len(APPLICANTS)

    # Ranking is meaningful: the strongest applicant outranks the weakest.
    ranked = result.meets_must_haves + result.missing_must_have
    scores = {c.filename: c.score for c in ranked}
    if "asha.docx" in scores and "chi.docx" in scores:
        assert scores["asha.docx"] > scores["chi.docx"]


def test_every_candidate_carries_its_evidence(worker: Worker) -> None:
    """A reviewer opening any candidate must see what the verdict rested on.

    Including escalated ones — a flag with no evidence is not a decision anyone
    can make.
    """
    service = worker.service
    run_id = seed(service)
    while worker.run_once():
        pass

    result = service.list_candidates(run_id)
    everyone = result.meets_must_haves + result.missing_must_have + result.needs_review

    for candidate in everyone:
        assert candidate.criteria, candidate.filename
        for criterion in candidate.criteria:
            assert criterion.evidence
            # Rejoined from the rubric, not stored per verdict.
            assert criterion.weight >= 1


def test_a_rerun_costs_no_inference(worker: Worker) -> None:
    """The cache, end to end. A second run over an unchanged folder is bookkeeping.

    This is what makes resumption after a crash cheap rather than a re-run of the
    whole batch (§12.5).
    """
    import time

    service = worker.service
    first = seed(service)
    while worker.run_once():
        pass

    from screener.storage import runs_store

    with service.uow_factory() as tx:
        run = runs_store.get(tx, first)
    assert run is not None

    second = service.create_run(run.position_id, run.rubric_id, ACTOR)
    service.start_run(second.id, ACTOR)

    started = time.monotonic()
    while worker.run_once():
        pass
    elapsed = time.monotonic() - started

    assert service.run_status(second.id).done == len(APPLICANTS)
    # Three real judge calls would take ~15 s; cache hits take well under one.
    assert elapsed < 5.0, f"re-run took {elapsed:.1f}s — the cache is not being hit"
