"""Evidence verification (spec 10.5, build gate 20 step 3).

The fixtures here are the ones the spec names as gates. Several encode holes that
earlier drafts actually shipped, so they are regression tests, not illustrations.
"""

from conftest import make_rubric

from screener.core.verify_evidence import (
    align,
    evidence_mentions_criterion,
    is_non_substantive,
    verify_evidence,
)
from screener.models import CriterionVerdict, Flag, JudgeOutput

RESUME = (
    "Asha Nair. Senior Backend Engineer with 7 years backend engineer experience. "
    "Built and operated payment systems handling 40 million requests per day. "
    "Python and Go across the whole stack, with PostgreSQL and Redis for storage. "
    "Led the migration from a monolith to microservices between 2019 and 2024."
)

RUBRIC = make_rubric(
    ("C1", True, 3, "Payment systems at scale"),
    ("C2", True, 2, "Backend engineering experience"),
    ("C3", False, 1, "Python and Go"),
    ("C4", False, 1, "Migration to microservices"),
)


def judge(*pairs: tuple[str, str, str]) -> JudgeOutput:
    """Build a JudgeOutput from ``(id, verdict, evidence)`` triples."""
    return JudgeOutput(
        criteria=[CriterionVerdict(id=i, verdict=v, evidence=e) for i, v, e in pairs]  # type: ignore[arg-type]
    )


# --- (a) consistency gate ---------------------------------------------------


def test_injection_fixture_is_forced_to_none() -> None:
    """The verified live attack.

    A resume carrying `IGNORE ALL PREVIOUS INSTRUCTIONS` flipped every verdict to
    `strong` while the evidence field still read `not found (...)`. That
    self-contradiction is the hook, and it costs nothing to check.
    """
    out = judge(
        ("C1", "strong", "not found (candidate has only 1 year of experience)"),
        ("C2", "strong", "not found (limited to HTML and CSS only)"),
        ("C3", "strong", "not found (no mention of Kubernetes)"),
        ("C4", "strong", "not found"),
    )
    result = verify_evidence(out, RESUME, RUBRIC)

    assert all(c.verdict == "none" for c in result.criteria)
    assert all(c.model_verdict == "strong" for c in result.criteria)  # audit trail intact
    assert Flag.EVIDENCE_CONTRADICTS in result.flags
    assert result.review_required is True


def test_none_verdict_with_not_found_is_not_flagged() -> None:
    """The honest case: verdict and evidence agree, so nothing is wrong."""
    out = judge(
        ("C1", "none", "not found"),
        ("C2", "none", "not found"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )
    result = verify_evidence(out, RESUME, RUBRIC)
    assert result.flags == []
    assert result.scoreable is True


def test_non_substantive_variants() -> None:
    for text in ["not found", "N/A", "none", "  ", "Not mentioned in the resume", "unknown"]:
        assert is_non_substantive(text), text
    assert not is_non_substantive("7 years backend engineer experience at scale")


# --- (b) quote verification -------------------------------------------------


def test_verbatim_quote_verifies() -> None:
    quote = "Built and operated payment systems handling 40 million requests per day"
    ratio, span, chars = align(quote, RESUME)
    assert ratio == 1.0
    assert span >= 3
    assert chars >= 25


def test_mid_quote_insertion_still_verifies() -> None:
    """A long quote with one word inserted mid-span must not false-escalate.

    Single-longest-match scoring gave this ~0.5 and escalated honest evidence.
    Summing filtered blocks recovers both halves.
    """
    quote = (
        "Built and operated payment systems INSERTED handling 40 million requests per day "
        "Python and Go across the whole stack"
    )
    ratio, span, chars = align(quote, RESUME)
    assert ratio >= 0.60
    assert span >= 3
    assert chars >= 25


def test_stopword_confetti_is_rejected() -> None:
    """Evidence built from common words must not verify against an arbitrary resume.

    This is the hole an unfiltered `sum(get_matching_blocks())` opens: size-1
    blocks are stopword noise, and enough of them approach ratio 1.0 against
    anything. MIN_BLOCK is what closes it.
    """
    quote = "the and of the in a for the with and to the from a of the"
    ratio, _span, _chars = align(quote, RESUME)
    assert ratio < 0.60


def test_short_fragment_is_rejected_despite_perfect_ratio() -> None:
    """`ratio OR 25 chars` let a tiny fragment verify a fabricated quote.

    The three conditions are AND-ed, so a fully-matching but very short quote
    still escalates.
    """
    ratio, span, chars = align("Python and Go", RESUME)
    assert ratio == 1.0 and span >= 3  # passes two conditions
    assert chars < 25  # fails the third

    out = judge(
        ("C1", "strong", "Python and Go"),
        ("C2", "none", "not found"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )
    result = verify_evidence(out, RESUME, RUBRIC)
    assert Flag.EVIDENCE_UNVERIFIED in result.flags
    assert result.scoreable is False


def test_fabricated_quote_escalates_without_changing_the_verdict() -> None:
    """Escalation, not downgrade: a paraphrase quirk must not become an adverse outcome."""
    out = judge(
        ("C1", "strong", "Ten years of Kubernetes and Terraform across four continents"),
        ("C2", "none", "not found"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )
    result = verify_evidence(out, RESUME, RUBRIC)

    c1 = next(c for c in result.criteria if c.id == "C1")
    assert c1.verdict == "strong"  # untouched
    assert c1.verified is False
    assert Flag.EVIDENCE_UNVERIFIED in result.flags
    assert result.scoreable is False and result.review_required is True


def test_autojunk_regression_on_a_large_document() -> None:
    """`autojunk` silently destroys matching once len(b) >= 200.

    With it enabled, common tokens in a 20 kB resume are treated as junk and an
    exact quote stops verifying. `autojunk=False` is not optional.
    """
    big = (RESUME + " Additional filler describing unrelated project work. ") * 60
    assert len(big) > 20_000

    quote = "Built and operated payment systems handling 40 million requests per day"
    ratio, span, chars = align(quote, big)
    assert ratio == 1.0
    assert span >= 3 and chars >= 25


def test_matching_is_against_the_redacted_text_actually_sent() -> None:
    """Quotes must be checked against `sent`, not the pre-redaction original."""
    redacted = RESUME.replace("Asha Nair", "[REDACTED]")
    ratio, _, _ = align(
        "Senior Backend Engineer with 7 years backend engineer experience", redacted
    )
    assert ratio == 1.0


def test_persisted_metrics_are_recorded_for_tuning() -> None:
    """match_ratio and longest_span exist so thresholds are tuned against data (18.2)."""
    out = judge(
        ("C1", "strong", "Built and operated payment systems handling 40 million requests per day"),
        ("C2", "none", "not found"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )
    c1 = next(c for c in verify_evidence(out, RESUME, RUBRIC).criteria if c.id == "C1")
    assert c1.match_ratio == 1.0
    assert c1.longest_span >= 3
    assert c1.verified is True


def test_criterion_metadata_comes_from_the_rubric() -> None:
    """Weight and must_have are never taken from model output."""
    out = judge(
        ("C1", "strong", "Built and operated payment systems handling 40 million requests per day"),
        ("C2", "none", "not found"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )
    result = verify_evidence(out, RESUME, RUBRIC)
    c1 = next(c for c in result.criteria if c.id == "C1")
    assert c1.weight == 3 and c1.must_have is True


# --- (c) relevance: real quote, wrong criterion ------------------------------


def test_a_real_quote_about_something_else_is_caught() -> None:
    """The failure (a) and (b) both miss, measured on this system.

    The model returned `strong` for "Rust in production" on a resume containing
    no Rust, evidencing it with a genuine sentence about Go. The quote is
    verbatim, so it verified at ratio 1.00; the consistency gate saw substantive
    text and passed it; the id set was correct. A wrong verdict went into the
    arithmetic with nothing reporting a problem.
    """
    rubric = make_rubric(
        ("C1", True, 3, "Rust in production"),
        ("C2", True, 2, "Backend engineering experience"),
        ("C3", False, 1, "Python and Go"),
        ("C4", False, 1, "Migration to microservices"),
    )

    out = judge(
        ("C1", "strong", "Built and operated payment systems handling 40 million requests"),
        ("C2", "none", "not found"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )
    result = verify_evidence(out, RESUME, rubric)

    assert Flag.EVIDENCE_IRRELEVANT in result.flags
    assert result.review_required is True
    # **Flagged, not unranked.** Measured on a live run, this check removed the
    # two strongest candidates because their evidence was concrete
    # ("payments monolith to microservices") while the criterion was generic
    # ("production backend services") — the right evidence, no shared word. A
    # lexical heuristic that misfires that often has not earned the power to
    # take someone out of a ranking.
    assert result.scoreable is True
    assert result.criteria[0].verdict == "strong"


def test_a_relevant_quote_passes() -> None:
    rubric = make_rubric(
        ("C1", True, 3, "Payment systems at scale"),
        ("C2", True, 2, "Backend engineering experience"),
        ("C3", False, 1, "Python and Go"),
        ("C4", False, 1, "Migration to microservices"),
    )

    out = judge(
        ("C1", "strong", "Built and operated payment systems handling 40 million requests"),
        ("C2", "none", "not found"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )
    result = verify_evidence(out, RESUME, rubric)

    assert Flag.EVIDENCE_IRRELEVANT not in result.flags
    assert result.scoreable is True


def test_relevance_needs_only_one_content_word() -> None:
    """Deliberately crude. It looks for evidence about something *else entirely*,
    not for a well-argued match — that judgement is what the model is for."""
    assert evidence_mentions_criterion("Strong Python", "Built REST APIs in Python and Django")
    assert evidence_mentions_criterion("Go in production", "microservices written in Go")
    assert not evidence_mentions_criterion("Rust in production", "microservices written in Go")


def test_stopword_only_criteria_are_not_flagged() -> None:
    """Nothing to look for means nothing to fail on."""
    assert evidence_mentions_criterion("has experience with", "anything at all")
