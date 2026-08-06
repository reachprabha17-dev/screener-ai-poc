"""Transactional command/query surface (spec 14, build gate 20 step 13).

The gate names three properties: **actor threaded through**, **audit written in
the same transaction as its effect**, and **no pass-through methods**.

The atomicity tests are the ones that matter. They assert that a failure leaves
*neither* the effect nor its audit row — because the alternative is an override
recorded against a candidate with no record of who made it, which is
indistinguishable from tampering after the fact.

Runs against a real SQLite file. The audit triggers, the foreign keys and the
transaction semantics are the behaviour under test, and none of them exist in a
fake.
"""

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest

from config.settings import settings
from screener.models import Actor, Candidate, Criterion, Flag, ScoredCriterion
from screener.ports import CacheKey
from screener.service import (
    NotFoundError,
    RubricNotApprovedError,
    ScreenerService,
    ServiceError,
)
from screener.storage import audit_store, results_store, runs_store
from screener.storage.connection import apply_migrations, connect
from screener.storage.uow import UnitOfWork

ACTOR = Actor(id="poc-operator", display_name="PoC Operator", roles=frozenset({"admin"}))
OTHER = Actor(id="second-reviewer", display_name="Second Reviewer")

JD = "Senior Backend Engineer. Required: 5+ years backend. Kubernetes essential."


class FakeLLM:
    """Enough of `LLMClient` for the one call the service makes."""

    def __init__(self, *, healthy: bool = True, digest: str = "sha256:aaa") -> None:
        self._healthy = healthy
        self._digest = digest
        self.extractions = 0

    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.extractions += 1
        return {
            "criteria": [
                {"text": "5+ years backend engineering", "must_have": True, "weight": 5},
                {"text": "Kubernetes in production", "must_have": True, "weight": 4},
                {"text": "Strong Python", "must_have": False, "weight": 3},
                {"text": "Mentoring", "must_have": False, "weight": 1},
            ]
        }

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def count_prompt_tokens(self, system: str, user: str) -> int:
        return 900

    def health(self) -> bool:
        return self._healthy

    @property
    def model_digest(self) -> str:
        return self._digest


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


def resumes_for(reference: str, count: int = 3) -> Path:
    folder = Path(settings.resumes_dir) / reference
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (folder / f"cv{i}.pdf").write_bytes(b"%PDF-1.4\n")
    return folder


def approved_position(service: ScreenerService, reference: str = "REQ-1") -> tuple[str, str]:
    position = service.create_position(
        reference=reference, title="Backend Engineer", jd_text=JD, actor=ACTOR
    )
    rubric = service.extract_rubric(position.id, ACTOR)
    service.approve_rubric(rubric.id, ACTOR)
    return position.id, rubric.id


def audit_actions(uow_factory: Callable[[], UnitOfWork]) -> list[str]:
    with uow_factory() as tx:
        return [entry["action"] for entry in audit_store.recent(tx, limit=100)]


# --- gate: the actor reaches the audit log on every mutation -----------------


def test_every_mutating_call_records_its_actor(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Auth is stubbed today (15.2); the *plumbing* is what is expensive to retrofit.

    So the value has to arrive at the audit row now, not once real
    authentication exists.
    """
    position_id, rubric_id = approved_position(service)
    resumes_for("REQ-1")
    run = service.create_run(position_id, rubric_id, ACTOR)
    service.start_run(run.id, ACTOR)
    service.sign_off_run(run.id, OTHER)

    with uow_factory() as tx:
        entries = audit_store.recent(tx, limit=100)

    by_action = {e["action"]: e for e in entries}
    for action in (
        "create_position",
        "extract_rubric",
        "approve_rubric",
        "create_run",
        "start_run",
    ):
        assert by_action[action]["actor_id"] == ACTOR.id, action
    # A different person signed off, and the record says so.
    assert by_action["sign_off_run"]["actor_id"] == OTHER.id


def test_a_second_person_can_approve_override_and_sign_off(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Separation of duties. Regression test for a real defect.

    `runs.reviewed_by`, `rubrics.approved_by` and `overrides.actor_id` are all
    foreign keys into `users`, and only `create_position` seeded a row — so
    anyone who had not raised the requisition was rejected by referential
    integrity when they tried to approve or sign off. Exactly backwards: the
    creator and the approver being different people is the point of an audit
    trail.
    """
    position = service.create_position(reference="REQ-1", title="t", jd_text=JD, actor=ACTOR)
    rubric = service.extract_rubric(position.id, ACTOR)

    # A different person from here on.
    service.approve_rubric(rubric.id, OTHER)
    resumes_for("REQ-1")
    run = service.create_run(position.id, rubric.id, OTHER)
    service.sign_off_run(run.id, OTHER)

    with uow_factory() as tx:
        row = tx.execute("SELECT reviewed_by FROM runs WHERE id = ?", (run.id,)).fetchone()
        approved = tx.execute(
            "SELECT approved_by FROM rubrics WHERE id = ?", (rubric.id,)
        ).fetchone()

    assert row["reviewed_by"] == OTHER.id
    assert approved["approved_by"] == OTHER.id


def test_the_audit_detail_captures_what_determined_the_output(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """A rubric extraction is an LLM decision; the digest and prompt identify it."""
    position = service.create_position(reference="REQ-1", title="t", jd_text=JD, actor=ACTOR)
    service.extract_rubric(position.id, ACTOR)

    with uow_factory() as tx:
        entry = next(e for e in audit_store.recent(tx, limit=50) if e["action"] == "extract_rubric")

    assert entry["detail"]["model_digest"] == "sha256:aaa"
    assert len(entry["detail"]["prompt_hash"]) == 64
    assert entry["detail"]["criteria"] == 4


# --- gate: audit and effect are one transaction ------------------------------


def test_an_override_and_its_audit_row_commit_together(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    candidate_id = _seed_candidate(service, uow_factory)

    service.record_override(candidate_id, "advance", "strong payments background", ACTOR)

    with uow_factory() as tx:
        overrides = tx.execute("SELECT * FROM overrides").fetchall()
        entries = audit_store.list_for_entity(tx, "candidate", str(candidate_id))

    assert len(overrides) == 1
    assert overrides[0]["actor_id"] == ACTOR.id
    assert overrides[0]["old_score"] == 7.8  # what it was before the human intervened
    assert len(entries) == 1


def test_a_failed_mutation_leaves_no_audit_row(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Neither the effect nor its record — the failure mode is the pair diverging.

    An audit row surviving a rolled-back write would describe a decision that
    never happened; the reverse is worse.
    """
    position = service.create_position(reference="REQ-1", title="t", jd_text=JD, actor=ACTOR)

    with pytest.raises(sqlite3.IntegrityError):
        service.create_position(reference="REQ-1", title="duplicate", jd_text=JD, actor=ACTOR)

    with uow_factory() as tx:
        positions = tx.execute("SELECT COUNT(*) AS n FROM positions").fetchone()["n"]
        creates = [e for e in audit_store.recent(tx, 50) if e["action"] == "create_position"]

    assert positions == 1
    assert len(creates) == 1
    assert creates[0]["entity_id"] == position.id


def test_an_override_needs_a_reason(service: ScreenerService, uow_factory) -> None:  # noqa: ANN001
    """The reason is the record. An override without one is unexplainable later."""
    candidate_id = _seed_candidate(service, uow_factory)

    with pytest.raises(ServiceError, match="reason"):
        service.record_override(candidate_id, "advance", "   ", ACTOR)

    with uow_factory() as tx:
        assert tx.execute("SELECT COUNT(*) AS n FROM overrides").fetchone()["n"] == 0


def test_an_unknown_decision_is_refused(service: ScreenerService, uow_factory) -> None:  # noqa: ANN001
    candidate_id = _seed_candidate(service, uow_factory)

    with pytest.raises(ServiceError):
        service.record_override(candidate_id, "teleport", "why not", ACTOR)


# --- the human gate in front of an LLM-written rubric ------------------------


def test_a_run_cannot_be_created_against_an_unapproved_rubric(
    service: ScreenerService,
) -> None:
    """The whole reason an LLM is acceptable at this position (9.1).

    A hallucinated requirement rejects every applicant who lacks something the
    job never asked for — across the entire run, invisibly in each result.
    """
    position = service.create_position(reference="REQ-1", title="t", jd_text=JD, actor=ACTOR)
    rubric = service.extract_rubric(position.id, ACTOR)
    resumes_for("REQ-1")

    with pytest.raises(RubricNotApprovedError):
        service.create_run(position.id, rubric.id, ACTOR)


def test_a_rubric_from_another_position_is_refused(service: ScreenerService) -> None:
    first, _ = approved_position(service, "REQ-1")
    _, other_rubric = approved_position(service, "REQ-2")
    resumes_for("REQ-1")

    with pytest.raises(ServiceError, match="does not belong"):
        service.create_run(first, other_rubric, ACTOR)


def test_editing_a_rubric_creates_a_new_version(service: ScreenerService) -> None:
    """Never in place. A run already scored against v1 must stay explainable."""
    position = service.create_position(reference="REQ-1", title="t", jd_text=JD, actor=ACTOR)
    first = service.extract_rubric(position.id, ACTOR)

    edited = service.save_rubric(
        position.id,
        [
            Criterion(id="C1", text="8+ years backend", must_have=True, weight=5),
            Criterion(id="C2", text="Kubernetes", must_have=True, weight=4),
            Criterion(id="C3", text="Go", weight=2),
            Criterion(id="C4", text="Mentoring", weight=1),
        ],
        ACTOR,
    )

    assert edited.version == first.version + 1
    assert edited.id != first.id
    assert edited.content_hash != first.content_hash


# --- runs: enqueue only ------------------------------------------------------


def test_create_run_snapshots_the_folder(service: ScreenerService, uow_factory) -> None:  # noqa: ANN001
    """A run is a defined set of candidates at a point in time (16.2)."""
    position_id, rubric_id = approved_position(service)
    resumes_for("REQ-1", count=5)

    run = service.create_run(position_id, rubric_id, ACTOR)
    status = service.run_status(run.id)

    assert status.total == 5
    assert status.pending == 5


def test_create_run_freezes_the_reproducibility_inputs(service: ScreenerService) -> None:
    position_id, rubric_id = approved_position(service)
    resumes_for("REQ-1")

    run = service.create_run(position_id, rubric_id, ACTOR)

    assert run.model_digest == "sha256:aaa"
    assert len(run.prompt_hash) == 64
    assert run.num_ctx == settings.num_ctx
    assert run.redaction_on == settings.redact_pii
    assert run.status == "pending"


def test_files_added_later_need_an_explicit_rescan(service: ScreenerService) -> None:
    """Nothing is silently added mid-run.

    A ranking computed over a set that changed underneath it describes nothing.
    """
    position_id, rubric_id = approved_position(service)
    folder = resumes_for("REQ-1", count=2)
    run = service.create_run(position_id, rubric_id, ACTOR)

    (folder / "late-arrival.pdf").write_bytes(b"%PDF-1.4\n")

    assert service.run_status(run.id).total == 2  # unchanged until asked
    assert service.rescan_run(run.id, ACTOR) == 1
    assert service.run_status(run.id).total == 3


def test_starting_an_empty_run_is_refused(service: ScreenerService) -> None:
    position_id, rubric_id = approved_position(service)
    resumes_for("REQ-1", count=0)

    run = service.create_run(position_id, rubric_id, ACTOR)

    with pytest.raises(ServiceError, match="no queued files"):
        service.start_run(run.id, ACTOR)


def test_aborting_a_run_keeps_it_resumable(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    from screener.storage import jobs_store

    position_id, rubric_id = approved_position(service)
    resumes_for("REQ-1", count=3)
    run = service.create_run(position_id, rubric_id, ACTOR)
    with uow_factory() as tx:
        jobs_store.claim_next(tx, "worker-1")

    service.abort_run(run.id, ACTOR)

    status = service.run_status(run.id)
    assert status.status == "aborted"
    assert status.pending == 3  # the claimed job came back
    assert "abort_run" in audit_actions(uow_factory)


def test_run_status_surfaces_the_eta(service: ScreenerService) -> None:
    """A known four-hour wait is fine; an unknown one produces support tickets."""
    position_id, rubric_id = approved_position(service)
    resumes_for("REQ-1", count=10)
    run = service.create_run(position_id, rubric_id, ACTOR)

    status = service.run_status(run.id)

    assert status.eta_seconds == pytest.approx(10 * settings.seconds_per_resume)


def test_unknown_ids_raise_not_found(service: ScreenerService) -> None:
    for call in (
        lambda: service.run_status("nope"),
        lambda: service.list_candidates("nope"),
        lambda: service.get_candidate(999),
        lambda: service.approve_rubric("nope", ACTOR),
        lambda: service.rescan_run("nope", ACTOR),
    ):
        with pytest.raises(NotFoundError):
            call()


# --- reading results back ----------------------------------------------------


def test_listing_returns_three_disjoint_partitions(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    run_id = _seed_run_with_candidates(service, uow_factory)

    result = service.list_candidates(run_id)

    ids = [
        c.file_sha256
        for group in (result.meets_must_haves, result.missing_must_have, result.needs_review)
        for c in group
    ]
    assert sorted(ids) == ["sha-qualified", "sha-unqualified", "sha-unscoreable"]
    assert len(result.meets_must_haves) == 1
    assert len(result.missing_must_have) == 1
    assert len(result.needs_review) == 1


def test_weight_and_must_have_are_rejoined_from_the_rubric(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """They are not stored per verdict — a second copy is a second source of truth.

    This is the orchestration that makes `get_candidate` more than a forwarding
    call: without it the reviewer sees every criterion as weight 1, non-mandatory.
    """
    run_id = _seed_run_with_candidates(service, uow_factory)
    candidate = service.list_candidates(run_id).meets_must_haves[0]

    first = next(c for c in candidate.criteria if c.id == "C1")
    assert first.weight == 5
    assert first.must_have is True


def test_escalation_rate_is_reported_live(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Surfaced during the run, not discovered at the end (18.2)."""
    run_id = _seed_run_with_candidates(service, uow_factory)

    assert service.run_status(run_id).escalation_rate == pytest.approx(1 / 3, abs=0.01)


# --- erasure -----------------------------------------------------------------


def test_purge_removes_trace_files_from_disk(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork], tmp_path: Path
) -> None:
    """The step 12.6 calls easy to forget and fatal to omit.

    Clearing database columns while a full copy of the resume sits in a trace
    file leaves erasure looking implemented — worse than absent, because it gets
    reported as done.
    """
    from screener.storage import traces_store

    run_id = _seed_run_with_candidates(service, uow_factory)
    trace = tmp_path / "trace.jsonl"
    trace.write_text('{"resume": "Asha Nair, Senior Backend Engineer"}')
    with uow_factory() as tx:
        traces_store.record(tx, run_id, "sha-qualified", trace)

    removed = service.purge_candidate("sha-qualified", ACTOR)

    assert removed == 1
    assert not trace.exists()


def test_purge_keeps_a_non_identifying_audit_stub(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Records *that* an erasure happened without re-recording the person."""
    _seed_run_with_candidates(service, uow_factory)

    service.purge_candidate("sha-qualified", ACTOR)

    with uow_factory() as tx:
        entry = next(e for e in audit_store.recent(tx, 50) if e["action"] == "purge_candidate")

    assert entry["actor_id"] == ACTOR.id
    assert entry["entity_id"] == "sha-qualified"[:12]


def test_purge_survives_an_already_missing_trace_file(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork], tmp_path: Path
) -> None:
    """The index is the record; the file is a copy that may already be gone."""
    from screener.storage import traces_store

    run_id = _seed_run_with_candidates(service, uow_factory)
    with uow_factory() as tx:
        traces_store.record(tx, run_id, "sha-qualified", tmp_path / "never-written.jsonl")

    assert service.purge_candidate("sha-qualified", ACTOR) == 0


# --- health ------------------------------------------------------------------


def test_health_reports_each_dependency_separately(service: ScreenerService) -> None:
    """ "Unhealthy" alone tells an operator nothing at 2am."""
    report = service.health()

    assert report.llm_reachable is True
    assert report.migrations_current is True
    assert report.ok is True
    assert report.app_version


def test_health_never_raises_when_the_model_is_unreachable(
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    """A health check that throws takes down the thing reporting the problem."""

    class Broken(FakeLLM):
        def health(self) -> bool:
            raise ConnectionError("connection refused")

    service = ScreenerService(llm=Broken(), uow_factory=uow_factory)  # type: ignore[arg-type]
    report = service.health()

    assert report.ok is False
    assert report.llm_reachable is False
    assert "llm" in report.detail


def test_a_digest_that_drifts_from_the_pin_is_reported(
    uow_factory: Callable[[], UnitOfWork], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every decision stored under the old weights has stopped being reproducible."""
    monkeypatch.setattr(settings, "model_digest_pin", "sha256:expected")
    service = ScreenerService(llm=FakeLLM(digest="sha256:actual"), uow_factory=uow_factory)  # type: ignore[arg-type]

    report = service.health()

    assert report.model_digest_matches_pin is False
    assert "model_digest" in report.detail


# --- the boundary that must not move -----------------------------------------


def test_the_service_never_imports_the_pipeline() -> None:
    """14's hard rule: it enqueues, the worker executes.

    A service that screens collapses the process boundaries in 2 and makes the
    API unresponsive for the length of a 78-minute batch. Enforced fully by
    `test_layering.py` at step 20; asserted here because this is the module the
    rule is about.
    """
    import ast

    source = Path("screener/service.py").read_text(encoding="utf-8")
    imported = {
        node.module
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.ImportFrom) and node.module
    }

    assert not any(m.startswith("screener.pipeline") for m in imported)
    assert not any(m.startswith("screener.intake") for m in imported)


# --- helpers -----------------------------------------------------------------


def _seed_candidate(service: ScreenerService, uow_factory: Callable[[], UnitOfWork]) -> int:
    run_id = _seed_run_with_candidates(service, uow_factory)
    with uow_factory() as tx:
        row = tx.execute(
            "SELECT id FROM candidates WHERE run_id = ? AND file_sha256 = 'sha-qualified'",
            (run_id,),
        ).fetchone()
    return int(row["id"])


def _seed_run_with_candidates(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> str:
    """One qualified, one missing a must-have, one unscoreable."""
    position_id, rubric_id = approved_position(service)
    resumes_for("REQ-1", count=1)
    run = service.create_run(position_id, rubric_id, ACTOR)

    def key(sha: str) -> CacheKey:
        return CacheKey(
            file_sha256=sha,
            position_id=position_id,
            rubric_hash="r" * 64,
            model_digest="sha256:aaa",
            prompt_hash="p" * 64,
            redaction_on=True,
            num_ctx=settings.num_ctx,
            app_version="v0.1.0",
        )

    def candidate(sha: str, **kwargs: Any) -> Candidate:
        return Candidate(
            run_id=run.id,
            filename=f"{sha}.pdf",
            file_sha256=sha,
            criteria=[
                ScoredCriterion(
                    id="C1",
                    verdict="strong",
                    model_verdict="strong",
                    evidence="Senior Backend Engineer with 7 years of experience",
                    verified=True,
                    match_ratio=1.0,
                    longest_span=8,
                    weight=1,  # placeholder; the service rejoins the real value
                    must_have=False,
                )
            ],
            **kwargs,
        )

    with uow_factory() as tx:
        results_store.save(
            tx,
            run.id,
            candidate("sha-qualified", score=7.8, band="A", must_haves_met=True),
            key("sha-qualified"),
        )
        results_store.save(
            tx,
            run.id,
            candidate(
                "sha-unqualified",
                score=4.2,
                band="C",
                must_haves_met=False,
                flags=[Flag.MISSING_MUST_HAVE],
            ),
            key("sha-unqualified"),
        )
        results_store.save(
            tx,
            run.id,
            candidate(
                "sha-unscoreable",
                score=None,
                must_haves_met=False,
                scoreable=False,
                review_required=True,
                flags=[Flag.EVIDENCE_UNVERIFIED],
            ),
            key("sha-unscoreable"),
        )
        runs_store.set_status(tx, run.id, "running")

    return run.id
