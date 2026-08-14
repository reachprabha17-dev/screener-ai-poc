"""Role-scoped views and highlight translation (spec 15.1, 15.2, 15.3).

The read layer is the human-oversight surface, and whether oversight is real is
entirely a function of whether this screen shows enough to *disagree*. Two things
can break that without any test noticing: a field that leaks to someone who
should not have it, and a highlight that lands on the wrong words. Both are
silent — the page renders either way.
"""

from screener.core.redact_pii import redact_pii
from screener.core.verify_evidence import align
from screener.models import Actor, Candidate, ScoredCriterion
from screener.schemas import (
    CandidateAuditResponse,
    candidate_response,
    criterion_view,
    evidence_status,
)

RECRUITER = Actor(id="rec-1", display_name="Rae", roles=frozenset({"recruiter"}))
AUDITOR = Actor(id="aud-1", display_name="Ada", roles=frozenset({"auditor"}))

RESUME = (
    "Asha Nair. Senior Backend Engineer, 2019-2024. Managed EKS clusters for 40 services. "
    "Led the payments platform at a retail bank."
)


def scored(
    criterion_id: str = "C1",
    evidence: str = "Managed EKS clusters for 40 services",
    *,
    verdict: str = "strong",
    resume: str = RESUME,
    **overrides: object,
) -> ScoredCriterion:
    result = align(evidence, resume)
    fields: dict[str, object] = {
        "id": criterion_id,
        "verdict": verdict,
        "model_verdict": verdict,
        "evidence": evidence,
        "verified": True,
        "match_ratio": result.ratio,
        "longest_span": result.longest_span,
        "match_blocks": result.blocks,
        "text": "Kubernetes in production",
        "weight": 2,
        "must_have": False,
    }
    return ScoredCriterion(**{**fields, **overrides})  # type: ignore[arg-type]


def candidate(*criteria: ScoredCriterion, resume: str = RESUME, **overrides: object) -> Candidate:
    fields: dict[str, object] = {
        "run_id": "run-1",
        "filename": "asha_nair.pdf",
        "file_sha256": "sha-1",
        "resume_text": resume,
        "sent_text": resume,
        "score": 7.2,
        "band": "B",
        "criteria": list(criteria),
    }
    return Candidate(**{**fields, **overrides})  # type: ignore[arg-type]


# --- 15.2 field exposure -----------------------------------------------------


def test_a_recruiter_never_sees_the_pre_verification_verdict() -> None:
    """ "The model said strong, we corrected it to none" invites second-guessing
    a correction made on unambiguous grounds (10.5 A)."""
    view = candidate_response(candidate(scored(verdict="none", model_verdict="strong")), RECRUITER)

    assert not hasattr(view.criteria[0], "model_verdict")


def test_a_recruiter_never_sees_the_text_the_model_actually_read() -> None:
    """`sent_text` is redacted. Two versions of one document on one screen
    prompts "which is real?" about the thing whose only job is to be read."""
    view = candidate_response(candidate(scored()), RECRUITER)

    assert not hasattr(view, "sent_text")


def test_an_auditor_sees_the_numbers_and_the_redacted_text() -> None:
    view = candidate_response(candidate(scored()), AUDITOR)

    assert isinstance(view, CandidateAuditResponse)
    assert view.sent_text == RESUME
    assert view.criteria[0].match_ratio > 0
    assert view.criteria[0].model_verdict == "strong"


def test_the_role_comes_from_the_actor_not_from_the_caller() -> None:
    """There is no `audit=True` a handler could pass by mistake."""
    unprivileged = Actor(id="x", roles=frozenset({"recruiter", "hiring_manager"}))

    assert not isinstance(
        candidate_response(candidate(scored()), unprivileged), CandidateAuditResponse
    )


# --- 15.3 highlight translation ----------------------------------------------


def test_highlights_address_the_text_hr_actually_reads() -> None:
    """The offsets that come out of stage B point into `sent_text`.

    Redaction changes length at every substitution, so an untranslated offset
    does not land slightly off — it lands further off with every redaction above
    it in the document.
    """
    resume = "Reach Asha at asha.nair@example.com. Managed EKS clusters for 40 services."
    sent, _report, span_map = redact_pii(resume)
    assert sent != resume, "fixture no longer redacts anything"

    evidence = "Managed EKS clusters"
    result = align(evidence, sent)
    view = criterion_view(
        scored(evidence=evidence, resume=sent, match_blocks=result.blocks),
        candidate(resume=resume, redaction_map=span_map, sent_text=sent),
    )

    span = view.highlights[0]
    assert resume[span.start : span.end].startswith("Managed EKS")


def test_an_untranslated_highlight_would_have_been_wrong() -> None:
    """The regression this guards against, stated as a fact about the fixture.

    Without translation the same offsets read from `resume_text` land on
    different words — which is what a reviewer would have seen.
    """
    resume = "Reach Asha at asha.nair@example.com. Managed EKS clusters for 40 services."
    sent, _report, span_map = redact_pii(resume)
    result = align("Managed EKS clusters", sent)
    raw = result.blocks[0]

    assert resume[raw.doc_start : raw.doc_end] != sent[raw.doc_start : raw.doc_end]


# --- 15.1 evidence status ----------------------------------------------------


def test_a_none_verdict_is_not_applicable_rather_than_unverified() -> None:
    """Nothing was quoted, so there is nothing to have verified. `unverified`
    would put a warning badge on the system working correctly."""
    assert evidence_status(scored(verdict="none", verified=False, match_ratio=0.0)) == (
        "not_applicable"
    )


def test_a_quote_that_only_partly_aligned_is_badged_partial() -> None:
    """The reviewer's actual question is *which part* was not in the resume."""
    assert evidence_status(scored(match_ratio=0.72)) == "partial"
    assert evidence_status(scored(match_ratio=1.0)) == "verified"
    assert evidence_status(scored(verified=False, match_ratio=0.2)) == "unverified"


# --- 15.5 the verifier panel -------------------------------------------------


def test_the_verifier_panel_is_absent_when_the_two_models_agree() -> None:
    """A panel that always appears is furniture. One that appears only on
    disagreement is a signal."""
    view = criterion_view(scored(support="supported"), candidate(scored()))

    assert view.verifier is None


def test_a_contradiction_is_surfaced_with_the_suggestion_and_the_reason() -> None:
    criterion = scored(
        support="contradicted",
        suggested_verdict="none",
        verifier_rationale="Skills-section mention only; no production context.",
    )

    view = criterion_view(criterion, candidate(criterion))

    assert view.verifier is not None
    assert view.verifier.disagrees is True
    assert view.verifier.suggested_verdict == "none"
    assert "production context" in view.verifier.rationale


def test_evidence_found_for_an_absent_criterion_is_located_in_the_resume() -> None:
    """The absence check found something. A reviewer needs to see *where*."""
    found = "Led the payments platform at a retail bank"
    criterion = scored(
        verdict="none",
        verified=False,
        evidence="",
        match_blocks=[],
        match_ratio=0.0,
        absence_confirmed=False,
        absence_evidence=found,
    )

    view = criterion_view(criterion, candidate(criterion))

    assert view.verifier is not None
    assert view.verifier.found_evidence == found
    span = view.verifier.found_highlights[0]
    assert "payments platform" in RESUME[span.start : span.end]


def test_a_confirmed_absence_raises_no_panel() -> None:
    """Both models agree the criterion is missing. That is not a disagreement."""
    criterion = scored(verdict="none", verified=False, absence_confirmed=True)

    assert criterion_view(criterion, candidate(criterion)).verifier is None
