"""Negation detection (spec 10.5 C, build gate 20 step 5).

The two named gates are `"no production Kubernetes"` flagged and
`"no-code platform"` not. The second is the harder one: tokenization drops the
hyphen, so a product category and a denial arrive at this module looking
identical.
"""

from screener.core.detect_negation import detect_negation
from screener.core.verify_evidence import align
from screener.models import Flag, ScoredCriterion


def scored(evidence: str, document: str, *, verdict: str = "strong") -> ScoredCriterion:
    """A criterion carrying real blocks, produced by the real aligner.

    Hand-written offsets would let this suite pass while the two modules
    disagreed about where a quote sits.
    """
    result = align(evidence, document)
    return ScoredCriterion(
        id="C1",
        verdict=verdict,  # type: ignore[arg-type]
        model_verdict=verdict,  # type: ignore[arg-type]
        evidence=evidence,
        verified=True,
        match_ratio=result.ratio,
        longest_span=result.longest_span,
        match_blocks=result.blocks,
        weight=3,
        must_have=False,
    )


def test_a_denial_quoted_as_support_is_flagged() -> None:
    """The case that verifies at 1.00 while meaning the opposite."""
    document = "Backend engineer. Has no production Kubernetes experience to date."

    result = detect_negation([scored("production Kubernetes experience", document)], document)

    assert result.criteria[0].negation_suspected is True
    assert result.flags == [Flag.NEGATION_SUSPECTED]
    assert result.review_required is True


def test_a_hyphenated_compound_is_not_a_negation() -> None:
    """`no-code` is a product category. Tokenization cannot tell; the source can.

    Without the hyphen check this fires on every resume mentioning low-code or
    no-code tooling, and a check that cries wolf on a common word is a check
    reviewers learn to ignore.
    """
    document = "Built internal tooling on a no-code platform for the operations team."

    result = detect_negation(
        [scored("no-code platform for the operations team", document)], document
    )

    assert result.criteria[0].negation_suspected is False
    assert result.flags == []
    assert result.review_required is False


def test_a_weak_marker_is_flagged_too() -> None:
    """`familiar with` describes depth, not absence — a `partial` dressed as `strong`."""
    document = "Familiar with Kubernetes and Terraform from a training course."

    result = detect_negation([scored("Kubernetes and Terraform", document)], document)

    assert result.criteria[0].negation_suspected is True


def test_a_marker_outside_the_window_is_ignored() -> None:
    """A `not` belonging to an earlier sentence must not flag a good quote."""
    document = (
        "The team did not use Terraform. Separately, the candidate ran production "
        "Kubernetes clusters for three years across two regions."
    )

    result = detect_negation([scored("Kubernetes clusters for three years", document)], document)

    assert result.criteria[0].negation_suspected is False


def test_the_window_is_configurable_and_actually_widens() -> None:
    document = "There was no meaningful budget or headcount for the production Kubernetes work."

    narrow = detect_negation([scored("production Kubernetes work", document)], document, window=2)
    wide = detect_negation([scored("production Kubernetes work", document)], document, window=12)

    assert narrow.criteria[0].negation_suspected is False
    assert wide.criteria[0].negation_suspected is True


def test_the_verdict_and_score_inputs_are_never_touched() -> None:
    """1.23: unverifiable output escalates, it never scores a candidate down."""
    document = "Has no production Kubernetes experience."
    criterion = scored("production Kubernetes experience", document)

    result = detect_negation([criterion], document)

    assert result.criteria[0].verdict == criterion.verdict
    assert result.criteria[0].model_verdict == criterion.model_verdict
    assert result.criteria[0].match_ratio == criterion.match_ratio
    assert result.criteria[0].weight == criterion.weight


def test_unverified_criteria_are_left_alone() -> None:
    """They escalate for a stronger reason, and their offsets are not trustworthy."""
    document = "Has no production Kubernetes experience."
    criterion = scored("production Kubernetes experience", document).model_copy(
        update={"verified": False}
    )

    result = detect_negation([criterion], document)

    assert result.criteria[0].negation_suspected is False
    assert result.flags == []


def test_a_criterion_with_no_blocks_is_left_alone() -> None:
    """A `none` verdict with no evidence has no window to read."""
    criterion = ScoredCriterion(
        id="C1",
        verdict="none",
        model_verdict="none",
        evidence="not found",
        verified=False,
        match_ratio=0.0,
        longest_span=0,
        weight=1,
        must_have=False,
    )

    result = detect_negation([criterion], "Any document at all.")

    assert result.criteria[0].negation_suspected is False
