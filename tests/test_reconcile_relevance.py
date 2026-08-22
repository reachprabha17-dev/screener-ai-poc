"""Folding the relevance check back onto a candidate (spec 10.5 C).

The invariant worth a property test: this can only ever clear
`Flag.EVIDENCE_IRRELEVANT`, never add a flag `verify_evidence` did not already
raise, and never touch a verdict, a score, or `scoreable`.
"""

from hypothesis import given
from hypothesis import strategies as st

from screener.core.reconcile_relevance import reconcile_relevance
from screener.core.verify_evidence import VerificationResult
from screener.models import Flag, RelevanceCheck, ScoredCriterion


def criterion(criterion_id: str = "C1", *, evidence_irrelevant: bool = True) -> ScoredCriterion:
    return ScoredCriterion(
        id=criterion_id,
        verdict="strong",
        model_verdict="strong",
        evidence="Led the migration of a payments monolith to microservices in Go",
        verified=True,
        match_ratio=1.0,
        longest_span=6,
        evidence_irrelevant=evidence_irrelevant,
        weight=3,
        must_have=True,
    )


def test_a_confident_agreement_clears_the_flag_and_the_review() -> None:
    result = VerificationResult(
        criteria=[criterion("C1")],
        flags=[Flag.EVIDENCE_IRRELEVANT],
        scoreable=True,
        review_required=True,
    )
    checks = [RelevanceCheck(id="C1", related=True, rationale="same subject, different words")]

    updated = reconcile_relevance(result, checks)

    assert updated.criteria[0].evidence_irrelevant is False
    assert Flag.EVIDENCE_IRRELEVANT not in updated.flags
    assert updated.review_required is False
    assert updated.scoreable is True


def test_disagreement_leaves_everything_exactly_as_it_was() -> None:
    result = VerificationResult(
        criteria=[criterion("C1")],
        flags=[Flag.EVIDENCE_IRRELEVANT],
        scoreable=True,
        review_required=True,
    )
    checks = [RelevanceCheck(id="C1", related=False, rationale="different technology entirely")]

    updated = reconcile_relevance(result, checks)

    assert updated.criteria[0].evidence_irrelevant is True
    assert Flag.EVIDENCE_IRRELEVANT in updated.flags
    assert updated.review_required is True


def test_no_answer_for_that_id_is_fail_safe_not_a_shrug() -> None:
    """An id the model dropped, not one it disagreed on — same outcome either way."""
    result = VerificationResult(
        criteria=[criterion("C1")],
        flags=[Flag.EVIDENCE_IRRELEVANT],
        scoreable=True,
        review_required=True,
    )

    updated = reconcile_relevance(result, checks=[])

    assert updated.criteria[0].evidence_irrelevant is True
    assert Flag.EVIDENCE_IRRELEVANT in updated.flags
    assert updated.review_required is True


def test_one_of_two_flagged_criteria_confirmed_leaves_the_flag_for_the_other() -> None:
    """The candidate-level flag is an OR over criteria — one still-irrelevant
    criterion is enough to keep it, and keep review_required, regardless of how
    many others were cleared."""
    result = VerificationResult(
        criteria=[criterion("C1"), criterion("C2")],
        flags=[Flag.EVIDENCE_IRRELEVANT],
        scoreable=True,
        review_required=True,
    )
    checks = [
        RelevanceCheck(id="C1", related=True, rationale="same subject"),
        RelevanceCheck(id="C2", related=False, rationale="unrelated"),
    ]

    updated = reconcile_relevance(result, checks)

    assert updated.criteria[0].evidence_irrelevant is False
    assert updated.criteria[1].evidence_irrelevant is True
    assert Flag.EVIDENCE_IRRELEVANT in updated.flags
    assert updated.review_required is True


def test_clearing_irrelevance_does_not_clear_review_required_from_another_reason() -> None:
    """EVIDENCE_UNVERIFIED on a *different* criterion must keep review_required
    True even once the only EVIDENCE_IRRELEVANT criterion is cleared — clearing
    one reason is not clearing all of them."""
    result = VerificationResult(
        criteria=[criterion("C1"), criterion("C2", evidence_irrelevant=False)],
        flags=[Flag.EVIDENCE_IRRELEVANT, Flag.EVIDENCE_UNVERIFIED],
        scoreable=False,
        review_required=True,
    )
    checks = [RelevanceCheck(id="C1", related=True, rationale="same subject")]

    updated = reconcile_relevance(result, checks)

    assert Flag.EVIDENCE_IRRELEVANT not in updated.flags
    assert Flag.EVIDENCE_UNVERIFIED in updated.flags
    assert updated.review_required is True
    # Untouched by this function either way — not its question to answer.
    assert updated.scoreable is False


@given(
    related=st.booleans(),
    rationale=st.text(max_size=200),
)
def test_never_touches_verdict_score_or_anything_but_the_irrelevance_flag(
    related: bool, rationale: str
) -> None:
    result = VerificationResult(
        criteria=[criterion("C1")],
        flags=[Flag.EVIDENCE_IRRELEVANT],
        scoreable=True,
        review_required=True,
    )
    checks = [RelevanceCheck(id="C1", related=related, rationale=rationale)]

    updated = reconcile_relevance(result, checks)

    before, after = result.criteria[0], updated.criteria[0]
    assert after.verdict == before.verdict
    assert after.model_verdict == before.model_verdict
    assert after.match_ratio == before.match_ratio
    assert after.evidence == before.evidence
    assert after.weight == before.weight
    assert after.must_have == before.must_have
    # The only field this function is allowed to change.
    assert after.evidence_irrelevant == (before.evidence_irrelevant and not related)
