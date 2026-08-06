"""Input redaction (spec 9, 13). Pure — no I/O, no model.

Removes direct identifiers and the protected-attribute fields that international
CV conventions still put at the top of the page — date of birth, age,
nationality, marital status, gender — before the resume reaches the model. The
model cannot weigh what it never sees, which is a stronger guarantee than
instructing it not to.

**The constraint that shapes everything here: employment date ranges must
survive.** The `strong` verdict anchor in 9.2 requires evidence of depth,
scope, or *duration*. A redactor that eats four-digit years to catch a birth year
would quietly convert every duration-based `strong` into a `partial` — a
system-wide downgrade applied to every candidate, produced by a privacy control,
invisible in the output. So date handling is **label-anchored**: `DOB: 1985` goes,
`2019–2024 Senior Engineer` stays. A bare year is never touched.

**What is not redacted, stated plainly.** Names are not. Identifying a person's
name in free text needs NER, which is a model, which this module is not — and
`Candidate.filename` carries the name regardless (5). URLs are not: a GitHub
profile is often the evidence a technical criterion rests on. Redaction here
reduces what the model weighs; it is not anonymization, and the audit trail,
traces, and quarantine all still hold the full text (17).
"""

import re
from dataclasses import dataclass, field

# Placeholders, not secrets — the substitution markers left in the redacted text.
EMAIL_TOKEN = "[EMAIL]"  # noqa: S105
PHONE_TOKEN = "[PHONE]"  # noqa: S105
REDACTED_TOKEN = "[REDACTED]"  # noqa: S105

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Two stages, because one regex cannot express "phone-shaped **and** long
# enough". The pattern finds digit runs with phone separators; `_redact_phone`
# then counts digits and leaves anything outside 9–15 alone.
#
# The digit count is what protects the evidence. A single looser pattern eats
# "2015 - 2019" (8 digits) and takes the duration evidence a `strong` verdict
# rests on with it — the same class of failure as a year-shaped DOB regex.
_PHONE_CANDIDATE = re.compile(r"(?<!\w)\+?\(?\d[\d\s().-]{7,17}\d(?!\w)")
_PHONE_MIN_DIGITS = 9
_PHONE_MAX_DIGITS = 15

# Label-anchored fields. Each consumes to end of line: these appear as
# `Label: value` header rows, and the value is the part that must not survive.
_LABELLED = (
    ("date_of_birth", r"(?:date\s+of\s+birth|d\.?o\.?b\.?|birth\s*date|born)"),
    ("age", r"age"),
    ("nationality", r"(?:nationality|citizenship)"),
    ("marital_status", r"(?:marital\s+status|civil\s+status)"),
    ("gender", r"(?:gender|sex)"),
    ("religion", r"religion"),
)

_LABELLED_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(rf"^[ \t]*{label}[ \t]*[:\-–][^\n]*", re.IGNORECASE | re.MULTILINE))
    for name, label in _LABELLED
)

# The one unlabelled age form common enough to be worth matching, and narrow
# enough not to collide with "7 years experience".
_AGE_PHRASE = re.compile(r"\b\d{1,2}\s*years?\s+old\b", re.IGNORECASE)

# Parser artefacts for an embedded photograph. The image itself never reaches
# this module; its filename sometimes does.
_PHOTO_ARTEFACT = re.compile(
    r"\[(?:image|photo|picture)[^\]\n]{0,80}\]",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RedactionReport:
    """Counts per category. Logged, and surfaced when a redaction looks wrong."""

    counts: dict[str, int] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return sum(self.counts.values())

    @property
    def any_redacted(self) -> bool:
        return self.total > 0


def redact_pii(text: str) -> tuple[str, RedactionReport]:
    """Return the string that will be sent to the model, plus what was removed.

    The returned string is what 10.5 must later match evidence against.
    Verifying a quote against the pre-redaction original fails on every quote
    sitting near a redaction, so callers must carry this value forward rather
    than re-deriving it.
    """
    counts: dict[str, int] = {}
    redacted = text

    def _apply(name: str, pattern: re.Pattern[str], replacement: str) -> None:
        nonlocal redacted
        redacted, hits = pattern.subn(replacement, redacted)
        if hits:
            counts[name] = counts.get(name, 0) + hits

    _apply("email", _EMAIL, EMAIL_TOKEN)

    # Phones are counted by the callback rather than by match count: a candidate
    # run that turns out to be too short to be a number is left alone, and
    # leaving text alone is not a redaction.
    phone_hits = 0

    def _phone(match: re.Match[str]) -> str:
        nonlocal phone_hits
        digits = sum(character.isdigit() for character in match.group())
        if _PHONE_MIN_DIGITS <= digits <= _PHONE_MAX_DIGITS:
            phone_hits += 1
            return PHONE_TOKEN
        return match.group()

    redacted = _PHONE_CANDIDATE.sub(_phone, redacted)
    if phone_hits:
        counts["phone"] = phone_hits

    for name, pattern in _LABELLED_PATTERNS:
        _apply(name, pattern, REDACTED_TOKEN)

    _apply("age", _AGE_PHRASE, REDACTED_TOKEN)
    _apply("photo", _PHOTO_ARTEFACT, REDACTED_TOKEN)

    return redacted, RedactionReport(counts=counts)
