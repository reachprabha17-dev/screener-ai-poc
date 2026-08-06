"""Verdict set integrity (spec §10.3). Pure — no I/O, no model.

The JSON schema handed to Ollama constrains *shape*: a list of objects, each with
an id, a verdict from three values, and an evidence string under 300 characters.
It cannot express "exactly these eight ids, each once". A grammar has no way to
say that, so the model can omit a criterion, return one twice, or invent ``C9``
against a seven-criterion rubric, and every one of those is schema-valid.

**This matters arithmetically, and in one direction.** ``compute_score`` divides
by the rubric's total weight, so a missing criterion contributes nothing to the
numerator and the score simply comes out lower — recoverable. But a *duplicate*
counts a verdict twice, and an *invented* id contributes weight the rubric never
granted. Both inflate. A candidate promoted by a model's bookkeeping error is the
outcome with no downstream check, because nothing about an inflated score looks
wrong.

The response is a single corrective retry, then escalation. The model is not
argued with twice: a second failure to reproduce a list of ids it was handed is
evidence about the model, not about this candidate, and quietly scoring around it
would hide exactly the signal that should reach a human.
"""

from collections import Counter
from dataclasses import dataclass, field

from screener.models import Flag, JudgeOutput, Rubric


@dataclass(frozen=True)
class VerdictSetCheck:
    """Diagnosis of the returned id set against the rubric."""

    ok: bool
    missing: list[str] = field(default_factory=list)
    unexpected: list[str] = field(default_factory=list)
    duplicated: list[str] = field(default_factory=list)

    @property
    def flags(self) -> list[Flag]:
        """Terminal flags — applied only after the corrective retry also fails."""
        return [] if self.ok else [Flag.VERDICT_SET_MISMATCH]

    @property
    def scoreable(self) -> bool:
        return self.ok

    @property
    def review_required(self) -> bool:
        return not self.ok

    def describe(self) -> str:
        """One line for logs and for the reviewer's screen."""
        parts = [
            f"{label}: {', '.join(ids)}"
            for label, ids in (
                ("missing", self.missing),
                ("unexpected", self.unexpected),
                ("duplicated", self.duplicated),
            )
            if ids
        ]
        return "; ".join(parts) if parts else "verdict set matches the rubric"


def validate_verdicts(output: JudgeOutput, rubric: Rubric) -> VerdictSetCheck:
    """Compare returned ids to rubric ids as sets, rejecting duplicates.

    Order is not checked. It carries no meaning — every criterion is scored by
    its own id and weight — and demanding it would fail honest responses.
    """
    returned = [c.id for c in output.criteria]
    counts = Counter(returned)
    rubric_ids = rubric.ids

    duplicated = sorted(i for i, n in counts.items() if n > 1)
    missing = sorted(rubric_ids - counts.keys())
    unexpected = sorted(counts.keys() - rubric_ids)

    return VerdictSetCheck(
        ok=not (duplicated or missing or unexpected),
        missing=missing,
        unexpected=unexpected,
        duplicated=duplicated,
    )


def corrective_instruction(check: VerdictSetCheck, rubric: Rubric) -> str:
    """Text appended to the retry, naming the ids rather than restating the rule.

    The first attempt already carried the rule and the model broke it, so
    repeating it verbatim is the one thing known not to work. This states what
    came back and what was required.
    """
    required = ", ".join(c.id for c in rubric.criteria)
    return (
        f"Your previous response did not match the rubric ({check.describe()}). "
        f"Return exactly one object per criterion, using these ids and no others, "
        f"each exactly once: {required}. "
        f"Do not add, omit, rename, or repeat an id. "
        f"If a criterion is unsupported by the resume, still return it with "
        f'verdict "none" and evidence "not found".'
    )
