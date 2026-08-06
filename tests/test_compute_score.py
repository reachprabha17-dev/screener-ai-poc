"""Scoring arithmetic and the must-have gate (spec 10.4, 18.3)."""

import pytest
from conftest import make_rubric, score_all, scored
from hypothesis import given
from hypothesis import strategies as st

from screener.core.compute_score import compute_score
from screener.models import VERDICT_VALUE, Flag, Verdict

VERDICTS: list[Verdict] = ["strong", "partial", "none"]


def test_worked_example_from_spec() -> None:
    """10.4: C1(mh,3) C2(mh,3) C3(mh,2) C4(1) / strong strong none strong."""
    rubric = make_rubric(("C1", True, 3), ("C2", True, 3), ("C3", True, 2), ("C4", False, 1))
    result = compute_score(score_all(rubric, "strong", "strong", "none", "strong"), rubric)

    assert result.score == 7.8  # (3 + 3 + 0 + 1) / 9 = 0.778
    assert result.must_haves_met is False
    assert Flag.MISSING_MUST_HAVE in result.flags
    # The score is NOT capped — partitioning, not capping, separates the unqualified.
    assert result.score > 4.0


def test_all_strong_is_ten() -> None:
    rubric = make_rubric(("C1", True, 3), ("C2", False, 2), ("C3", False, 1), ("C4", False, 1))
    result = compute_score(score_all(rubric, "strong", "strong", "strong", "strong"), rubric)
    assert result.score == 10.0
    assert result.must_haves_met is True
    assert result.review_required is False


def test_all_none_is_zero() -> None:
    rubric = make_rubric(("C1", False, 1), ("C2", False, 1), ("C3", False, 1), ("C4", False, 1))
    result = compute_score(score_all(rubric, "none", "none", "none", "none"), rubric)
    assert result.score == 0.0
    assert result.must_haves_met is True  # no must-haves to miss


def test_a_missing_must_have_does_not_escalate() -> None:
    """The partition *is* the outcome (10.4) — no human adjudication needed.

    Measured regression: flagging these for review drove the end-to-end
    escalation rate to 100% against a 3% budget. On any real corpus most
    applicants fail at least one hard requirement, so escalating them all puts
    the entire run in the review queue — the 18.2 failure where oversight
    collapses into rubber-stamping because nobody can read that much.

    Escalation is for what the *system* could not resolve, not for candidates it
    resolved against.
    """
    rubric = make_rubric(("C1", True, 3), ("C2", False, 1), ("C3", False, 1), ("C4", False, 1))
    result = compute_score(score_all(rubric, "none", "strong", "strong", "strong"), rubric)

    assert result.must_haves_met is False
    assert Flag.MISSING_MUST_HAVE in result.flags
    assert result.review_required is False


def test_partial_must_have_stays_qualified_but_escalates() -> None:
    """A half-satisfied hard requirement is a human call, not an arithmetic one."""
    rubric = make_rubric(("C1", True, 3), ("C2", False, 1), ("C3", False, 1), ("C4", False, 1))
    result = compute_score(score_all(rubric, "partial", "strong", "strong", "strong"), rubric)

    assert result.must_haves_met is True
    assert result.review_required is True
    assert Flag.MISSING_MUST_HAVE not in result.flags


def test_denominator_is_the_rubric_not_the_returned_set() -> None:
    """A missing criterion must not shrink the denominator and inflate the score.

    Guards the arithmetic independently of validate_verdicts (10.3): if that
    check were ever bypassed, a dropped criterion would otherwise read as a
    perfect score.
    """
    rubric = make_rubric(("C1", False, 1), ("C2", False, 1), ("C3", False, 1), ("C4", False, 1))
    partial_return = [scored(rubric.criteria[0], "strong")]  # 3 of 4 criteria missing

    result = compute_score(partial_return, rubric)

    assert result.score == 2.5  # 1/4, not 10.0
    assert result.score != 10.0


def test_empty_rubric_raises() -> None:
    rubric = make_rubric(("C1", False, 1), ("C2", False, 1), ("C3", False, 1), ("C4", False, 1))
    rubric.criteria.clear()
    with pytest.raises(ValueError, match="empty rubric"):
        compute_score([], rubric)


# --- Property-based invariants (18.3) --------------------------------------


@given(
    verdicts=st.lists(st.sampled_from(VERDICTS), min_size=4, max_size=12),
    weights=st.lists(st.integers(min_value=1, max_value=5), min_size=4, max_size=12),
)
def test_score_always_within_bounds(verdicts: list[Verdict], weights: list[int]) -> None:
    n = min(len(verdicts), len(weights))
    rubric = make_rubric(*[(f"C{i}", False, weights[i]) for i in range(n)])
    result = compute_score(score_all(rubric, *verdicts[:n]), rubric)
    assert 0.0 <= result.score <= 10.0


@given(
    verdicts=st.lists(st.sampled_from(VERDICTS), min_size=4, max_size=12),
    index=st.integers(min_value=0, max_value=11),
)
def test_upgrading_a_verdict_never_lowers_the_score(verdicts: list[Verdict], index: int) -> None:
    rubric = make_rubric(*[(f"C{i}", False, 1) for i in range(len(verdicts))])
    i = index % len(verdicts)

    before = compute_score(score_all(rubric, *verdicts), rubric).score

    upgraded = list(verdicts)
    order = ["none", "partial", "strong"]
    upgraded[i] = order[min(order.index(verdicts[i]) + 1, 2)]
    after = compute_score(score_all(rubric, *upgraded), rubric).score

    assert after >= before


@given(verdicts=st.lists(st.sampled_from(VERDICTS), min_size=4, max_size=12))
def test_score_matches_the_definition(verdicts: list[Verdict]) -> None:
    rubric = make_rubric(*[(f"C{i}", False, 1) for i in range(len(verdicts))])
    result = compute_score(score_all(rubric, *verdicts), rubric)
    expected = round(sum(VERDICT_VALUE[v] for v in verdicts) / len(verdicts) * 10, 1)
    assert result.score == expected
