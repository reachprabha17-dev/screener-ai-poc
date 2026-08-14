"""Folding verifier output onto a candidate (spec 10.6 C, build gate 20 step 11).

The gates: the verifier never mutates verdict or score, its absence quote is
re-verified through stage B, and a fabricated absence quote is discarded.

The first is a property test rather than an example, because it is an invariant
about *every* verifier output including the malformed and adversarial ones —
"a second model must not be able to change an outcome by itself" is not a
statement one example can carry.
"""

from hypothesis import given
from hypothesis import strategies as st

from screener.core.reconcile_judge import escalation_reasons_for, reconcile_judge
from screener.models import (
    AbsenceCheck,
    Candidate,
    EscalationReason,
    Flag,
    ScoredCriterion,
    SupportCheck,
    VerifyOutput,
)

RESUME = (
    "Asha Nair. Senior Backend Engineer, 2019-2024. "
    "Python listed in skills. Led the payments platform at a retail bank."
)

VERDICTS = ["strong", "partial", "none"]
SUPPORTS = ["supported", "insufficient", "contradicted"]


def criterion(
    criterion_id: str = "C1",
    verdict: str = "strong",
    *,
    must_have: bool = False,
    verified: bool = True,
) -> ScoredCriterion:
    return ScoredCriterion(
        id=criterion_id,
        verdict=verdict,  # type: ignore[arg-type]
        model_verdict=verdict,  # type: ignore[arg-type]
        evidence="Python listed in skills",
        verified=verified,
        match_ratio=1.0,
        longest_span=4,
        weight=3,
        must_have=must_have,
    )


def candidate(*criteria: ScoredCriterion, **overrides: object) -> Candidate:
    base: dict[str, object] = {
        "run_id": "run1",
        "filename": "asha_nair.pdf",
        "file_sha256": "abc123",
        "sent_text": RESUME,
        "resume_text": RESUME,
        "score": 7.8,
        "band": "A",
        "must_haves_met": True,
        "criteria": list(criteria) or [criterion()],
    }
    base.update(overrides)
    return Candidate(**base)  # type: ignore[arg-type]


# --- the invariant -----------------------------------------------------------


@given(
    verdicts=st.lists(st.sampled_from(VERDICTS), min_size=1, max_size=6),
    supports=st.lists(st.sampled_from(SUPPORTS), min_size=1, max_size=6),
    suggested=st.lists(st.sampled_from(VERDICTS), min_size=1, max_size=6),
    confirmed=st.booleans(),
)
def test_the_verifier_can_never_change_a_verdict_or_a_score(
    verdicts: list[str], supports: list[str], suggested: list[str], confirmed: bool
) -> None:
    """1.9: the verifier escalates and proposes. It never overrules.

    Whatever it says, about however many criteria, in whatever combination —
    the numbers a candidate is ranked on come out the other side untouched.
    """
    criteria = [criterion(f"C{i}", v) for i, v in enumerate(verdicts)]
    before = candidate(*criteria)
    output = VerifyOutput(
        support_checks=[
            SupportCheck(
                id=f"C{i}",
                support=s,  # type: ignore[arg-type]
                suggested_verdict=suggested[i % len(suggested)],  # type: ignore[arg-type]
                rationale="because",
            )
            for i, s in enumerate(supports)
            if i < len(criteria)
        ],
        absence_checks=[
            AbsenceCheck(id=f"C{i}", confirmed_absent=confirmed, found_evidence="Python")
            for i in range(len(criteria))
        ],
    )

    after = reconcile_judge(before, output)

    assert after.score == before.score
    assert after.band == before.band
    assert after.must_haves_met == before.must_haves_met
    assert [c.verdict for c in after.criteria] == [c.verdict for c in before.criteria]
    assert [c.model_verdict for c in after.criteria] == [c.model_verdict for c in before.criteria]
    assert [c.id for c in after.criteria] == [c.id for c in before.criteria]


# --- support checks ----------------------------------------------------------


def test_disagreement_is_recorded_with_its_alternative() -> None:
    """A bare "I disagree" is not something a reviewer can act on."""
    before = candidate(criterion("C1", "strong"))
    output = VerifyOutput(
        support_checks=[
            SupportCheck(
                id="C1",
                support="insufficient",
                suggested_verdict="partial",
                rationale="Skills-section mention only; no production context.",
            )
        ]
    )

    after = reconcile_judge(before, output)

    assert after.criteria[0].verdict == "strong", "the verdict is not the verifier's to change"
    assert after.criteria[0].support == "insufficient"
    assert after.criteria[0].suggested_verdict == "partial"
    assert after.criteria[0].verifier_rationale.startswith("Skills-section")
    assert Flag.JUDGE_DISAGREES in after.flags
    assert EscalationReason.JUDGE_DISAGREEMENT in after.escalation_reasons
    assert after.review_required is True


def test_agreement_is_recorded_too_and_escalates_nothing() -> None:
    """ "Ran and agreed" must be distinguishable from "has not run" (17.6)."""
    before = candidate(criterion("C1", "strong"))
    output = VerifyOutput(
        support_checks=[
            SupportCheck(id="C1", support="supported", suggested_verdict="strong", rationale="")
        ]
    )

    after = reconcile_judge(before, output)

    assert after.criteria[0].support == "supported"
    assert after.escalation_reasons == []
    assert after.review_required is False
    assert after.verification_status == "done"


def test_a_check_for_an_unknown_criterion_is_ignored() -> None:
    """The model inventing an id must not attach a judgment to nothing."""
    before = candidate(criterion("C1"))
    output = VerifyOutput(
        support_checks=[
            SupportCheck(id="C99", support="contradicted", suggested_verdict="none", rationale="x")
        ]
    )

    after = reconcile_judge(before, output)

    assert [c.id for c in after.criteria] == ["C1"]
    assert after.flags == []


# --- absence checks ----------------------------------------------------------


def test_a_quoted_find_is_re_verified_and_escalates() -> None:
    """The judge said absent, the verifier found real text: a human decides."""
    before = candidate(criterion("C1", "none", must_have=True))
    output = VerifyOutput(
        absence_checks=[
            AbsenceCheck(
                id="C1",
                confirmed_absent=False,
                found_evidence="Led the payments platform at a retail bank",
            )
        ]
    )

    after = reconcile_judge(before, output)

    assert after.criteria[0].verdict == "none", "still none — the reviewer decides, not the model"
    assert after.criteria[0].absence_confirmed is False
    assert after.criteria[0].absence_evidence.startswith("Led the payments platform")
    assert Flag.UNVERIFIED_ABSENCE in after.flags
    assert EscalationReason.ABSENCE_FOUND in after.escalation_reasons


def test_a_fabricated_find_is_discarded() -> None:
    """Requiring a quote and re-checking it stops one hallucination overriding another.

    Without stage B here, the verifier's invented evidence would escalate a
    candidate and be shown to a reviewer as text from their resume.
    """
    before = candidate(criterion("C1", "none"))
    output = VerifyOutput(
        absence_checks=[
            AbsenceCheck(
                id="C1",
                confirmed_absent=False,
                found_evidence="Fifteen years of Kubernetes at a hyperscaler",
            )
        ]
    )

    after = reconcile_judge(before, output)

    assert after.criteria[0].absence_evidence == ""
    assert Flag.UNVERIFIED_ABSENCE not in after.flags
    assert after.escalation_reasons == []


def test_a_confirmed_absence_escalates_nothing() -> None:
    before = candidate(criterion("C1", "none"))
    output = VerifyOutput(absence_checks=[AbsenceCheck(id="C1", confirmed_absent=True)])

    after = reconcile_judge(before, output)

    assert after.criteria[0].absence_confirmed is True
    assert after.escalation_reasons == []


# --- escalation reasons ------------------------------------------------------


def test_phase_one_review_survives_an_agreeable_verifier() -> None:
    """A candidate already flagged must not be unflagged by phase 2 agreeing.

    The guide's `review_required = bool(escalation_reasons)` clears it; that
    would drop an injection-flagged candidate out of the queue the moment the
    verifier liked their evidence.
    """
    before = candidate(criterion("C1"), flags=[Flag.SUSPECTED_INJECTION], review_required=True)

    after = reconcile_judge(before, VerifyOutput())

    assert after.review_required is True
    assert EscalationReason.SUSPECTED_INJECTION in after.escalation_reasons


def test_reasons_are_derived_from_flags_not_accumulated() -> None:
    """One place decides why a person is asked to look (15.4)."""
    reasons = escalation_reasons_for(
        [Flag.EVIDENCE_UNVERIFIED, Flag.PARSER_CRASHED, Flag.SANITIZED_TEXT], []
    )

    assert reasons == [
        EscalationReason.UNPROCESSABLE,
        EscalationReason.UNVERIFIED_EVIDENCE,
    ]


def test_a_half_met_must_have_is_its_own_reason() -> None:
    """Read off the verdicts — no flag carries a fact already in the record."""
    reasons = escalation_reasons_for([], [criterion("C1", "partial", must_have=True)])

    assert reasons == [EscalationReason.PARTIAL_MUST_HAVE]


def test_an_unmet_must_have_is_an_outcome_not_an_escalation() -> None:
    """The unqualified partition is the answer. Escalating it measured 100%."""
    reasons = escalation_reasons_for(
        [Flag.MISSING_MUST_HAVE], [criterion("C1", "none", must_have=True)]
    )

    assert reasons == []
