"""Free-text output screening (spec §10.7). Pure — no I/O, no model.

Omitting a sentiment *field* from the schema does not stop the model putting
sentiment somewhere else. `JudgeOutput.summary` and `notable_strengths` are
unconstrained strings, and they are where "comes across as a great culture fit",
"appears to have taken a career break", and "energetic young developer" actually
land. The schema's silence on demographics is what makes those the only exposure
left, not what removes it.

`red_flags` is a closed enum for the same reason, which is why it needs no
screening here.

**Neither field affects the score**, so removing an item costs a reviewer some
context and costs the candidate nothing. That asymmetry is what makes stripping
the right response rather than escalation: there is no adverse outcome to guard
against, and leaving the text in place is how a protected characteristic reaches
a human reviewer's screen and anchors their judgement.

The removed text goes to the **audit log, never to `candidates`** (§17). It is
the evidence for correcting the prompt; storing it on the candidate row would
re-import the exact content this function exists to remove.
"""

import re
from dataclasses import dataclass, field

from screener.models import Flag, JudgeOutput

# Each category names something a resume screener must not weigh. The patterns
# are broad on purpose: a false positive drops a line of non-scoring commentary,
# which is cheap, and the alternative is protected-attribute language reaching a
# reviewer's screen.
_CATEGORIES: tuple[tuple[str, str], ...] = (
    (
        "career_gap",
        r"\b(?:employment|career|cv|resume)\s+(?:gap|break)s?\b"
        r"|\bgaps?\s+in\s+(?:their|his|her|the)?\s*(?:employment|career|history)\b"
        r"|\bjob[\s-]?hopp(?:er|ing)\b"
        r"|\bfrequent(?:ly)?\s+(?:job\s+)?chang\w*\b"
        r"|\bshort\s+tenure\b|\bcareer\s+break\b",
    ),
    (
        "demographics",
        # Bare pronouns included deliberately. A summary that reaches for "she"
        # has inferred gender from a name or a photo, which is the inference
        # itself — not a stylistic quirk to tolerate because the sentence around
        # it happens to be about Kubernetes.
        r"\b(?:he|she|him|her|his|hers|male|female|man|woman)\b"
        r"|\b(?:nationality|ethnicity|citizenship|immigrant|foreign(?:er|-born)?)\b"
        r"|\b(?:native|non-native)\s+speaker\b",
    ),
    (
        "age",
        r"\b(?:young|older|elderly|junior-aged|mature|middle-aged|fresh\s+graduate)\b"
        r"|\b\d{1,2}\s*years?\s+old\b|\bage\s+of\s+\d{1,2}\b"
        r"|\b(?:digital\s+native|recent\s+grad(?:uate)?)\b",
    ),
    (
        "family_health",
        # Bare "single" is deliberately absent: "single-page application" and
        # "single point of failure" are ordinary engineering prose, and marital
        # status is already removed at input by the label-anchored redaction.
        r"\b(?:married|divorced|widowed|spouse|children|kids|pregnan(?:t|cy))\b"
        r"|\bsingle\s+parent\b"
        r"|\b(?:parental|maternity|paternity|medical|sick)\s+leave\b"
        r"|\b(?:disabilit|illness|health\s+condition|mental\s+health)\w*\b",
    ),
    (
        "appearance",
        r"\b(?:attractive|well[\s-]groomed|presentable|photogenic|smart\s+appearance)\b"
        r"|\bphoto(?:graph)?\s+(?:shows|suggests)\b",
    ),
    (
        "personality",
        r"\b(?:culture\s+fit|cultural\s+fit|personality|temperament|attitude)\b"
        r"|\b(?:enthusiastic|passionate|energetic|charismatic|likeable|likable|pleasant)\b"
        r"|\b(?:comes\s+across|seems\s+(?:like|to\s+be)|appears\s+to\s+be)\b"
        r"|\b(?:team\s+player|good\s+vibes|positive\s+energy|great\s+communicator)\b",
    ),
)

_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE)) for name, pattern in _CATEGORIES
)


@dataclass(frozen=True)
class RemovedText:
    """One stripped item, bound for the audit log only."""

    field_name: str
    category: str
    value: str


@dataclass(frozen=True)
class FreeTextScreen:
    output: JudgeOutput
    removed: list[RemovedText] = field(default_factory=list)

    @property
    def flags(self) -> list[Flag]:
        return [Flag.FREETEXT_SCREENED] if self.removed else []


def categorize(text: str) -> str | None:
    """First category the text trips, or ``None``."""
    return next((name for name, pattern in _PATTERNS if pattern.search(text)), None)


def screen_freetext(output: JudgeOutput) -> FreeTextScreen:
    """Strip screened content from ``summary`` and ``notable_strengths``.

    ``summary`` is cleared entirely rather than edited. It is one piece of
    connected prose, and excising the offending clause leaves reasoning built on
    a premise the reviewer can no longer see — worse than no summary, because it
    reads as complete.

    ``notable_strengths`` is a list of independent items, so only the offending
    ones are dropped.
    """
    removed: list[RemovedText] = []

    summary = output.summary
    category = categorize(summary) if summary else None
    if category is not None:
        removed.append(RemovedText(field_name="summary", category=category, value=summary))
        summary = ""

    kept_strengths: list[str] = []
    for item in output.notable_strengths:
        hit = categorize(item)
        if hit is None:
            kept_strengths.append(item)
        else:
            removed.append(RemovedText(field_name="notable_strengths", category=hit, value=item))

    cleaned = output.model_copy(update={"summary": summary, "notable_strengths": kept_strengths})
    return FreeTextScreen(output=cleaned, removed=removed)
