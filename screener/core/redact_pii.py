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
reduces what the model weighs; it is not anonymization, and `resume_text`,
`sent_text` and quarantine all still hold the full text (17).
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field

from screener.models import Span

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


def identity_map(text: str) -> list[Span]:
    """The span map for text that was not redacted — translation becomes a no-op.

    Exists so the read path never branches on `redact_pii`. A `None` span map
    that callers must remember to special-case is the same bug as a missing one.
    """
    return [Span(src_start=0, src_end=len(text), dst_start=0, dst_end=len(text))]


def _substitute(
    text: str, pattern: re.Pattern[str], replace: Callable[[re.Match[str]], str | None]
) -> tuple[str, list[Span]]:
    """One pattern pass, returning the new text and its preserved-segment map.

    `replace` returning `None` means "leave this match alone" — the phone rule
    needs it, because a digit run that turns out to be too short to be a number
    is not a redaction and must not appear in the map as one.

    The map records the *gaps between* matches, which is why it is built here
    while walking them rather than recovered afterwards by comparing the two
    strings: a placeholder is not a function of the text it replaced, so the
    correspondence is unrecoverable once the substitution has happened.
    """
    pieces: list[str] = []
    spans: list[Span] = []
    src = dst = 0

    for match in pattern.finditer(text):
        replacement = replace(match)
        if replacement is None:
            continue
        preserved = text[src : match.start()]
        pieces.append(preserved)
        spans.append(
            Span(
                src_start=src,
                src_end=match.start(),
                dst_start=dst,
                dst_end=dst + len(preserved),
            )
        )
        dst += len(preserved)
        pieces.append(replacement)
        dst += len(replacement)
        src = match.end()

    tail = text[src:]
    pieces.append(tail)
    spans.append(Span(src_start=src, src_end=len(text), dst_start=dst, dst_end=dst + len(tail)))
    return "".join(pieces), spans


def _compose(first: list[Span], second: list[Span]) -> list[Span]:
    """Compose A→B with B→C to get A→C.

    Redaction is a sequence of passes and each one renumbers the text underneath
    the next, so the maps have to be composed rather than concatenated. Segments
    surviving both passes are the intersection of the two, measured in the
    shared middle coordinate system.
    """
    out: list[Span] = []
    for b in second:
        for a in first:
            low = max(a.dst_start, b.src_start)
            high = min(a.dst_end, b.src_end)
            if low >= high:
                continue
            out.append(
                Span(
                    src_start=a.src_start + (low - a.dst_start),
                    src_end=a.src_start + (high - a.dst_start),
                    dst_start=b.dst_start + (low - b.src_start),
                    dst_end=b.dst_start + (high - b.src_start),
                )
            )
    return _merge(out)


def _merge(spans: list[Span]) -> list[Span]:
    """Join segments left adjacent in both coordinate systems.

    Composition fragments the map at every pass boundary. Unmerged, a document
    with no redactions at all would accumulate one segment per pattern instead
    of one segment total — correct, but stored per candidate (12.6), so worth
    the six lines.
    """
    merged: list[Span] = []
    for span in sorted(spans, key=lambda s: s.dst_start):
        last = merged[-1] if merged else None
        if last is not None and last.dst_end == span.dst_start and last.src_end == span.src_start:
            merged[-1] = Span(
                src_start=last.src_start,
                src_end=span.src_end,
                dst_start=last.dst_start,
                dst_end=span.dst_end,
            )
        else:
            merged.append(span)
    return merged


def redact_pii(text: str) -> tuple[str, RedactionReport, list[Span]]:
    """Return what the model will see, what was removed, and how to map back.

    The returned string is what 10.5 must later match evidence against.
    Verifying a quote against the pre-redaction original fails on every quote
    sitting near a redaction, so callers must carry this value forward rather
    than re-deriving it.

    The third element is the span map of 12.6: one `Span` per surviving segment,
    carrying its offsets in both `resume_text` and `sent_text`. Every evidence
    highlight is an offset into the redacted string, and HR reads the unredacted
    one — without the map every highlight in the UI lands wrong, and lands wrong
    *quietly*, since the text either side of a redaction still looks plausible.
    """
    counts: dict[str, int] = {}
    redacted = text
    span_map = identity_map(text)

    def _apply(name: str, pattern: re.Pattern[str], replacement: str) -> None:
        nonlocal redacted, span_map
        redacted, spans = _substitute(redacted, pattern, lambda _match: replacement)
        span_map = _compose(span_map, spans)
        hits = len(spans) - 1
        if hits:
            counts[name] = counts.get(name, 0) + hits

    _apply("email", _EMAIL, EMAIL_TOKEN)

    # Phones are counted by the callback rather than by match count: a candidate
    # run that turns out to be too short to be a number is left alone, and
    # leaving text alone is not a redaction.
    phone_hits = 0

    def _phone(match: re.Match[str]) -> str | None:
        nonlocal phone_hits
        digits = sum(character.isdigit() for character in match.group())
        if _PHONE_MIN_DIGITS <= digits <= _PHONE_MAX_DIGITS:
            phone_hits += 1
            return PHONE_TOKEN
        return None

    redacted, spans = _substitute(redacted, _PHONE_CANDIDATE, _phone)
    span_map = _compose(span_map, spans)
    if phone_hits:
        counts["phone"] = phone_hits

    for name, pattern in _LABELLED_PATTERNS:
        _apply(name, pattern, REDACTED_TOKEN)

    _apply("age", _AGE_PHRASE, REDACTED_TOKEN)
    _apply("photo", _PHOTO_ARTEFACT, REDACTED_TOKEN)

    return redacted, RedactionReport(counts=counts), span_map
