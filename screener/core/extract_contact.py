"""Contact details for the reviewer-facing export (15.6). Pure — no I/O, no model.

The review screen's CSV identified a *file*: filename, hash, band, score. A
recruiter acting on it then had to open each resume again to find out who to
call. This module produces the three fields that close that gap — name, email,
phone — from material the read path already has.

**This module owns the email and phone patterns; `redact_pii` imports them.**
The import runs in that direction because the two jobs are the same recognition
problem read twice: one module removes what these patterns find before the model
sees it, the other reports it to the human afterwards. Two copies of the phone
rule would drift, and the direction they would drift in is a number that gets
redacted from the model's view but is still not the one exported to HR.

**Nothing here is persisted.** `candidate_response` derives these at read time
from `resume_text`, which is what makes erasure keep working: `purge_candidate`
(12.10) blanks `resume_text`, and the contact details go with it. Three stored
columns would be three more entries that erasure has to remember to list, which
is the exact failure that function's docstring warns about.

**Names come from the filename, not the text.** Identifying a name in free text
needs NER, which is a model, which this module is not — the same reasoning that
leaves names unredacted in `redact_pii`. The filename is the weaker source but
the honest one: it is what the reviewer already sees in the table, so a wrong
name in the export is a wrong name on screen too, rather than a plausible
fabrication that only the CSV contains.
"""

import re
from dataclasses import dataclass
from pathlib import PurePath

EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")

# Two stages, because one regex cannot express "phone-shaped **and** long
# enough". The pattern finds digit runs with phone separators; `is_phone_number`
# then counts digits and rejects anything outside 9–15.
#
# The digit count is what protects the evidence in `redact_pii`. A single looser
# pattern eats "2015 - 2019" (8 digits) and takes the duration evidence a
# `strong` verdict rests on with it.
PHONE_CANDIDATE_PATTERN = re.compile(r"(?<!\w)\+?\(?\d[\d\s().-]{7,17}\d(?!\w)")
PHONE_MIN_DIGITS = 9
PHONE_MAX_DIGITS = 15

# `84637_` — the requisition the applicant tracker prefixes every export with.
# It is a position reference, not part of anyone's name, and the export carries
# the position's real title in its own column.
_REQUISITION_PREFIX = re.compile(r"^\d+[_\-\s]+")

# The document word the same tracker appends. Anchored to a separator so a
# surname of "Cv" survives and a file called plainly `resume.pdf` is left alone
# rather than reduced to nothing.
_DOCUMENT_SUFFIX = re.compile(r"[_\-\s]+(?:resume|resum[eé]|cv)$", re.IGNORECASE)


@dataclass(frozen=True)
class Contact:
    """Empty strings, never `None`. Every field here lands in a CSV cell, and a
    blank cell is what "not found" looks like there."""

    name: str = ""
    email: str = ""
    phone: str = ""


def is_phone_number(text: str) -> bool:
    """Does a phone-shaped digit run have a phone-shaped number of digits?"""
    digits = sum(character.isdigit() for character in text)
    return PHONE_MIN_DIGITS <= digits <= PHONE_MAX_DIGITS


def _first_email(text: str) -> str:
    match = EMAIL_PATTERN.search(text)
    return match.group() if match else ""


def _first_phone(text: str) -> str:
    """First match wins, which is a deliberate bias towards the top of the page.

    A resume can carry other people's numbers — a referee's, a former employer's
    switchboard. The applicant's own is the one in the header, and reading down
    from the start is the cheapest way to prefer it. It is a heuristic and it can
    be wrong; the column is the reviewer's starting point, not an assertion.
    """
    for match in PHONE_CANDIDATE_PATTERN.finditer(text):
        if is_phone_number(match.group()):
            return " ".join(match.group().split())
    return ""


def _recase(token: str) -> str:
    """Title-case a token only when it is uniformly cased.

    The corpus is inconsistent — `GEOFREY_SEMAKULA`, `adil_Mohamed`,
    `Rajivi_shivamurthy_Lekhak` — so some normalisation earns its place. A
    blanket `.title()` does not: it turns `McDonald` into `Mcdonald`. Mixed case
    is evidence somebody typed the name deliberately, so it is left alone.
    """
    return token.title() if token.isupper() or token.islower() else token


def name_from_filename(filename: str) -> str:
    """`84637_GEOFREY_SEMAKULA_resume.pdf` → `Geofrey Semakula`.

    The separator is whichever one the filename uses. Underscores are the
    convention in the tracker's exports, and treating a hyphen as a separator
    there would split `Anne-Marie` into two names; where there is no underscore
    at all the hyphen or dot is plainly doing the separating instead.
    """
    stem = PurePath(filename).stem
    trimmed = _DOCUMENT_SUFFIX.sub("", _REQUISITION_PREFIX.sub("", stem))

    # Everything stripped — `84637_resume.pdf` and the like. Fall back to the
    # stem rather than returning nothing: a filename that carries no name is
    # still the only handle the reviewer has on this row.
    source = trimmed or stem
    separators = r"[_\s]+" if "_" in source else r"[\-.\s]+"
    tokens = [_recase(token) for token in re.split(separators, source) if token]
    return " ".join(tokens)


def extract_contact(resume_text: str, filename: str) -> Contact:
    """Who this is and how to reach them, for the export only.

    Nothing in scoring reads this. It is derived after every decision about the
    candidate has been made, which is the property that keeps it from becoming
    an input to one.
    """
    return Contact(
        name=name_from_filename(filename),
        email=_first_email(resume_text),
        phone=_first_phone(resume_text),
    )
