"""End-to-end screening (spec 13, build gate 20 step 12).

The first point in the build where a **real file** goes through the **real
sandbox** to the **real model** and comes back as a scored candidate. Everything
below the pipeline has been exercised in isolation; this is the only test that
proves the pieces fit together — that the parser's layout markup survives
evidence matching, that the budget pre-check predicts the actual prompt, and that
a DOCX on disk becomes a band.

Run with `pytest -m live`.
"""

from pathlib import Path

import pytest
from fixtures_docs import real_docx, scanned_pdf

from screener.clients.ollama_client import OllamaClient
from screener.intake.sandbox import SandboxedParser
from screener.models import Criterion, Flag, Rubric
from screener.pipeline import Deps, judge_one, screen_batch

pytestmark = pytest.mark.live

RESUME_TEXT = (
    "Asha Nair. Senior Backend Engineer with 7 years of experience. "
    "Led the migration of a payments monolith to microservices in Go, "
    "handling 40 million requests per day. Ran the on-call rotation and owned "
    "the Kubernetes platform for 12 services. Built REST APIs in Python and "
    "Django, with PostgreSQL schema design and query optimisation. "
    "Some exposure to Rust through a training course."
)

RUBRIC = Rubric(
    id="r-live",
    position_id="p-live",
    version=1,
    created_by="tester",
    criteria=[
        Criterion(
            id="C1", text="5+ years building production backend services", must_have=True, weight=5
        ),
        Criterion(
            id="C2", text="Operating services on Kubernetes in production", must_have=True, weight=4
        ),
        Criterion(id="C3", text="Strong Python", weight=3),
        Criterion(id="C4", text="Rust in production", weight=1),
    ],
)


@pytest.fixture(scope="module")
def deps() -> Deps:
    llm = OllamaClient()
    if not llm.health():
        pytest.skip("ollama unreachable")
    return Deps(parser=SandboxedParser(), llm=llm)


@pytest.fixture
def root(tmp_path: Path) -> Path:
    folder = tmp_path / "resumes" / "REQ-1"
    folder.mkdir(parents=True)
    return folder


def test_a_docx_becomes_a_scored_candidate(deps: Deps, root: Path) -> None:
    """The whole chain: file → sandbox → sanitize → redact → judge → verify → score."""
    path = real_docx(root / "asha.docx", paragraphs=(RESUME_TEXT,))

    candidate = judge_one(path, RUBRIC, deps, run_id="run1", root=root)

    assert candidate.scoreable is True, candidate.flags
    assert candidate.score is not None
    assert candidate.band in ("A", "B", "C", "D")
    assert len(candidate.criteria) == 4
    assert candidate.file_sha256


def test_verdicts_reflect_the_document(deps: Deps, root: Path) -> None:
    """Not just well-formed — right.

    Rust appears only as training-course exposure, which 9.2's anchors define
    as `none`. A pipeline that scored it `strong` would be well-formed and wrong.
    """
    path = real_docx(root / "asha.docx", paragraphs=(RESUME_TEXT,))

    candidate = judge_one(path, RUBRIC, deps, run_id="run1", root=root)
    by_id = {c.id: c for c in candidate.criteria}

    assert by_id["C1"].verdict in ("strong", "partial")
    assert by_id["C2"].verdict in ("strong", "partial")
    assert by_id["C4"].verdict == "none", by_id["C4"].evidence


def test_evidence_verifies_against_real_parser_output(deps: Deps, root: Path) -> None:
    """The integration 10.5 depends on and no unit test can prove.

    xberg emits layout markup — OCR output arrives as markdown tables. Evidence
    is matched against that exact string, so a quote spanning a cell boundary
    carries pipes in the middle of it. If `normalize_tokens` stopped stripping
    them, this surfaces as unexplained escalations on scanned CVs rather than as
    a parse failure.
    """
    path = real_docx(root / "asha.docx", paragraphs=(RESUME_TEXT,))

    candidate = judge_one(path, RUBRIC, deps, run_id="run1", root=root)
    supported = [c for c in candidate.criteria if c.verdict != "none"]

    assert supported
    assert Flag.EVIDENCE_UNVERIFIED not in candidate.flags
    assert all(c.verified for c in supported), [
        (c.id, round(c.match_ratio, 2), c.evidence) for c in supported
    ]


def test_a_scanned_resume_screens_through_ocr(deps: Deps, root: Path) -> None:
    """A scanned CV must reach a score, not an EXTRACTION_FAILED.

    Without OCR the whole corpus of scanned applications would come back empty
    and the conclusion drawn would be about model quality (3.3).
    """
    path = scanned_pdf(root / "scan.pdf", text="Senior Backend Engineer, 7 years, Python and Go")

    candidate = judge_one(path, RUBRIC, deps, run_id="run1", root=root)

    assert Flag.EXTRACTION_FAILED not in candidate.flags
    assert candidate.criteria


def test_a_corrupt_file_yields_no_score_and_does_not_stop_the_batch(deps: Deps, root: Path) -> None:
    """The failure isolation the worker relies on (16.1)."""
    good = real_docx(root / "good.docx", paragraphs=(RESUME_TEXT,))
    corrupt = root / "corrupt.pdf"
    corrupt.write_bytes(b"%PDF-1.4\n" + bytes(range(256)) * 20)

    results = screen_batch([good, corrupt], RUBRIC, deps, run_id="run1", root=root)

    assert len(results) == 2
    assert results[0].score is not None
    assert results[1].score is None
    assert results[1].review_required is True


def test_the_budget_precheck_matches_the_real_prompt(deps: Deps, root: Path) -> None:
    """No `BUDGET_EXCEEDED` on an ordinary resume, and no silent overflow either.

    Both failures are invisible in the output: a false positive escalates a
    normal candidate, and a false negative judges a truncated document.
    """
    path = real_docx(root / "asha.docx", paragraphs=(RESUME_TEXT,))

    candidate = judge_one(path, RUBRIC, deps, run_id="run1", root=root)

    assert Flag.BUDGET_EXCEEDED not in candidate.flags


def test_the_candidate_carries_both_stored_text_versions(deps: Deps, root: Path) -> None:
    """12.6: `resume_text` is what HR reads, `sent_text` is what the model saw.

    This replaced the trace: the same content, in the database, where retention,
    permissions and the erasure path already exist. Names are deliberately not
    redacted (see `redact_pii`) — identifying one in free text needs NER, and
    `Candidate.filename` carries it regardless — so both columns hold the resume
    in full, which is why both are named in `purge_candidate`.
    """
    path = real_docx(root / "asha.docx", paragraphs=(RESUME_TEXT,))

    candidate = judge_one(path, RUBRIC, deps, run_id="run1", root=root)

    assert "Asha Nair" in candidate.resume_text
    assert "Asha Nair" in candidate.sent_text
    assert candidate.redaction_map, "no span map means every highlight lands wrong"
    # The map covers the redacted string end to end, gaps included.
    assert candidate.redaction_map[-1].dst_end <= len(candidate.sent_text)
