"""Validation and path resolution for résumé folder references (spec intake share).

A reference is the folder a requisition screens, expressed **relative to
`settings.resumes_dir`** — `engineering-2026`, or `2026/Q3/engineering` when the
share is organised in subfolders. Never absolute, never containing `..`.

The rule is per *segment*, not per string. Allowing `/` while validating each
side of it is what lets the picker browse to any depth without reopening the
traversal it exists to prevent: `..` fails as a segment, an absolute path fails
on its empty leading segment, and `folder_for`'s resolve-and-compare is the
backstop that does not depend on the pattern being exhaustive.

Pure module in core/ — no I/O, no state, no database dependencies.
"""

import re
from pathlib import Path

from config.settings import settings

SEGMENT_PATTERN = r"[A-Za-z0-9](?:[A-Za-z0-9 ._-]*[A-Za-z0-9])?"
MAX_DEPTH = 8


def is_safe_segment(segment: str) -> bool:
    """True for one folder name with no separators, no `..`, no leading dot.

    `fullmatch`, not `match`: Python's `$` also matches immediately before a
    trailing newline, so `re.match(r"...$", "eng-2026\\n")` succeeds. `reference`
    is UNIQUE and is the folder name, so that would admit two positions that
    render identically in the picker and create two different directories.
    """
    if not segment or segment in {".", ".."}:
        return False
    if segment.startswith("."):
        # Dot-prefixed directories are hidden infrastructure (`.git`, `.Trashes`
        # on a share), never a requisition's CV folder.
        return False
    return bool(re.fullmatch(SEGMENT_PATTERN, segment))


def is_safe_reference(reference: str) -> bool:
    """True if every `/`-separated segment is a safe folder name.

    Backslash is rejected outright rather than treated as a separator: on Linux
    it is a legal filename character, so accepting `a\\b` would create one
    directory literally named `a\\b` while a Windows-minded reader — and the
    share's own Windows clients — would read it as two.
    """
    if not reference or "\\" in reference:
        return False
    segments = reference.split("/")
    if len(segments) > MAX_DEPTH:
        return False
    return all(is_safe_segment(s) for s in segments)


def folder_for(reference: str) -> Path:
    """Joined Path under `settings.resumes_dir`, asserting containment.

    Raises ValueError if reference is malformed or escapes the root directory.
    """
    if not is_safe_reference(reference):
        raise ValueError(
            f"Invalid reference '{reference}': each folder name may contain only "
            "letters, numbers, spaces, dots, hyphens and underscores, separated by '/'"
        )

    base = Path(settings.resumes_dir).resolve()
    target = (base / reference).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"Reference '{reference}' escapes root '{base}'")

    return target
