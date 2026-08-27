"""Evidence verification (spec 10.5, build gate 20 step 3).

The fixtures here are the ones the spec names as gates. Several encode holes that
earlier drafts actually shipped, so they are regression tests, not illustrations.
"""

from conftest import make_rubric

from screener.core.verify_evidence import (
    align,
    evidence_mentions_criterion,
    is_non_substantive,
    quote_verifies,
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


def test_char_floor_still_rejects_a_quote_whose_matched_part_is_all_short_words() -> None:
    """`ratio OR 25 chars` let a tiny fragment verify a fabricated quote.

    This is the case the character floor is actually for, isolated: a quote long
    enough that the `_is_verified` cap does not apply, mostly fabricated, whose
    *real* part is a contiguous run of function words. It clears the ratio bar
    and the span bar, and only the character floor stops it.

    Kept as a live guard on the cap added for skills-list quotes: capping the
    floor at the quote's own length must not reach a quote this long.
    """
    quote = "and Go across the Kubernetes Terraform"
    result = align(quote, RESUME)
    assert result.ratio >= 0.60 and result.longest_span >= 3  # passes two conditions
    assert result.evidence_chars > 16  # long enough that the cap does not apply
    assert result.matched_chars < 16  # fails the third, uncapped

    out = judge(
        ("C1", "strong", quote),
        ("C2", "none", "not found"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )
    result = verify_evidence(out, RESUME, RUBRIC)
    assert Flag.EVIDENCE_UNVERIFIED in result.flags
    assert result.scoreable is False


# --- the character floor, capped at the quote's own length -------------------

# Mirrors the rubric and resumes of run-955a8e246735: an Executive Assistant
# posting whose must-haves are answered by a skills-list line, not a sentence.
EA_RESUME = (
    "Sabrina Atouf. Executive Assistant supporting the CEO of a regional hub. "
    "Coordinated board meetings, VIP visits and quarterly leadership forums. "
    "SKILLS: Microsoft Office, SAP Concur, document management. "
    "LANGUAGES: English (Fluent), French (Native)."
)

EA_RUBRIC = make_rubric(
    ("C1", True, 3, "Fluent in English"),
    ("C2", True, 3, "Advanced proficiency in Microsoft Office Suite (PowerPoint, Excel, Word)"),
    ("C3", False, 1, "Experience coordinating VIP visits and board meetings"),
    ("C4", False, 1, "Migration to microservices"),
)


def test_a_quote_shorter_than_the_char_floor_verifies_when_fully_matched() -> None:
    """A quote holding fewer characters than the floor must not be held to it.

    Regression from run-955a8e246735, where this left 152 of 314 candidates
    (48%) unscoreable, 136 of them for this reason alone. Both failing criteria
    were must-haves whose honest evidence on a real resume is a skills-list
    entry: `Microsoft Office` is 15 characters and was rejected at `ratio=1.0`
    by a 16-character bar it could never have reached.

    The same cap `align` applies to its block filter and `_is_verified` applies
    to `longest_span`, applied to the third bar. Without it a perfect match is
    indistinguishable from no match at all.
    """
    office = align("Microsoft Office", EA_RESUME)
    assert office.ratio == 1.0
    assert office.matched_chars == office.evidence_chars == 15  # every character matched
    assert office.matched_chars < 16  # yet under the uncapped floor

    english = align("English (Fluent)", EA_RESUME)
    assert english.ratio == 1.0
    assert english.matched_chars == english.evidence_chars == 13

    out = judge(
        ("C1", "strong", "English (Fluent)"),
        ("C2", "strong", "Microsoft Office"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )
    result = verify_evidence(out, EA_RESUME, EA_RUBRIC)

    assert [c.verified for c in result.criteria[:2]] == [True, True]
    assert result.scoreable is True
    assert Flag.EVIDENCE_UNVERIFIED not in result.flags


def test_the_cap_is_not_a_free_pass_for_short_quotes() -> None:
    """Capping the floor must not verify a short quote that simply is not there.

    The cap lowers the bar to the quote's own size; it does not remove the other
    two conditions. A fabricated skills-list entry has nothing to match and
    fails on ratio, exactly as before.
    """
    assert not quote_verifies("Kubernetes Terraform", EA_RESUME)
    assert not quote_verifies("Arabic (Fluent)", EA_RESUME)

    out = judge(
        ("C1", "strong", "Arabic (Fluent)"),
        ("C2", "strong", "Kubernetes Terraform"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )
    result = verify_evidence(out, EA_RESUME, EA_RUBRIC)

    assert all(c.verified is False for c in result.criteria[:2])
    assert Flag.EVIDENCE_UNVERIFIED in result.flags
    assert result.scoreable is False
    # Escalated, never downgraded — the 10.5(b) contract is unchanged.
    assert [c.verdict for c in result.criteria[:2]] == ["strong", "strong"]


def test_a_filler_only_quote_is_never_capped() -> None:
    """The hole the character cap would open if it applied unconditionally.

    A quote of pure function words is short for a different reason than a
    skills-list entry: it is not a small true thing, it is nothing. Capping it
    would set its bar to its own length, which it clears by definition — and
    short function-word runs occur in most documents, so it would verify against
    almost any resume. Measured across run-955a8e246735's 314 resumes with the
    cap ungated: ``"and"`` verified against 100% of them, ``"of the"`` 44%,
    ``"in the"`` 33%. All three are rejected outright before the fix and must
    stay rejected after it.

    So the cap is withheld unless a token survives `_TOPIC_STOPWORDS`, and
    filler faces the full `evidence_match_min_chars` floor it cannot reach.
    """
    filler = "Executive Assistant supporting the CEO and the board of the regional hub."

    for quote in ("and", "and the", "of the", "in the", "is and the"):
        result = align(quote, filler)
        assert result.evidence_content_tokens == 0, quote
        # Present in the document, and still refused: the cap never applies.
        assert not quote_verifies(quote, filler), quote

    # The contrast that makes the rule: same length class, but it says something.
    office = align("Microsoft Office", EA_RESUME)
    assert office.evidence_content_tokens == 2
    assert quote_verifies("Microsoft Office", EA_RESUME)


def test_filler_only_evidence_escalates_end_to_end() -> None:
    """A filler quote must still pull the candidate out of the ranking.

    `evidence_mentions_criterion` would flag this quote too, but check (c) only
    flags — it never unranks. If the cap let filler verify, a `strong` verdict
    backed by ``"and the"`` would keep its full weight in the arithmetic with
    nothing stopping it. Stage B is what has to catch this.
    """
    out = judge(
        ("C1", "strong", "and the"),
        ("C2", "strong", "of the"),
        ("C3", "none", "not found"),
        ("C4", "none", "not found"),
    )
    result = verify_evidence(out, EA_RESUME, EA_RUBRIC)

    assert all(c.verified is False for c in result.criteria[:2])
    assert Flag.EVIDENCE_UNVERIFIED in result.flags
    assert result.scoreable is False


def test_a_partially_matched_short_quote_still_fails_on_ratio() -> None:
    """The cap keys off the quote's own length, not off how much of it matched.

    A short quote half-invented still has to clear `evidence_match_ratio`, so
    the cap cannot be reached by shrinking the matched portion.
    """
    result = align("Microsoft Kubernetes", EA_RESUME)
    assert result.ratio < 0.60
    assert not quote_verifies("Microsoft Kubernetes", EA_RESUME)


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
