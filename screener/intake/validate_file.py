"""Pre-parse file validation (spec 8.2). I/O: reads the header and, for zips, the
central directory. Nothing else outside the sandbox touches raw file bytes.

Resumes are untrusted **files** before they are untrusted text. PDF and DOCX are
among the most heavily exploited container formats in existence, and a malicious
one compromises the host at *parse* time — before any prompt-level control runs,
with whatever privileges the worker holds. Every control in 10 is downstream of
this one.

**Rejection is not a skip.** A rejected file moves to quarantine with an audit
entry and still becomes a ``Candidate`` with ``scoreable=False`` and
``review_required=True``. Nobody drops out of a run without a human seeing it —
silently discarding a file that failed a check is how a real applicant vanishes
from a requisition and nobody finds out.

**What this cannot do.** These are structural checks against known bad shapes,
not a verdict on whether a file is safe. A novel parser exploit in a
well-formed PDF passes every check here. The sandbox (8.4) is what makes that
survivable; this layer exists to make it rare and to keep the cheap attacks
cheap to stop.
"""

import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import magic

from config.settings import settings
from screener.models import Flag

FileType = Literal["pdf", "docx"]

PDF_SIGNATURE = b"%PDF-"
ZIP_SIGNATURE = b"PK\x03\x04"

# libmagic reports the OOXML type when it can see `[Content_Types].xml` near the
# start, and plain `application/zip` when the central directory is out of reach
# of the header read. Both are accepted here: the container type is all this
# check owns, and `_check_docx_structure` is what establishes it is a Word file.
PDF_MIMES = frozenset({"application/pdf"})
DOCX_MIMES = frozenset(
    {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
    }
)

_HEADER_BYTES = 4096
MAX_ZIP_ENTRIES = 500

# A DOCX without this is not a Word document, whatever its extension claims.
_REQUIRED_DOCX_ENTRY = "word/document.xml"

# Cheap page-count signals, read without parsing. See `_pdf_page_hint`.
_PDF_PAGE_OBJ = re.compile(rb"/Type\s*/Page(?![sA-Za-z])")
_PDF_PAGE_COUNT = re.compile(rb"/Count\s+(\d+)")


@dataclass(frozen=True)
class FileCheck:
    """Outcome of validation. Returned, never raised.

    A rejection is an expected, recordable state of a run — not an exception —
    because every rejection has to end up in front of a reviewer.
    """

    ok: bool
    file_type: FileType | None = None
    reason: str | None = None
    detail: dict[str, object] = field(default_factory=dict)
    security_event: bool = False

    @property
    def flags(self) -> list[Flag]:
        return [] if self.ok else [Flag.INPUT_REJECTED]

    @property
    def scoreable(self) -> bool:
        return self.ok

    @property
    def review_required(self) -> bool:
        return not self.ok


def _reject(reason: str, *, security_event: bool = False, **detail: object) -> FileCheck:
    return FileCheck(ok=False, reason=reason, detail=detail, security_event=security_event)


def _check_location(path: Path, root: Path) -> FileCheck | None:
    """Containment and symlinks.

    Symlinks are rejected outright rather than resolved. A link pointing inside
    the run folder is indistinguishable at read time from one that was pointing
    elsewhere a moment ago, and the folder is writable by whoever drops CVs into
    it.
    """
    if path.is_symlink():
        return _reject("symlink", security_event=True, path=str(path))

    try:
        resolved = path.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        return _reject("unresolvable_path", path=str(path), error=str(exc))

    if not resolved.is_relative_to(resolved_root):
        # A parent component was a symlink out of the folder, or the caller
        # passed a traversal path.
        return _reject("outside_run_folder", security_event=True, resolved=str(resolved))

    if not resolved.is_file():
        return _reject("not_a_regular_file", path=str(path))

    return None


def _check_size(path: Path, limit: int) -> FileCheck | None:
    size = path.stat().st_size
    if size == 0:
        return _reject("empty_file", size=0)
    if size > limit:
        return _reject("oversize", size=size, limit=limit)
    return None


def _sniff(header: bytes) -> FileType | None:
    """Container type from magic bytes. The extension is not consulted.

    Signature first, libmagic second. The signature is a two-byte fact that does
    not move between libmagic versions, and the whole point of this check is
    that it produces the same answer on every host.
    """
    if header.startswith(PDF_SIGNATURE):
        detected = magic.from_buffer(header, mime=True)
        return "pdf" if detected in PDF_MIMES else None
    if header.startswith(ZIP_SIGNATURE):
        detected = magic.from_buffer(header, mime=True)
        return "docx" if detected in DOCX_MIMES else None
    return None


def _check_docx_structure(path: Path) -> FileCheck | None:
    """Enumerate the central directory **without extracting anything**.

    Reading the directory is metadata only; every check below runs against
    declared sizes and names. Extracting first to find out whether extraction is
    safe is the bug this ordering avoids.
    """
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
    except (zipfile.BadZipFile, OSError) as exc:
        return _reject("corrupt_zip", error=str(exc))

    if len(entries) > MAX_ZIP_ENTRIES:
        return _reject("too_many_entries", entries=len(entries), limit=MAX_ZIP_ENTRIES)

    names = {e.filename for e in entries}
    if _REQUIRED_DOCX_ENTRY not in names:
        return _reject("not_a_word_document", missing=_REQUIRED_DOCX_ENTRY)

    total_uncompressed = 0
    for entry in entries:
        name = entry.filename
        # Zip slip. `..` is checked per path component so that a legitimate
        # `word/media/..jpg` style name is not caught by a substring test.
        parts = name.replace("\\", "/").split("/")
        if name.startswith("/") or ".." in parts:
            return _reject("path_traversal", security_event=True, entry=name)
        if len(name) > 1 and name[1] == ":":
            return _reject("absolute_path", security_event=True, entry=name)

        total_uncompressed += entry.file_size
        if total_uncompressed > settings.max_decompressed_bytes:
            return _reject(
                "decompression_limit",
                total=total_uncompressed,
                limit=settings.max_decompressed_bytes,
            )

        if entry.compress_size > 0:
            ratio = entry.file_size / entry.compress_size
            if ratio > settings.max_compression_ratio:
                return _reject(
                    "compression_ratio",
                    security_event=True,
                    entry=name,
                    ratio=round(ratio, 1),
                    limit=settings.max_compression_ratio,
                )
        elif entry.file_size > 0:
            # Claims to expand from nothing. Not a shape any writer produces.
            return _reject("impossible_ratio", security_event=True, entry=name)

    return None


def _pdf_page_hint(path: Path) -> int | None:
    """Best-effort page count from the raw bytes, or ``None`` when unknowable.

    Two signals, both readable without a parse: ``/Type /Page`` object headers
    and the ``/Count`` on the page tree. The larger wins, since a page bomb has
    to inflate at least one of them.

    **``None`` is the common case on modern PDFs.** Since 1.5 the page tree can
    live inside compressed object streams, where neither signal is visible
    without decompressing — which is a parse, which is the thing this check
    exists to happen before. So an unknown count does *not* reject: doing so
    would refuse most legitimately produced CVs. The real enforcement is the
    sandbox's rlimits and the post-parse ``page_count`` on ``ParsedResume``.
    This screen catches the naive bomb for free and is honest about the rest.
    """
    data = path.read_bytes()
    signals = [len(_PDF_PAGE_OBJ.findall(data))]
    signals.extend(int(m) for m in _PDF_PAGE_COUNT.findall(data))
    hint = max(signals)
    return hint or None


def _check_pdf_pages(path: Path, limit: int) -> FileCheck | None:
    pages = _pdf_page_hint(path)
    if pages is not None and pages > limit:
        return _reject("too_many_pages", pages=pages, limit=limit)
    return None


def validate_file(
    path: Path,
    *,
    root: Path,
    max_bytes: int | None = None,
    max_pages: int | None = None,
) -> FileCheck:
    """Run every 8.2 check in order, stopping at the first failure.

    Order matters: containment before any read, size before any parse of
    structure, type before the type-specific checks. Each stage narrows what the
    next one has to be robust against.

    ``max_bytes`` and ``max_pages`` default to ``settings.max_file_bytes`` and
    ``settings.max_pages``. They exist so a caller may make these checks
    *tighter* than the resume path — an uploaded job description allows 2 MB and
    10 pages, because it is a two-page document and the cap is what bounds a held
    request thread — and for no other purpose.

    There is deliberately **no parameter for the accepted types**. Both callers
    want the same two container formats, and a per-call type list is the one
    parameter here that could weaken this function rather than narrow it.

    Both are ``None`` sentinels rather than default expressions reading
    ``settings``: a default is evaluated once, at import, and this codebase
    mutates the settings object at runtime — `.env` overrides land on the same
    singleton, and `tests/test_parse_worker.py` monkeypatches `max_pages`
    directly. A def-time default would silently ignore all of it.
    """
    limit_bytes = settings.max_file_bytes if max_bytes is None else max_bytes
    limit_pages = settings.max_pages if max_pages is None else max_pages
    # Sequential, not a tuple of results: containment has to short-circuit
    # before anything else so much as stats the path.
    location = _check_location(path, root)
    if location is not None:
        return location

    size = _check_size(path, limit_bytes)
    if size is not None:
        return size

    with path.open("rb") as handle:
        header = handle.read(_HEADER_BYTES)

    file_type = _sniff(header)
    if file_type is None:
        return _reject(
            "unrecognized_type",
            security_event=True,
            detected=magic.from_buffer(header, mime=True),
        )

    # Extension/MIME disagreement is reported as a security event before it is
    # rejected: a `.pdf` that is really a zip is not a user mistake pattern, it
    # is someone probing which of the two the pipeline trusts.
    extension = path.suffix.casefold()
    if extension not in settings.allowed_extensions:
        return _reject("disallowed_extension", extension=extension)
    if extension != f".{file_type}":
        return _reject(
            "extension_mime_mismatch",
            security_event=True,
            extension=extension,
            actual=file_type,
        )

    type_check = (
        _check_docx_structure(path) if file_type == "docx" else _check_pdf_pages(path, limit_pages)
    )
    if type_check is not None:
        return type_check

    return FileCheck(ok=True, file_type=file_type)
