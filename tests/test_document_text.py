"""Uploaded document → text (spec 8, 9.1). The real parser, not a fake.

`DocumentTextExtractor` is three existing steps composed in one order, so what is
worth testing is not the steps — 8.2, 8.4 and 8.6 each have their own suite — but
that the composition is the one the caller needs: a rejected file never reaches
the parser, and a parsed file never reaches the caller unsanitized.

The extraction cases run the actual sandbox against actual documents. A fake
parser here would test the plumbing and nothing about whether this system can
read a job description, which is the entire claim being made.
"""

from pathlib import Path

import pytest

from config.settings import settings
from screener.intake.document_text import DocumentTextExtractor, sha256_of
from screener.models import Flag, ParsedResume, ParseResult
from tests.fixtures_docs import real_docx
from tests.fixtures_files import write_docx, write_pdf

JD_PARAGRAPHS = (
    "Senior Backend Engineer",
    "Required: 5+ years of backend engineering. Kubernetes is essential.",
    "Nice to have: Go, and experience mentoring juniors.",
)


@pytest.fixture
def extractor() -> DocumentTextExtractor:
    return DocumentTextExtractor()


# --- it reads real documents -------------------------------------------------


def test_a_real_docx_job_description_becomes_its_text(
    extractor: DocumentTextExtractor, tmp_path: Path
) -> None:
    data = real_docx(tmp_path / "jd.docx", paragraphs=JD_PARAGRAPHS).read_bytes()

    result = extractor.extract(data, filename="senior-backend.docx")

    assert result.parsed is not None
    assert "Kubernetes is essential" in result.parsed.text
    assert result.parsed.ocr_used is False
    assert result.parsed.parser_version.startswith("xberg/")


def test_a_pdf_with_no_text_layer_fails_as_a_data_quality_event(
    extractor: DocumentTextExtractor, tmp_path: Path
) -> None:
    """`EXTRACTION_FAILED`, not a crash and not an empty string.

    This is the scanned job description on a host with no OCR available, and it
    is the case the distinction in 8.5 exists for: the parser read the file,
    understood there was no text in it, and said so. The reviewer has to be told
    to paste the text — which they cannot be if this arrives as empty output that
    looks like a successful read of a blank document.
    """
    data = write_pdf(tmp_path / "jd.pdf").read_bytes()

    result = extractor.extract(data, filename="jd.pdf")

    assert result.parsed is None
    assert result.flags == [Flag.EXTRACTION_FAILED]
    assert not result.security_event


# --- it rejects before it parses ---------------------------------------------


def test_a_file_pretending_to_be_a_pdf_never_reaches_the_parser(
    extractor: DocumentTextExtractor, tmp_path: Path
) -> None:
    """The extension is not consulted for the type; the type is sniffed (8.2).

    A `.pdf` that is really something else is not a user mistake pattern, it is
    somebody establishing which of the two this intake trusts.
    """
    result = extractor.extract(b"MZ\x90\x00 not a pdf at all", filename="jd.pdf")

    assert result.parsed is None
    assert result.flags == [Flag.INPUT_REJECTED]


def test_an_unsupported_extension_is_refused_without_writing_it(
    extractor: DocumentTextExtractor,
) -> None:
    result = extractor.extract(b"#!/bin/sh\nrm -rf /\n", filename="jd.sh")

    assert result.parsed is None
    assert result.flags == [Flag.INPUT_REJECTED]
    assert "jd.sh" in result.error or ".sh" in result.error


def test_a_traversal_filename_cannot_escape_the_scratch_directory(
    extractor: DocumentTextExtractor, tmp_path: Path
) -> None:
    """The stem is generated; only the suffix comes from the upload.

    The name arrives from a browser, so it can be absolute or full of `..`. If
    it were used verbatim the write itself would be the vulnerability — before
    any of 8.2's checks got a chance to run.
    """
    data = real_docx(tmp_path / "ok.docx").read_bytes()
    marker = tmp_path / "escaped.docx"

    result = extractor.extract(data, filename=f"../../../../{marker}")

    assert not marker.exists()
    assert result.parsed is not None  # the suffix was honoured, the path was not


def test_the_tighter_caps_are_honoured(extractor: DocumentTextExtractor, tmp_path: Path) -> None:
    """A job description gets 2 MB and 10 pages, not the resume path's 5 MB / 50.

    Passed per call rather than read from settings inside `validate_file`,
    because both limits are live in the same process: the worker is screening
    resumes under the wider ones while this runs.
    """
    data = write_pdf(tmp_path / "big.pdf", pages=12).read_bytes()

    tight = extractor.extract(data, filename="big.pdf", max_pages=10)
    assert tight.flags == [Flag.INPUT_REJECTED]
    assert "too_many_pages" in tight.error

    # The same document clears validation under the resume path's cap and is
    # handed to the parser, which is where it fails for an unrelated reason —
    # this fixture has a page tree and no text layer. That it gets that far is
    # the assertion: the cap moved, nothing else did.
    loose = extractor.extract(data, filename="big.pdf", max_pages=50)
    assert loose.flags == [Flag.EXTRACTION_FAILED]

    oversize = write_docx(tmp_path / "big.docx").read_bytes()
    rejected = extractor.extract(oversize, filename="big.docx", max_bytes=10)
    assert rejected.parsed is None
    assert rejected.flags == [Flag.INPUT_REJECTED]
    assert "oversize" in rejected.error


# --- it sanitizes before the caller sees anything ----------------------------


class _RawParser:
    """Returns text a hostile document would produce, without touching a file."""

    def __init__(self, text: str) -> None:
        self._text = text

    def parse(self, path: Path, *, timeout_s: int | None = None) -> ParseResult:
        return ParseResult(
            parsed=ParsedResume(
                text=self._text, page_count=1, ocr_used=False, parser_version="fake/1.0"
            )
        )


def test_the_text_handed_back_has_already_been_sanitized(tmp_path: Path) -> None:
    """8.6, and it is load-bearing *here* specifically.

    This feature's premise is that a person reads the extracted text before a
    rubric is drafted from it. Returning the raw extract and sanitizing later
    would mean they are reading a string with the bidi overrides and zero-width
    characters still in it — reader and model seeing different documents, which
    is the exact divergence sanitization exists to close, rebuilt inside the
    control meant to close it.
    """
    hostile = "Required:​ 5+ years‮ backend"
    extractor = DocumentTextExtractor(parser=_RawParser(hostile))
    data = real_docx(tmp_path / "jd.docx").read_bytes()

    result = extractor.extract(data, filename="jd.docx")

    assert result.parsed is not None
    assert "​" not in result.parsed.text
    assert "‮" not in result.parsed.text
    assert result.parsed.chars_stripped == 2
    assert "Required: 5+ years backend" == result.parsed.text


def test_a_parser_failure_is_passed_through_untouched(tmp_path: Path) -> None:
    """A failed parse is a `ParseResult` with flags, not an exception.

    The caller has to be able to tell a timeout from a rejection, because only
    one of them is worth telling somebody to retry.
    """

    class _Failing:
        def parse(self, path: Path, *, timeout_s: int | None = None) -> ParseResult:
            return ParseResult(flags=[Flag.PARSER_TIMEOUT], error="too slow")

    extractor = DocumentTextExtractor(parser=_Failing())
    data = real_docx(tmp_path / "jd.docx").read_bytes()

    result = extractor.extract(data, filename="jd.docx")

    assert result.parsed is None
    assert result.flags == [Flag.PARSER_TIMEOUT]


def test_the_scratch_directory_does_not_survive_the_call(
    extractor: DocumentTextExtractor, tmp_path: Path
) -> None:
    """Uploaded bytes live on disk for the length of one parse and no longer.

    They are outside `data/`, so nothing in the erasure path (12.6) would ever
    find them — which makes "deleted in a `finally`" the only guarantee there is.
    """
    import tempfile

    before = set(Path(tempfile.gettempdir()).glob("screener-jd-*"))
    extractor.extract(real_docx(tmp_path / "jd.docx").read_bytes(), filename="jd.docx")

    assert set(Path(tempfile.gettempdir()).glob("screener-jd-*")) == before


def test_the_hash_is_of_the_uploaded_bytes() -> None:
    """Named for what it identifies. See `JdExtraction.file_sha256`."""
    assert sha256_of(b"") == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def test_allowed_extensions_are_not_a_per_call_parameter() -> None:
    """The one knob deliberately absent from `extract` (8.2).

    `max_bytes` and `max_pages` exist so a caller can be *stricter*. A per-call
    type list is the parameter that would let one be laxer, which is the
    direction this intake must not be configurable in.
    """
    import inspect

    parameters = set(inspect.signature(DocumentTextExtractor.extract).parameters)

    assert "allowed_extensions" not in parameters
    assert settings.allowed_extensions == (".pdf", ".docx")
