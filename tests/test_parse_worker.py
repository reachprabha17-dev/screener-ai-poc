"""Sandboxed document extraction (spec 8, build gate 20 step 8).

Marked `live` because it runs the real parser in a real subprocess: it needs
xberg's native stack and, for the scanned fixture, its OCR models. That is the
point — a parse path exercised only against a mock has proven nothing about the
one component that touches hostile bytes.

Run with `pytest -m live`.
"""

from pathlib import Path

import pytest
from fixtures_docs import RESUME_TEXT, corrupt_pdf, real_docx, scanned_pdf

from config.settings import settings
from screener.intake.sandbox import SandboxedParser
from screener.models import Flag

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def parser() -> SandboxedParser:
    return SandboxedParser()


# --- the formats that must work ---------------------------------------------


def test_docx_is_parsed(parser: SandboxedParser, tmp_path: Path) -> None:
    result = parser.parse(real_docx(tmp_path / "asha.docx"))

    assert result.ok
    assert result.parsed is not None
    assert "Senior Backend Engineer" in result.parsed.text
    assert result.parsed.page_count >= 1
    assert result.parsed.ocr_used is False
    assert result.parsed.parser_version.startswith("xberg/")
    assert result.flags == []


def test_scanned_pdf_is_read_by_ocr(parser: SandboxedParser, tmp_path: Path) -> None:
    """The fixture that proves OCR runs.

    Without it a corpus of scanned CVs extracts as empty, and the conclusion
    drawn is about model quality rather than about documents that were never
    read (3.3).
    """
    result = parser.parse(scanned_pdf(tmp_path / "scan.pdf"))

    assert result.ok
    assert result.parsed is not None
    assert result.parsed.ocr_used is True
    assert "Backend Engineer" in result.parsed.text
    assert "Python and Go" in result.parsed.text


def test_extracted_text_carries_layout_markup(parser: SandboxedParser, tmp_path: Path) -> None:
    """xberg is layout-aware: it emits markdown, including table pipes.

    Pinned because it reaches 10.5. Evidence is matched against this exact
    string, so a quote spanning a cell boundary arrives with `|` and `---` in
    the middle of it. `normalize_tokens` strips punctuation before aligning,
    which is what keeps that from breaking honest evidence — a regression there
    would show up as unexplained escalations on scanned CVs, not as a parse
    failure.
    """
    result = parser.parse(scanned_pdf(tmp_path / "layout.pdf"))

    assert result.parsed is not None
    from screener.core.verify_evidence import normalize_tokens

    tokens = normalize_tokens(result.parsed.text)
    assert "|" not in tokens
    assert "backend" in tokens


def test_multi_page_scan_reports_its_page_count(parser: SandboxedParser, tmp_path: Path) -> None:
    result = parser.parse(scanned_pdf(tmp_path / "long.pdf", pages=3))

    assert result.ok
    assert result.parsed is not None
    assert result.parsed.page_count == 3


# --- the failures, kept distinct ---------------------------------------------


def test_corrupt_file_is_a_data_quality_event_not_a_security_one(
    parser: SandboxedParser, tmp_path: Path
) -> None:
    """The distinction 8.5's table does not draw explicitly.

    The parser read the file, understood it was broken, and reported it. That is
    a deterministic property of the document — cacheable, and no reason to page
    anybody. Only a parser that dies *without reporting* is treated as an attack.
    """
    result = parser.parse(corrupt_pdf(tmp_path / "corrupt.pdf"))

    assert result.ok is False
    assert result.flags == [Flag.EXTRACTION_FAILED]
    assert result.security_event is False
    assert result.error


def test_empty_document_is_extraction_failed(parser: SandboxedParser, tmp_path: Path) -> None:
    """OCR has already been attempted by this point.

    So an empty result is a statement about the document, not a missing
    capability.
    """
    result = parser.parse(real_docx(tmp_path / "blank.docx", paragraphs=("",)))

    assert result.ok is False
    assert result.flags == [Flag.EXTRACTION_FAILED]


def test_page_bomb_is_caught_after_parsing(
    parser: SandboxedParser, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real page-limit enforcement.

    `validate_file` screens what it can read without parsing; a PDF 1.5+ page
    tree inside compressed object streams is only countable here, after the
    parse it was meant to guard.
    """
    monkeypatch.setattr(settings, "max_pages", 2)

    result = parser.parse(scanned_pdf(tmp_path / "many.pdf", pages=3))

    assert result.ok is False
    assert result.flags == [Flag.INPUT_REJECTED]
    assert "max_pages" in result.error


def test_memory_limit_clears_the_ocr_plateau(parser: SandboxedParser, tmp_path: Path) -> None:
    """A multi-page scan must parse, not abort.

    Regression test for a measured failure: at the spec's original 1024 MB the
    OCR path aborted on any document past three pages, and the candidate came
    back flagged `PARSER_CRASHED` — a security event raised by an ordinary
    scanned CV. Memory use plateaus near 950 MB regardless of page count, so
    this fixture proves the ceiling clears the plateau rather than the fixture.
    """
    result = parser.parse(scanned_pdf(tmp_path / "long-scan.pdf", pages=10))

    assert result.ok, result.error
    assert result.parsed is not None
    assert result.parsed.page_count == 10


def test_missing_file_fails_cleanly(parser: SandboxedParser, tmp_path: Path) -> None:
    result = parser.parse(tmp_path / "does-not-exist.pdf")

    assert result.ok is False
    assert result.parsed is None


# --- the boundary the whole design rests on ---------------------------------


def test_the_parser_never_raises(parser: SandboxedParser, tmp_path: Path) -> None:
    """A file that cannot be read is data, not an exception.

    The pipeline turns `flags` into an unscoreable candidate a human sees.
    Raising would put that decision in a `try` block instead of in the record.
    """
    for path in (
        corrupt_pdf(tmp_path / "a.pdf"),
        tmp_path / "missing.pdf",
        real_docx(tmp_path / "b.docx"),
    ):
        assert parser.parse(path) is not None


def test_text_arrives_needing_sanitization_not_pre_sanitized(
    parser: SandboxedParser, tmp_path: Path
) -> None:
    """Ordering check: the parser returns raw extracted text.

    `sanitize()` runs next in the pipeline (13), against this exact string. If
    the parser were to sanitize, `chars_stripped` would always be zero and the
    `SANITIZED_TEXT` signal would silently stop working.
    """
    result = parser.parse(real_docx(tmp_path / "zw.docx", paragraphs=(f"Py​thon {RESUME_TEXT}",)))

    assert result.ok
    assert result.parsed is not None
    assert "​" in result.parsed.text
