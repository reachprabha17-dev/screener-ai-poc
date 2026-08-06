"""Partitioned ranking and banding (spec 10.6). Pure — no I/O.

Two decisions worth stating outright:

**There is no score cap.** Qualification and quality are orthogonal, so they get
two dimensions rather than one squashed number. Candidates missing a must-have
go to their own partition, where their score still orders them sensibly *within*
that group without ever competing against qualified applicants.

**Reviewers see a band, not the float.** Three verdict levels across at most
twelve criteria cannot support a rendered precision of ``7.8``; the decimal
implies resolution that does not exist and invites over-reliance. The float
stays internal and is exported for audit.

``needs_review`` is returned as its own list, never appended to the bottom of a
ranking. At 1,000 applicants a reviewer only ever looks at the top of Band A, so
an unscoreable candidate parked at the bottom of one long list is invisible in
practice — precisely the adverse outcome escalation was designed to prevent.
"""

from config.settings import settings
from screener.models import Band, Candidate, RankedResult


def assign_band(score: float | None) -> Band | None:
    """Map a score to A/B/C/D. ``None`` in, ``None`` out — an unjudged resume has no band."""
    if score is None:
        return None
    high, mid, low = settings.band_thresholds
    if score >= high:
        return "A"
    if score >= mid:
        return "B"
    if score >= low:
        return "C"
    return "D"


def _sort_key(candidate: Candidate) -> tuple[float, str]:
    # Filename breaks ties deterministically, so repeated runs render identically.
    return (-(candidate.score or 0.0), candidate.filename)


def rank(candidates: list[Candidate]) -> RankedResult:
    """Split into three disjoint partitions whose union is the input set."""
    meets = sorted((c for c in candidates if c.scoreable and c.must_haves_met), key=_sort_key)
    missing = sorted((c for c in candidates if c.scoreable and not c.must_haves_met), key=_sort_key)
    # Unscoreable candidates are never ordered: there is no number to order them by,
    # and inventing one would be the failure this partition exists to avoid.
    review = [c for c in candidates if not c.scoreable]

    escalations = sum(1 for c in candidates if c.review_required or not c.scoreable)
    rate = escalations / len(candidates) if candidates else 0.0

    return RankedResult(
        meets_must_haves=meets,
        missing_must_have=missing,
        needs_review=review,
        escalation_rate=round(rate, 4),
    )
