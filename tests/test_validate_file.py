"""Pre-parse file validation (spec 8.2, build gate 20 step 5).

**This is a security gate.** The spec is explicit: do not point this pipeline at
real candidate files until these fixtures pass. Every test here is an attack that
reaches the parser if the check it exercises is removed.
"""

import zipfile
from pathlib import Path

import pytest
from fixtures_files import (
    write_docx,
    write_many_entries,
    write_pdf,
    write_zip_bomb,
    write_zip_slip,
)

from config.settings import settings
from screener.intake.validate_file import MAX_ZIP_ENTRIES, validate_file
from screener.models import Flag


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "resumes" / "REQ-1"
    folder.mkdir(parents=True)
    return folder


# --- the happy path ----------------------------------------------------------


def test_valid_pdf_passes(root: Path) -> None:
    check = validate_file(write_pdf(root / "asha.pdf"), root=root)

    assert check.ok is True
    assert check.file_type == "pdf"
    assert check.flags == []
    assert check.review_required is False


def test_valid_docx_passes(root: Path) -> None:
    check = validate_file(write_docx(root / "asha.docx"), root=root)

    assert check.ok is True
    assert check.file_type == "docx"


# --- rejection is never a silent skip ---------------------------------------


def test_rejection_flags_for_human_review(root: Path) -> None:
    """The whole point: a rejected file still reaches a person.

    Silently dropping it is how a real applicant disappears from a requisition
    and nobody finds out.
    """
    path = root / "notes.pdf"
    path.write_bytes(b"this is not a pdf at all, just text")

    check = validate_file(path, root=root)

    assert check.ok is False
    assert check.flags == [Flag.INPUT_REJECTED]
    assert check.scoreable is False
    assert check.review_required is True
    assert check.reason is not None


# --- size --------------------------------------------------------------------


def test_oversize_is_rejected(root: Path) -> None:
    path = root / "huge.pdf"
    path.write_bytes(b"%PDF-1.4\n" + b"0" * (settings.max_file_bytes + 1))

    check = validate_file(path, root=root)

    assert check.reason == "oversize"


def test_empty_file_is_rejected(root: Path) -> None:
    path = root / "empty.pdf"
    path.touch()

    assert validate_file(path, root=root).reason == "empty_file"


# --- type sniffing -----------------------------------------------------------


def test_type_comes_from_magic_bytes_not_the_extension(root: Path) -> None:
    """A zip renamed to `.pdf` is not a user mistake pattern.

    It is someone establishing which of the two the pipeline trusts.
    """
    path = write_docx(root / "resume.docx")
    renamed = path.rename(root / "resume.pdf")

    check = validate_file(renamed, root=root)

    assert check.ok is False
    assert check.reason == "extension_mime_mismatch"
    assert check.detail["actual"] == "docx"
    assert check.security_event is True


def test_unrecognized_type_is_a_security_event(root: Path) -> None:
    path = root / "payload.pdf"
    path.write_bytes(b"\x7fELF\x02\x01\x01\x00" + b"\x00" * 200)

    check = validate_file(path, root=root)

    assert check.reason == "unrecognized_type"
    assert check.security_event is True


def test_disallowed_extension_is_rejected(root: Path) -> None:
    path = write_pdf(root / "resume.pdf").rename(root / "resume.txt")

    assert validate_file(path, root=root).reason == "disallowed_extension"


# --- path containment --------------------------------------------------------


def test_symlink_is_rejected_not_followed(root: Path, tmp_path: Path) -> None:
    """Rejected outright rather than resolved.

    The run folder is writable by whoever drops CVs into it, so a link pointing
    somewhere harmless at check time is indistinguishable from one that will
    point elsewhere at read time.
    """
    target = write_pdf(tmp_path / "outside.pdf")
    link = root / "resume.pdf"
    link.symlink_to(target)

    check = validate_file(link, root=root)

    assert check.reason == "symlink"
    assert check.security_event is True


def test_path_outside_the_run_folder_is_rejected(root: Path, tmp_path: Path) -> None:
    outside = write_pdf(tmp_path / "elsewhere.pdf")

    check = validate_file(outside, root=root)

    assert check.reason == "outside_run_folder"
    assert check.security_event is True


def test_traversal_path_is_rejected(root: Path, tmp_path: Path) -> None:
    write_pdf(tmp_path / "elsewhere.pdf")

    check = validate_file(root / ".." / ".." / "elsewhere.pdf", root=root)

    assert check.ok is False


def test_missing_file_is_rejected_not_crashed(root: Path) -> None:
    assert validate_file(root / "gone.pdf", root=root).reason == "unresolvable_path"


def test_directory_is_not_a_file(root: Path) -> None:
    (root / "subdir.pdf").mkdir()

    assert validate_file(root / "subdir.pdf", root=root).reason == "not_a_regular_file"


# --- zip structure -----------------------------------------------------------


def test_zip_slip_is_rejected(root: Path) -> None:
    """An entry escaping the extraction directory. Never extracted to find out."""
    check = validate_file(write_zip_slip(root / "slip.docx"), root=root)

    assert check.reason == "path_traversal"
    assert check.security_event is True


def test_absolute_entry_path_is_rejected(root: Path) -> None:
    check = validate_file(write_zip_slip(root / "abs.docx", entry="/etc/passwd"), root=root)

    assert check.reason == "path_traversal"


def test_windows_absolute_entry_path_is_rejected(root: Path) -> None:
    check = validate_file(
        write_zip_slip(root / "win.docx", entry="C:/Windows/System32/x.dll"), root=root
    )

    assert check.reason == "absolute_path"


def test_zip_bomb_is_rejected_on_declared_ratio(root: Path) -> None:
    """Caught from the central directory, without decompressing a byte.

    Extracting first to find out whether extraction is safe is the bug the
    ordering here avoids.
    """
    check = validate_file(write_zip_bomb(root / "bomb.docx"), root=root)

    assert check.reason == "compression_ratio"
    assert check.security_event is True


def test_too_many_entries_is_rejected(root: Path) -> None:
    check = validate_file(write_many_entries(root / "many.docx", MAX_ZIP_ENTRIES + 1), root=root)

    assert check.reason == "too_many_entries"


def test_entry_count_at_the_limit_passes(root: Path) -> None:
    """Off-by-one here rejects legitimate CVs with many embedded images."""
    path = write_many_entries(root / "ok.docx", MAX_ZIP_ENTRIES - 3)

    assert validate_file(path, root=root).ok is True


def test_zip_without_word_document_is_rejected(root: Path) -> None:
    path = root / "plain.docx"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("hello.txt", "not a word document")

    assert validate_file(path, root=root).reason == "not_a_word_document"


def test_corrupt_zip_is_rejected_not_crashed(root: Path) -> None:
    path = root / "corrupt.docx"
    path.write_bytes(b"PK\x03\x04" + b"\x00" * 400)

    assert validate_file(path, root=root).reason == "corrupt_zip"


# --- pdf page bomb -----------------------------------------------------------


def test_page_bomb_is_rejected(root: Path) -> None:
    path = write_pdf(root / "bomb.pdf", pages=settings.max_pages + 5)

    check = validate_file(path, root=root)

    assert check.reason == "too_many_pages"
    assert check.detail["pages"] == settings.max_pages + 5


def test_page_count_at_the_limit_passes(root: Path) -> None:
    assert validate_file(write_pdf(root / "long.pdf", pages=settings.max_pages), root=root).ok


def test_unknowable_page_count_does_not_reject(root: Path) -> None:
    """Pinned deliberately, because it looks like a hole and is a considered trade.

    A PDF 1.5+ page tree can sit inside compressed object streams, where neither
    signal is readable without a parse — which is the thing this check runs
    before. Rejecting on an unknown count would refuse most legitimately
    produced CVs. The sandbox's rlimits and the post-parse `page_count` are the
    real enforcement.
    """
    path = root / "opaque.pdf"
    path.write_bytes(b"%PDF-1.7\n" + b"\x00\x01\x02\x03" * 100 + b"\n%%EOF\n")

    check = validate_file(path, root=root)

    assert check.ok is True
