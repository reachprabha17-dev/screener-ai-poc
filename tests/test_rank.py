"""Partitioned ranking and banding (spec §10.6, §18.3)."""

from conftest import candidate
from hypothesis import given
from hypothesis import strategies as st

from screener.core.rank import assign_band, rank


def test_unqualified_never_outranks_qualified() -> None:
    """The regression that killed the score cap.

    Under `min(score, 4.0)`, the unqualified candidate landed at 4.0 — above a
    qualified candidate at 3.9. Partitioning makes the comparison impossible to
    make by construction.
    """
    strong_but_unqualified = candidate("unqualified.pdf", score=7.8, must_haves_met=False)
    weak_but_qualified = candidate("qualified.pdf", score=3.9, must_haves_met=True)

    result = rank([strong_but_unqualified, weak_but_qualified])

    assert [c.filename for c in result.meets_must_haves] == ["qualified.pdf"]
    assert [c.filename for c in result.missing_must_have] == ["unqualified.pdf"]


def test_partitions_are_disjoint_and_cover_the_input() -> None:
    cs = [
        candidate("a.pdf", score=8.0, must_haves_met=True),
        candidate("b.pdf", score=6.0, must_haves_met=False),
        candidate("c.pdf", scoreable=False),
    ]
    result = rank(cs)

    names = [
        {c.filename for c in result.meets_must_haves},
        {c.filename for c in result.missing_must_have},
        {c.filename for c in result.needs_review},
    ]
    assert names[0] | names[1] | names[2] == {"a.pdf", "b.pdf", "c.pdf"}
    assert not (names[0] & names[1]) and not (names[1] & names[2]) and not (names[0] & names[2])


def test_unscoreable_is_never_ranked_even_when_must_haves_met() -> None:
    """`needs_review` wins over both scored partitions — it is checked first."""
    cs = [candidate("x.pdf", score=9.9, must_haves_met=True, scoreable=False)]
    result = rank(cs)
    assert result.meets_must_haves == []
    assert [c.filename for c in result.needs_review] == ["x.pdf"]


def test_sorted_by_score_then_filename() -> None:
    cs = [
        candidate("b.pdf", score=5.0),
        candidate("a.pdf", score=5.0),
        candidate("c.pdf", score=9.0),
    ]
    result = rank(cs)
    assert [c.filename for c in result.meets_must_haves] == ["c.pdf", "a.pdf", "b.pdf"]


def test_escalation_rate_counts_review_and_unscoreable() -> None:
    cs = [
        candidate("a.pdf", score=8.0),
        candidate("b.pdf", score=7.0, review_required=True),
        candidate("c.pdf", scoreable=False),
        candidate("d.pdf", score=6.0),
    ]
    assert rank(cs).escalation_rate == 0.5


def test_escalation_rate_of_empty_run_is_zero() -> None:
    assert rank([]).escalation_rate == 0.0


def test_band_thresholds() -> None:
    assert assign_band(10.0) == "A"
    assert assign_band(7.5) == "A"  # boundary is inclusive
    assert assign_band(7.4) == "B"
    assert assign_band(5.5) == "B"
    assert assign_band(3.5) == "C"
    assert assign_band(3.4) == "D"
    assert assign_band(0.0) == "D"


def test_no_band_without_a_score() -> None:
    """An unjudged resume has no band — never a default that reads as 'poor'."""
    assert assign_band(None) is None


@given(
    scores=st.lists(st.floats(min_value=0, max_value=10), min_size=0, max_size=20),
    flags=st.lists(st.booleans(), min_size=0, max_size=20),
)
def test_partition_union_is_always_the_input(scores: list[float], flags: list[bool]) -> None:
    n = min(len(scores), len(flags))
    cs = [
        candidate(f"{i}.pdf", score=scores[i], must_haves_met=flags[i], scoreable=flags[i])
        for i in range(n)
    ]
    result = rank(cs)
    total = len(result.meets_must_haves) + len(result.missing_must_have) + len(result.needs_review)
    assert total == n
