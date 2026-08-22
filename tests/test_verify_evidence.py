"""Evidence verification (spec 10.5, build gate 20 step 3).

The fixtures here are the ones the spec names as gates. Several encode holes that
earlier drafts actually shipped, so they are regression tests, not illustrations.
"""

from conftest import make_rubric

from screener.core.verify_evidence import (
    align,
    evidence_mentions_criterion,
    is_non_substantive,
    tokenize_with_offsets,
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
    result = align(quote, RESUME)
    assert result.ratio == 1.0
    assert result.longest_span >= 3
    assert result.matched_chars >= 25


def test_mid_quote_insertion_still_verifies() -> None:
    """A long quote with one word inserted mid-span must not false-escalate.

    Single-longest-match scoring gave this ~0.5 and escalated honest evidence.
    Summing filtered blocks recovers both halves.
    """
    quote = (
        "Built and operated payment systems INSERTED handling 40 million requests per day "
        "Python and Go across the whole stack"
    )
    result = align(quote, RESUME)
    assert result.ratio >= 0.60
    assert result.longest_span >= 3
    assert result.matched_chars >= 25


def test_stopword_confetti_is_rejected() -> None:
    """Evidence built from common words must not verify against an arbitrary resume.

    This is the hole an unfiltered `sum(get_matching_blocks())` opens: size-1
    blocks are stopword noise, and enough of them approach ratio 1.0 against
    anything. MIN_BLOCK is what closes it.
    """
    quote = "the and of the in a for the with and to the from a of the"
    result = align(quote, RESUME)
    assert result.ratio < 0.60


def test_a_genuinely_short_quote_still_verifies() -> None:
    """A quote shorter than `evidence_min_block_tokens` (3) must not be held to
    a block-size bar it can never reach.

    Regression, observed in practice: a verbatim two-word quote from a
    skills-list bullet ("Technical Documentation") scored `ratio=0.0` against
    a resume that contained it exactly — the perfect match topped out at a
    block the size of the whole quote (2), smaller than the filter demanded
    (3), so it was silently indistinguishable from no match at all.
    """
    resume = (
        "SKILLS: Aviation Safety & Compliance, Technical Documentation, "
        "Maintenance Software. WORK EXPERIENCE: Aircraft Maintenance Engineer."
    )
    result = align("Technical Documentation", resume)
    assert result.ratio == 1.0
    assert result.evidence_tokens == 2
    assert result.longest_span == 2
    assert result.matched_chars >= 16

    rubric = make_rubric(
        ("C1", True, 3, "Ability to write technical reports and documentation"),
        ("C2", True, 2, "Backend engineering experience"),
        ("C3", False, 1, "Python and Go"),
        ("C4", False, 1, "Migration to microservices"),
    )
    out = judge(
        ("C1", "strong", "Technical Documentation"),
        ("C2", "none", "not found"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )
    verified = verify_evidence(out, resume, rubric)
    assert verified.criteria[0].verified is True
    assert verified.scoreable is True
    assert Flag.EVIDENCE_UNVERIFIED not in verified.flags


def test_short_fragment_is_rejected_despite_perfect_ratio() -> None:
    """`ratio OR 25 chars` let a tiny fragment verify a fabricated quote.

    The three conditions are AND-ed, so a fully-matching but very short quote
    still escalates.
    """
    result = align("Python and Go", RESUME)
    assert result.ratio == 1.0 and result.longest_span >= 3  # passes two conditions
    assert result.matched_chars < 25  # fails the third

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
    result = align(quote, big)
    assert result.ratio == 1.0
    assert result.longest_span >= 3 and result.matched_chars >= 25


# --- match blocks: which part of the quote was found (12.6) ------------------


def test_blocks_point_at_the_matched_text_in_both_strings() -> None:
    """The offsets have to address real substrings, or the highlight is fiction.

    A ratio of 0.42 is a number nobody can interrogate. The blocks answer the
    question it provokes — *which* part of the quote was not in the resume — and
    they only do that if both ends of every block land where they claim.
    """
    quote = "Built and operated payment systems handling 40 million requests per day"

    result = align(quote, RESUME)

    assert result.blocks
    for block in result.blocks:
        assert quote[block.ev_start : block.ev_end] in RESUME
        assert RESUME[block.doc_start : block.doc_end] == quote[block.ev_start : block.ev_end]


def test_an_unverified_quote_still_reports_what_did_match() -> None:
    """Blocks matter most exactly when verification failed (15.3).

    "Which part of this quote isn't in the resume?" is the reviewer's question
    about an escalated candidate, and answering it is the difference between a
    review queue that gets worked and one that gets clicked through.
    """
    quote = "Python and Go across the whole stack while leading a team of forty engineers"

    result = align(quote, RESUME)

    assert result.ratio < 1.0, "this quote is meant to be partly fabricated"
    matched = [quote[b.ev_start : b.ev_end] for b in result.blocks]
    assert any("Python and Go" in fragment for fragment in matched)
    assert not any("forty engineers" in fragment for fragment in matched)


def test_blocks_are_dropped_by_the_same_filter_as_the_ratio() -> None:
    """Stopword confetti scores no blocks, not just a low ratio.

    If the filter applied to the ratio but not to the blocks, the UI would
    highlight scattered single words across the whole document and present that
    as evidence of a match.
    """
    result = align("the and of the in a for the with and to the from a of the", RESUME)

    assert result.ratio < 0.60
    assert result.blocks == []


def test_a_line_broken_word_stays_one_token() -> None:
    """Soft hyphens are dropped without splitting — a PDF line break is not a word break."""
    tokens, offsets = tokenize_with_offsets("co\xadoperate with teams")

    assert tokens[0] == "cooperate"
    assert offsets[0][0] == 0


def test_offsets_round_trip_through_the_tokenizer() -> None:
    """Every token's span must reproduce that token from the original string."""
    tokens, offsets = tokenize_with_offsets(RESUME)

    for token, (start, end) in zip(tokens, offsets, strict=True):
        assert RESUME[start:end].casefold() == token


def test_verify_evidence_persists_the_blocks() -> None:
    """They are stored per criterion (12.7), so they must survive the scoring step."""
    out = judge(
        ("C1", "strong", "payment systems handling 40 million requests per day"),
        ("C2", "strong", "7 years backend engineer experience"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )

    result = verify_evidence(out, RESUME, RUBRIC)

    c1 = next(c for c in result.criteria if c.id == "C1")
    assert c1.match_blocks
    assert RESUME[c1.match_blocks[0].doc_start : c1.match_blocks[0].doc_end]


def test_matching_is_against_the_redacted_text_actually_sent() -> None:
    """Quotes must be checked against `sent`, not the pre-redaction original."""
    redacted = RESUME.replace("Asha Nair", "[REDACTED]")
    result = align("Senior Backend Engineer with 7 years backend engineer experience", redacted)
    assert result.ratio == 1.0


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
    # Per-criterion, so confirm_relevance knows exactly which id to ask a
    # second, more capable opinion about.
    assert result.criteria[0].evidence_irrelevant is True


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
    assert result.criteria[0].evidence_irrelevant is False


def test_relevance_needs_only_one_content_word() -> None:
    """Deliberately crude. It looks for evidence about something *else entirely*,
    not for a well-argued match — that judgement is what the model is for."""
    assert evidence_mentions_criterion("Strong Python", "Built REST APIs in Python and Django")
    assert evidence_mentions_criterion("Go in production", "microservices written in Go")
    assert not evidence_mentions_criterion("Rust in production", "microservices written in Go")


def test_stopword_only_criteria_are_not_flagged() -> None:
    """Nothing to look for means nothing to fail on."""
    assert evidence_mentions_criterion("has experience with", "anything at all")
