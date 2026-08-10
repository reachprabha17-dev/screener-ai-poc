"""Folding the verifier's output back onto a candidate (spec 10.6 C). Pure.

**The invariant, asserted by a property test (19.4): this never changes a verdict
or a score.** The verifier is a second non-deterministic model reading the same
attacker-controlled text as the first. Letting it silently move an outcome would
mean a candidate's result changed because two models disagreed, with no human
involved and nothing in the record a person could defend. So `suggested_verdict`
is *recorded and shown* — a concrete alternative a reviewer can act on — and the
verdict stays where phase 1 left it (1.9, 1.23).

Keeping this pure is what makes that testable without a GPU: the LLM call happens
in `llm/`, and everything the model's answer *does* to a candidate happens here.

`escalation_reasons` is computed here for both phases. It is derived from the
flags rather than accumulated alongside them, so there is exactly one place that
decides why a person is being asked to look, and no way for a flag to be raised
without its reason appearing in the queue grouping (15.4).
"""

from screener.core.verify_evidence import quote_verifies
from screener.models import (
    Candidate,
    EscalationReason,
    Flag,
    ScoredCriterion,
    VerifyOutput,
)

# Flags that put a candidate in front of a human, and why. A flag absent from
# this mapping deliberately does not escalate on its own: SANITIZED_TEXT and
# FREETEXT_SCREENED record that a control fired and worked, POSSIBLE_DUPLICATE is
# documented as weak, and MISSING_MUST_HAVE is an *outcome* — the unqualified
# partition is the answer, and escalating every candidate who fails a hard
# requirement is what drove a measured 100% escalation rate.
FLAG_REASONS: dict[Flag, EscalationReason] = {
    Flag.EVIDENCE_UNVERIFIED: EscalationReason.UNVERIFIED_EVIDENCE,
    # Self-contradiction and irrelevance are both "the quote does not stand up",
    # which is the same queue and the same reviewer question as an unverified
    # quote. Splitting them would fragment the grouping without changing the work.
    Flag.EVIDENCE_CONTRADICTS: EscalationReason.UNVERIFIED_EVIDENCE,
    Flag.EVIDENCE_IRRELEVANT: EscalationReason.UNVERIFIED_EVIDENCE,
    Flag.NEGATION_SUSPECTED: EscalationReason.NEGATION,
    Flag.JUDGE_DISAGREES: EscalationReason.JUDGE_DISAGREEMENT,
    Flag.UNVERIFIED_ABSENCE: EscalationReason.ABSENCE_FOUND,
    Flag.SUSPECTED_INJECTION: EscalationReason.SUSPECTED_INJECTION,
    Flag.INPUT_REJECTED: EscalationReason.UNPROCESSABLE,
    Flag.EXTRACTION_FAILED: EscalationReason.UNPROCESSABLE,
    Flag.BUDGET_EXCEEDED: EscalationReason.UNPROCESSABLE,
    Flag.VERDICT_SET_MISMATCH: EscalationReason.UNPROCESSABLE,
    Flag.LLM_ERROR: EscalationReason.UNPROCESSABLE,
    Flag.SCHEMA_INVALID: EscalationReason.UNPROCESSABLE,
    Flag.PARSER_TIMEOUT: EscalationReason.UNPROCESSABLE,
    Flag.PARSER_CRASHED: EscalationReason.UNPROCESSABLE,
}


def escalation_reasons_for(
    flags: list[Flag], criteria: list[ScoredCriterion]
) -> list[EscalationReason]:
    """Why this candidate is in the review queue, in a stable order.

    A queue of 23 is not something anyone can act on; "8 unverified evidence, 7
    judge disagreement, 4 absence found" is, because similar cases can then be
    worked as a batch (15.4).

    `PARTIAL_MUST_HAVE` comes from the criteria rather than a flag: a
    half-satisfied hard requirement is visible in the verdicts themselves, and
    inventing a flag to carry a fact already recorded is a second thing to keep
    in step.
    """
    reasons = {FLAG_REASONS[f] for f in flags if f in FLAG_REASONS}
    if any(c.must_have and c.verdict == "partial" for c in criteria):
        reasons.add(EscalationReason.PARTIAL_MUST_HAVE)
    return sorted(reasons)


def reconcile_judge(candidate: Candidate, output: VerifyOutput) -> Candidate:
    """Apply the verifier's checks to a candidate. Never touches verdict or score.

    Absence checks are the asymmetric half. The verifier claiming a criterion is
    *not* absent is a claim about text, so it is required to quote that text and
    the quote is re-run through stage B. Verified, it escalates; unverified, it
    is discarded — the second model fabricated too, and a fabrication does not
    become evidence by arriving from a different model.
    """
    criteria = {c.id: c for c in candidate.criteria}
    flags = set(candidate.flags)

    for check in output.support_checks:
        criterion = criteria.get(check.id)
        if criterion is None:
            # A criterion the verifier invented, or one edited out from under it.
            # Silently ignored: acting on it would attach a judgment to a
            # criterion nobody can display.
            continue
        criteria[check.id] = criterion.model_copy(
            update={
                "support": check.support,
                "suggested_verdict": check.suggested_verdict,
                "verifier_rationale": check.rationale,
            }
        )
        if check.support != "supported":
            flags.add(Flag.JUDGE_DISAGREES)

    for absence in output.absence_checks:
        criterion = criteria.get(absence.id)
        if criterion is None:
            continue
        update: dict[str, object] = {"absence_confirmed": absence.confirmed_absent}
        if (
            not absence.confirmed_absent
            and absence.found_evidence
            and quote_verifies(absence.found_evidence, candidate.sent_text)
        ):
            update["absence_evidence"] = absence.found_evidence
            flags.add(Flag.UNVERIFIED_ABSENCE)
        criteria[absence.id] = criterion.model_copy(update=update)

    ordered = [criteria[c.id] for c in candidate.criteria]
    reasons = escalation_reasons_for(sorted(flags), ordered)

    return candidate.model_copy(
        update={
            "criteria": ordered,
            "flags": sorted(flags),
            "escalation_reasons": reasons,
            # OR-ed with what phase 1 decided, never replacing it. A candidate
            # already flagged for review must not become unflagged because the
            # verifier happened to agree with everything it was shown.
            "review_required": candidate.review_required or bool(reasons),
            "verification_status": "done",
        }
    )
