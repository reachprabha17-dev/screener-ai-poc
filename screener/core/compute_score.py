"""Weighted score from per-criterion verdicts (spec 10.4). Pure — no I/O, no model.

The model is never asked for the number. It makes one narrow judgment per
criterion and this module does the arithmetic, which is what makes a score
reproducible, re-weightable without spending GPU time, and explainable line by
line when someone asks why a candidate ranked where they did.

**This function does not cap, penalise, or rank.** It returns a number and a
boolean. An earlier draft capped unqualified candidates at 4.0, which parked
them *above* every qualified candidate scoring 3.9 or less and collapsed two
orthogonal dimensions into one number when ``must_haves_met`` already existed.
Separating the unqualified is ``rank``'s job (10.6).
"""

from dataclasses import dataclass, field

from screener.models import VERDICT_VALUE, Flag, Rubric, ScoredCriterion


@dataclass
class ScoreResult:
    score: float
    must_haves_met: bool
    review_required: bool = False
    flags: list[Flag] = field(default_factory=list)


def compute_score(criteria: list[ScoredCriterion], rubric: Rubric) -> ScoreResult:
    """Score out of 10, plus whether every must-have is satisfied.

    The denominator is ``rubric.total_weight`` — always the rubric, never the
    returned set. Dividing by the weights that came back would let a silently
    missing criterion shrink the denominator and *inflate* the score, which is
    the failure ``validate_verdicts`` (10.3) exists to catch and this function
    refuses to depend on.
    """
    if not rubric.criteria:
        raise ValueError("cannot score against an empty rubric")

    total_weight = rubric.total_weight
    earned = sum(VERDICT_VALUE[c.verdict] * c.weight for c in criteria)
    score = round((earned / total_weight) * 10, 1)

    must_haves = [c for c in criteria if c.must_have]
    unmet = [c for c in must_haves if c.verdict == "none"]
    if unmet:
        # **No `review_required`.** 10.4 sends an unmet must-have to the
        # unqualified partition, and the partition *is* the outcome — a clear,
        # explainable result that needs no adjudication.
        #
        # Setting it here was measured as the dominant driver of a 100%
        # escalation rate: on any real corpus most applicants fail at least one
        # hard requirement, so flagging them all puts the entire run in the
        # review queue. That is the 18.2 failure exactly — human oversight
        # collapsing into rubber-stamping because the queue exceeds what anyone
        # will read. Escalation is for cases the *system* could not resolve, not
        # for candidates it resolved against.
        return ScoreResult(
            score=score,
            must_haves_met=False,
            review_required=False,
            flags=[Flag.MISSING_MUST_HAVE],
        )

    # A half-satisfied hard requirement is a human call, not an arithmetic one:
    # the candidate stays in the qualified partition but a reviewer must look.
    half_met = [c for c in must_haves if c.verdict == "partial"]
    return ScoreResult(score=score, must_haves_met=True, review_required=bool(half_met))
