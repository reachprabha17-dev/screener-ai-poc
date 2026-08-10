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
from screener.models import Actor, Candidate, Criterion, Flag, ScoredCriterion, now
from screener.ports import CacheKey
from screener.service import (
    ConflictError,
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

    def chat_json(
        self, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        if "support_checks" in schema.get("properties", {}):
            # A phase-2 call. Empty is a valid `VerifyOutput`: the verifier
            # agreed with everything and found nothing for the `none` criteria.
            return {"support_checks": [], "absence_checks": []}
        self.extractions += 1
        return {
            "criteria": [
                {"text": "5+ years backend engineering", "must_have": True, "weight": 5},
                {"text": "Kubernetes in production", "must_have": True, "weight": 4},
                {"text": "Strong Python", "must_have": False, "weight": 3},
                {"text": "Mentoring", "must_have": False, "weight": 1},
            ]
        }

    def count_tokens(self, model: str, text: str) -> int:
        return len(text) // 4

    def count_prompt_tokens(self, model: str, system: str, user: str) -> int:
        return 900

    def health(self) -> bool:
        return self._healthy

    def digest(self, model: str) -> str:
        return self._digest

    def ensure_loaded(self, model: str) -> None:
        self.loaded = model

    def unload(self, model: str) -> None:
        self.loaded = None


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

    assert entry["detail"]["judge_digest"] == "sha256:aaa"
    assert len(entry["detail"]["prompt_hash"]) == 64
    assert entry["detail"]["criteria"] == 4


# --- gate: audit and effect are one transaction ------------------------------


def test_a_decision_its_history_row_and_its_audit_entry_commit_together(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    candidate_id = _seed_candidate(service, uow_factory)

    service.record_decision(candidate_id, "advance", "strong payments background", ACTOR)

    with uow_factory() as tx:
        overrides = tx.execute("SELECT * FROM overrides").fetchall()
        entries = audit_store.list_for_entity(tx, "candidate", str(candidate_id))

    assert len(overrides) == 1
    assert overrides[0]["actor_id"] == ACTOR.id
    assert overrides[0]["old_score"] == 7.8  # what it was before the human intervened
    assert overrides[0]["old_decision"] == "undecided"
    assert len(entries) == 1

    # The third write: current state on the candidate itself, so "where does this
    # person stand" is one index probe rather than a walk of the history.
    with uow_factory() as tx:
        candidate = results_store.get(tx, candidate_id)
    assert candidate is not None
    assert candidate.decision == "advance"
    assert candidate.decided_by == ACTOR.id
    assert candidate.decided_at is not None


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


def test_a_decision_needs_a_reason(service: ScreenerService, uow_factory) -> None:  # noqa: ANN001
    """The reason is the record. A decision without one is unexplainable later."""
    candidate_id = _seed_candidate(service, uow_factory)

    with pytest.raises(ServiceError, match="reason"):
        service.record_decision(candidate_id, "advance", "   ", ACTOR)

    with uow_factory() as tx:
        assert tx.execute("SELECT COUNT(*) AS n FROM overrides").fetchone()["n"] == 0


def test_an_unknown_decision_is_refused(service: ScreenerService, uow_factory) -> None:  # noqa: ANN001
    candidate_id = _seed_candidate(service, uow_factory)

    with pytest.raises(ServiceError):
        service.record_decision(candidate_id, "teleport", "why not", ACTOR)


# --- bulk decisions and the sign-off gate (15.5, 23.1.6) ---------------------


def test_a_bulk_decision_skips_everyone_who_needs_review(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """The escalated ones are precisely where the system said *look at this*.

    Sweeping them into a shared reason answers that with a rubber stamp, and the
    escalation would have bought nothing.
    """
    run_id = _seed_run_with_candidates(service, uow_factory)
    with uow_factory() as tx:
        everyone = results_store.list_for_run(tx, run_id)
    escalated = next(c for c in everyone if c.review_required)

    result = service.record_bulk_decision(
        [c.id for c in everyone if c.id], "reject", "does not meet the must-have", ACTOR
    )

    assert escalated.id in result.skipped
    assert escalated.id not in result.decided
    with uow_factory() as tx:
        after = results_store.get(tx, escalated.id) if escalated.id else None
    assert after is not None
    assert after.decision == "undecided", "an escalated candidate was decided in bulk"


def test_a_bulk_decision_reports_which_ones_it_skipped(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """ "377 of your 400" is only actionable if you know which 23 remain."""
    run_id = _seed_run_with_candidates(service, uow_factory)
    with uow_factory() as tx:
        ids = [c.id for c in results_store.list_for_run(tx, run_id) if c.id]

    result = service.record_bulk_decision(ids, "hold", "second round", ACTOR)

    assert sorted(result.decided + result.skipped) == sorted(ids)
    assert result.skipped, "the unscoreable candidate should not have been decided"


def test_sign_off_is_refused_while_an_escalation_has_no_decision(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Sign-off is the artefact saying a human reviewed this run.

    Signing one with untouched escalations makes that artefact false at exactly
    the moment it starts being relied on.
    """
    run_id = _seed_run_with_candidates(service, uow_factory)

    with pytest.raises(ServiceError, match="need review"):
        service.sign_off_run(run_id, ACTOR)

    with uow_factory() as tx:
        row = tx.execute("SELECT reviewed_by FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["reviewed_by"] is None


def test_sign_off_proceeds_once_every_escalation_has_been_answered(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    run_id = _seed_run_with_candidates(service, uow_factory)
    with uow_factory() as tx:
        outstanding = [
            c
            for c in results_store.list_for_run(tx, run_id)
            if c.review_required or c.verification_status == "pending"
        ]
    for candidate in outstanding:
        assert candidate.id is not None
        service.record_decision(candidate.id, "hold", "looked at it", ACTOR)

    service.sign_off_run(run_id, ACTOR)

    with uow_factory() as tx:
        row = tx.execute("SELECT reviewed_by FROM runs WHERE id = ?", (run_id,)).fetchone()
    assert row["reviewed_by"] == ACTOR.id


def test_a_run_with_verification_off_does_not_block_sign_off_forever(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression. `verification_status` defaults to `pending`, and with phase 2
    disabled no job ever moves it — so treating `pending` as outstanding would
    make sign-off permanently impossible on exactly the configuration a small
    deployment is most likely to run.

    Advancing to `done` without a verify phase closes those rows out as
    `skipped`: verification did not run, which is true and is not `pending`.
    """
    monkeypatch.setattr(settings, "verification_enabled", False)
    run_id = _seed_run_with_candidates(service, uow_factory)
    with uow_factory() as tx:
        tx.execute("UPDATE jobs SET status = 'done' WHERE run_id = ?", (run_id,))

    assert service.advance_phase_if_complete(run_id) == "done"

    with uow_factory() as tx:
        statuses = {c.verification_status for c in results_store.list_for_run(tx, run_id)}
    assert statuses == {"skipped"}, statuses


def test_a_candidate_still_awaiting_verification_blocks_sign_off(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Partial verification must never look like completed verification (17.6).

    Phase 2 may yet raise an escalation on this candidate that nobody has seen.
    """
    run_id = _seed_run_with_candidates(service, uow_factory)
    with uow_factory() as tx:
        for candidate in results_store.list_for_run(tx, run_id):
            assert candidate.id is not None
            # Everyone decided, but one is still mid-verification.
            results_store.save_decision(tx, candidate.id, "advance", ACTOR.id, now())
        pending = results_store.list_for_run(tx, run_id)[0]
        assert pending.id is not None
        results_store.save_decision(tx, pending.id, "undecided", ACTOR.id, now())
        tx.execute(
            "UPDATE candidates SET verification_status = 'pending', review_required = 0 "
            "WHERE id = ?",
            (pending.id,),
        )

    with pytest.raises(ServiceError, match="need review"):
        service.sign_off_run(run_id, ACTOR)


# --- optimistic locking and stale claims (23.1.7, 23.1.8) --------------------


def _with_claims(service: ScreenerService, position_id: str) -> list[Criterion]:
    """A draft whose criteria carry claims, which is what staleness is about.

    The fake extraction returns none, matching a pre-v6 rubric — a useful default
    everywhere else and precisely the case these tests are *not* exercising.
    """
    draft = service.extract_rubric(position_id, ACTOR)
    return [
        c.model_copy(update={"claim": f"The candidate satisfies: {c.text}."})
        for c in draft.criteria
    ]


def test_a_second_editor_saving_from_a_stale_version_is_told(
    service: ScreenerService,
) -> None:
    """Two recruiters tuning one rubric in adjacent tabs is the ordinary case.

    Without this the second save silently supersedes the first: no conflict, no
    error, one person's edits gone, and a higher version number to suggest it
    all worked.
    """
    position = service.create_position(reference="REQ-1", title="t", jd_text=JD, actor=ACTOR)
    draft = service.extract_rubric(position.id, ACTOR)

    service.save_rubric(position.id, draft.criteria, ACTOR, base_version=draft.version)

    with pytest.raises(ConflictError, match="edited by someone else"):
        service.save_rubric(position.id, draft.criteria, OTHER, base_version=draft.version)


def test_saving_without_a_base_version_is_still_allowed(service: ScreenerService) -> None:
    """The CLI and the first save have no prior version to be stale about."""
    position = service.create_position(reference="REQ-1", title="t", jd_text=JD, actor=ACTOR)
    draft = service.extract_rubric(position.id, ACTOR)

    saved = service.save_rubric(position.id, draft.criteria, ACTOR)

    assert saved.version == draft.version + 1


def test_editing_a_criterion_without_its_claim_marks_the_claim_stale(
    service: ScreenerService,
) -> None:
    """The claim is what phase 2 verifies against (10.6 A).

    Edit the criterion and leave the claim behind and the verifier goes on
    checking a hypothesis the rubric no longer makes — silently, and in the
    direction that produces confident agreement with the wrong question.
    """
    position = service.create_position(reference="REQ-1", title="t", jd_text=JD, actor=ACTOR)
    saved = service.save_rubric(position.id, _with_claims(service, position.id), ACTOR)

    edited = list(saved.criteria)
    edited[0] = edited[0].model_copy(update={"text": "8+ years backend engineering"})
    second = service.save_rubric(position.id, edited, ACTOR, base_version=saved.version)

    assert second.criteria[0].claim_stale is True
    with pytest.raises(ServiceError, match="regenerate the claims"):
        service.approve_rubric(second.id, ACTOR)


def test_editing_the_text_and_the_claim_together_is_not_stale(
    service: ScreenerService,
) -> None:
    position = service.create_position(reference="REQ-1", title="t", jd_text=JD, actor=ACTOR)
    saved = service.save_rubric(position.id, _with_claims(service, position.id), ACTOR)

    edited = list(saved.criteria)
    edited[0] = edited[0].model_copy(
        update={
            "text": "8+ years backend engineering",
            "claim": "The candidate has at least 8 years of backend engineering experience.",
        }
    )
    second = service.save_rubric(position.id, edited, ACTOR, base_version=saved.version)

    assert second.criteria[0].claim_stale is False
    service.approve_rubric(second.id, ACTOR)  # not blocked


def test_a_rubric_with_no_claims_at_all_still_approves(service: ScreenerService) -> None:
    """Rubrics written before v6 have none, and `verify_support` falls back to
    the criterion text. Treating empty as stale would block approval on a claim
    that was never written rather than one that went out of date."""
    position = service.create_position(reference="REQ-1", title="t", jd_text=JD, actor=ACTOR)
    draft = service.extract_rubric(position.id, ACTOR)
    bare = [c.model_copy(update={"claim": ""}) for c in draft.criteria]
    saved = service.save_rubric(position.id, bare, ACTOR)

    edited = list(saved.criteria)
    edited[0] = edited[0].model_copy(update={"text": "changed entirely"})
    second = service.save_rubric(position.id, edited, ACTOR, base_version=saved.version)

    service.approve_rubric(second.id, ACTOR)


# --- an empty folder is not a completed run (17.6) ---------------------------


def test_a_run_over_an_empty_folder_is_marked_empty(service: ScreenerService) -> None:
    """Otherwise it reaches sign-off as a blank results screen, which reads
    exactly like "we screened everyone and nobody qualified"."""
    position_id, rubric_id = approved_position(service)
    Path(settings.resumes_dir, "REQ-1").mkdir(parents=True, exist_ok=True)

    run = service.create_run(position_id, rubric_id, ACTOR)

    assert run.status == "empty"


# --- the original document (16) ----------------------------------------------


def test_the_candidate_file_path_resolves_inside_the_run_folder(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    candidate_id = _seed_candidate(service, uow_factory)

    path = service.candidate_file_path(candidate_id)

    assert path is not None
    assert path.is_relative_to(Path(settings.resumes_dir).resolve())


def test_a_filename_escaping_the_run_folder_is_refused(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Today the filename comes from a directory scan, so this is not defending
    against current input — it makes the confinement a property of the read path
    so it stays true when someone adds a way to write one."""
    candidate_id = _seed_candidate(service, uow_factory)
    with uow_factory() as tx:
        tx.execute(
            "UPDATE candidates SET filename = ? WHERE id = ?",
            ("../../../etc/passwd", candidate_id),
        )

    assert service.candidate_file_path(candidate_id) is None


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

    assert run.judge_digest == "sha256:aaa"
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
    """A known four-hour wait is fine; an unknown one produces support tickets.

    **Both passes are counted.** A two-phase run quoted at one phase's duration
    is understated by half, and an ETA people plan around is worse wrong than
    absent — they schedule the review for a time the results will not exist.
    """
    position_id, rubric_id = approved_position(service)
    resumes_for("REQ-1", count=10)
    run = service.create_run(position_id, rubric_id, ACTOR)

    status = service.run_status(run.id)

    assert status.eta_seconds == pytest.approx(2 * 10 * settings.seconds_per_resume)


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


def test_purge_removes_failure_captures_from_disk(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork], tmp_path: Path
) -> None:
    """The step 12.10 calls easy to forget and fatal to omit.

    Clearing database columns while model output derived from the résumé sits in
    a failure capture leaves erasure looking implemented — worse than absent,
    because it gets reported as done.
    """
    from screener.logging import write_failure

    _seed_run_with_candidates(service, uow_factory)
    capture = write_failure(
        file_sha256="sha-qualified",
        prompt_hash="p" * 64,
        raw_output='{"evidence": "Asha Nair, Senior Backend Engineer"',
        error="unterminated object",
    )
    assert capture is not None and capture.exists()

    removed = service.purge_candidate("sha-qualified", ACTOR)

    assert removed == 1
    assert not capture.exists()


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


def test_purge_succeeds_when_there_is_nothing_on_disk(
    service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """A healthy run writes no captures at all, and erasure still has to work."""
    _seed_run_with_candidates(service, uow_factory)

    assert service.purge_candidate("sha-qualified", ACTOR) == 0

    with uow_factory() as tx:
        row = tx.execute(
            "SELECT filename, resume_text FROM candidates WHERE file_sha256 = ?",
            ("sha-qualified",),
        ).fetchone()
    assert row["filename"] == "[purged]"
    assert row["resume_text"] is None


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
    monkeypatch.setattr(settings, "judge_digest_pin", "sha256:expected")
    service = ScreenerService(llm=FakeLLM(digest="sha256:actual"), uow_factory=uow_factory)  # type: ignore[arg-type]

    report = service.health()

    assert report.model_digest_matches_pin is False
    assert "judge_digest" in report.detail


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
            judge_digest="sha256:aaa",
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
