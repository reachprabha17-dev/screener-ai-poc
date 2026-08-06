"""Shared fixtures and builders."""

from screener.models import Candidate, Criterion, Rubric, ScoredCriterion, Verdict

# Criterion text has to be *real*, not a placeholder.
#
# It previously read `f"criterion {i}"`, which broke the moment §10.5(c) started
# checking that evidence is about the criterion it was offered for: a quote about
# payment systems genuinely is not about "criterion C1", so the check fired on
# every fixture. The check was right and the fixture was fiction.
#
# This default shares vocabulary with the resume text used across the suite, so
# a rubric built without explicit text still behaves like a real one. Tests that
# care about the wording pass their own.
DEFAULT_CRITERION_TEXT = "Backend engineering experience"


def make_rubric(*specs: tuple[str, bool, int] | tuple[str, bool, int, str]) -> Rubric:
    """Build a rubric from ``(id, must_have, weight)`` or ``(id, must_have, weight, text)``.

    The contract enforces 4–12 criteria, so callers pass at least four.
    """
    return Rubric(
        id="r1",
        position_id="p1",
        version=1,
        created_by="tester",
        criteria=[
            Criterion(
                id=spec[0],
                text=spec[3] if len(spec) == 4 else DEFAULT_CRITERION_TEXT,
                must_have=spec[1],
                weight=spec[2],
            )
            for spec in specs
        ],
    )


def scored(criterion: Criterion, verdict: Verdict, *, verified: bool = True) -> ScoredCriterion:
    return ScoredCriterion(
        id=criterion.id,
        verdict=verdict,
        model_verdict=verdict,
        evidence="evidence" if verdict != "none" else "not found",
        verified=verified,
        match_ratio=1.0 if verified else 0.0,
        longest_span=5 if verified else 0,
        weight=criterion.weight,
        must_have=criterion.must_have,
    )


def score_all(rubric: Rubric, *verdicts: Verdict) -> list[ScoredCriterion]:
    return [scored(c, v) for c, v in zip(rubric.criteria, verdicts, strict=True)]


def candidate(
    name: str,
    *,
    score: float | None = None,
    must_haves_met: bool = True,
    scoreable: bool = True,
    review_required: bool = False,
) -> Candidate:
    return Candidate(
        run_id="run1",
        filename=name,
        file_sha256=name,
        score=score,
        must_haves_met=must_haves_met,
        scoreable=scoreable,
        review_required=review_required,
    )
