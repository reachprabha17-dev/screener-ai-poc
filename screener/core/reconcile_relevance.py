"""Folding the relevance check back onto a candidate (spec 10.5 C). Pure.

**Asymmetric, and the one invariant worth a property test (19.4): this can only
clear `Flag.EVIDENCE_IRRELEVANT`, never raise a flag `verify_evidence` did not
already raise.** `confirm_relevance` is a second opinion on a narrow question
asked only about criteria already flagged — there is nothing here for it to
escalate that is not escalated already, by construction of what it is even
handed to look at.

**`review_required` is recomputed, not carried over.** `verify_evidence` sets
it whenever `EVIDENCE_CONTRADICTS`, `EVIDENCE_UNVERIFIED`, or
`EVIDENCE_IRRELEVANT` fires — a single boolean, not one per reason. Clearing
the irrelevance flag without recomputing it would leave a candidate reading
"needs review" with zero visible reasons why, the moment irrelevance was the
only one that fired.
"""

import dataclasses

from screener.core.verify_evidence import VerificationResult
from screener.models import Flag, RelevanceCheck

# The only two other flags verify_evidence sets `review_required` for. Kept
# here rather than re-derived, so this stays in step with verify_evidence's
# own branches by inspection, not by re-running its logic.
_OTHER_REVIEW_FLAGS = frozenset({Flag.EVIDENCE_CONTRADICTS, Flag.EVIDENCE_UNVERIFIED})


def reconcile_relevance(
    result: VerificationResult, checks: list[RelevanceCheck]
) -> VerificationResult:
    """Clear `evidence_irrelevant` wherever the model confidently agreed.

    Anything short of `related=True` — disagreement, no answer for that id at
    all — leaves the criterion, the flag, and `review_required` exactly where
    `verify_evidence` left them. Fail-safe: an uncertain answer costs a human
    a look at something already flagged, which is cheaper than the reverse.
    """
    by_id = {c.id: c for c in checks}
    criteria = [
        criterion.model_copy(update={"evidence_irrelevant": False})
        if criterion.evidence_irrelevant
        and (check := by_id.get(criterion.id)) is not None
        and check.related
        else criterion
        for criterion in result.criteria
    ]

    if any(c.evidence_irrelevant for c in criteria):
        return dataclasses.replace(result, criteria=criteria)

    flags = [f for f in result.flags if f != Flag.EVIDENCE_IRRELEVANT]
    return dataclasses.replace(
        result,
        criteria=criteria,
        flags=sorted(flags),
        review_required=bool(_OTHER_REVIEW_FLAGS & set(flags)),
    )
