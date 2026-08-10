"""Evidence verification (spec 10.5). Pure — no I/O, no model.

Two checks, deliberately different in strictness *and in consequence*:

**(a) Consistency gate** — exact, zero tolerance, forces the verdict down. If the
model claims support and simultaneously says there is none, it has contradicted
itself. That is unambiguous, so automating it is safe.

**(b) Quote verification** — fuzzy alignment, *escalates* rather than penalises.
Forcing ``verdict="none"`` on a fuzzy-match failure would turn a model paraphrase
quirk into an adverse outcome for a candidate, on a heuristic this spec documents
as imperfect. When the system cannot verify its own output the correct response
is a human, not a silent penalty. The verdict is left exactly as returned and the
candidate leaves the ranking.

**Scope.** This is anti-hallucination, not anti-injection. Evidence is quoted
from attacker-controlled text: it confirms the model quoted the resume
faithfully, and can say nothing about whether the resume itself is truthful.
Never present it as fraud detection.
"""

import difflib
import re
from dataclasses import dataclass, field

from config.settings import settings
from screener.models import (
    CriterionVerdict,
    Flag,
    JudgeOutput,
    MatchBlock,
    Rubric,
    ScoredCriterion,
)

_SOFT_HYPHEN = "­"
_WORD = re.compile(r"\w", re.UNICODE)

# An assertion of support paired with one of these is self-contradiction, not a
# near-miss quote. Matched as a prefix: the observed injection returned
# "not found (candidate has only 1 year of experience...)".
NON_SUBSTANTIVE = (
    "not found",
    "not mentioned",
    "not specified",
    "not stated",
    "not present",
    "not applicable",
    "no evidence",
    "none",
    "n a",
    "unknown",
    "absent",
)


@dataclass
class VerificationResult:
    criteria: list[ScoredCriterion]
    flags: list[Flag] = field(default_factory=list)
    scoreable: bool = True
    review_required: bool = False


@dataclass(frozen=True)
class Alignment:
    """What one quote-against-document comparison produced.

    ``blocks`` is the part that is new in v6 and the part a reviewer can use:
    the ratio says *how much* of the quote was found, the blocks say *which
    part*. Persisted as character offsets (12.6) so the UI highlights without
    re-tokenizing.
    """

    ratio: float
    longest_span: int
    matched_chars: int
    blocks: list[MatchBlock] = field(default_factory=list)


def tokenize_with_offsets(text: str) -> tuple[list[str], list[tuple[int, int]]]:
    """Tokens plus each token's character span in the **original** string.

    One tokenizer, used for both matching and highlighting. Two would be two
    things that must agree forever, and the symptom of them disagreeing is a
    highlight off by one word — which looks like a rendering quirk rather than a
    bug and would never be reported.

    Tokens rather than characters for the alignment itself: at character
    granularity a resume-length ``b`` sequence makes almost every letter a
    repeated element and the alignment stops meaning anything.

    Soft hyphens are dropped without splitting the token — a line-broken
    ``co\xadoperate`` is one word, and treating it as two loses the match that a
    reviewer can plainly see is there.
    """
    tokens: list[str] = []
    offsets: list[tuple[int, int]] = []
    current: list[str] = []
    start = 0

    for index, character in enumerate(text):
        if character == _SOFT_HYPHEN:
            continue
        if _WORD.match(character):
            if not current:
                start = index
            current.append(character.casefold())
            continue
        if current:
            tokens.append("".join(current))
            offsets.append((start, index))
            current = []

    if current:
        tokens.append("".join(current))
        offsets.append((start, len(text)))

    return tokens, offsets


def normalize_tokens(text: str) -> list[str]:
    """The tokens alone, for callers that do not need offsets."""
    return tokenize_with_offsets(text)[0]


def is_non_substantive(evidence: str) -> bool:
    normalized = " ".join(normalize_tokens(evidence))
    if not normalized:
        return True
    return normalized.startswith(NON_SUBSTANTIVE)


def align(evidence: str, document: str) -> Alignment:
    """Align a quote against a document: how much matched, and which parts.

    ``autojunk=False`` is mandatory. With it enabled, any element appearing in
    ``b`` more than 1% of the time is treated as junk once ``len(b) >= 200`` —
    against a resume that discards most common words and silently collapses the
    ratio.

    Blocks shorter than ``evidence_min_block_tokens`` are dropped before summing.
    **That filter is load-bearing.** ``get_matching_blocks()`` returns every block
    down to size 1, so an unfiltered sum counts scattered stopword hits and
    evidence built largely from common words approaches ratio 1.0 against *any*
    resume — fabrication passing verification by a different route than the
    single-longest-match hole it replaced.

    Blocks are monotonically increasing in both sequences, so this stays an
    *alignment*: ordering is what distinguishes a quotation from a word cloud,
    which is why token-set overlap and Jaccard were rejected.
    """
    ev, ev_offsets = tokenize_with_offsets(evidence)
    doc, doc_offsets = tokenize_with_offsets(document)
    if not ev:
        return Alignment(ratio=0.0, longest_span=0, matched_chars=0)

    matcher = difflib.SequenceMatcher(None, ev, doc, autojunk=False)
    min_block = settings.evidence_min_block_tokens
    blocks = [b for b in matcher.get_matching_blocks() if b.size >= min_block]

    matched_tokens = sum(b.size for b in blocks)
    longest_span = max((b.size for b in blocks), default=0)
    matched_chars = sum(len(t) for b in blocks for t in ev[b.a : b.a + b.size])

    return Alignment(
        ratio=matched_tokens / len(ev),
        longest_span=longest_span,
        matched_chars=matched_chars,
        blocks=[
            MatchBlock(
                ev_start=ev_offsets[b.a][0],
                ev_end=ev_offsets[b.a + b.size - 1][1],
                doc_start=doc_offsets[b.b][0],
                doc_end=doc_offsets[b.b + b.size - 1][1],
            )
            for b in blocks
        ],
    )


# Words that carry no topic. Deliberately short: this list is subtracted from the
# *criterion*, so anything left out only makes the relevance check stricter.
_TOPIC_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "in",
        "on",
        "of",
        "to",
        "or",
        "and",
        "with",
        "for",
        "at",
        "by",
        "from",
        "is",
        "are",
        "be",
        "as",
        "that",
        "this",
        "it",
        "its",
        "has",
        "have",
        "using",
        "used",
        "experience",
        "experienced",
        "strong",
        "ability",
        "able",
        "including",
        "such",
        "other",
        "some",
        "any",
    }
)


def evidence_mentions_criterion(criterion_text: str, evidence: str) -> bool:
    """Does the quote mention what the criterion actually asks about?

    **The gap this closes.** `align()` answers "did this text appear in the
    document" and nothing else. It cannot tell a supporting quote from a real,
    verbatim, correctly-copied sentence about something entirely different — and
    that failure passes every other check in 10.5.

    Measured on this system: the model returned `strong` for **"Rust in
    production"** on a resume containing no Rust, evidenced by
    *"Led the migration of a payments monolith to microservices in Go"*. The
    quote is genuine, so it verified at `match_ratio = 1.00`; the consistency
    gate saw substantive evidence and passed it; `validate_verdicts` saw a
    correct id set and passed it. A wrong verdict then went into the arithmetic
    with nothing anywhere reporting a problem.

    The check is deliberately crude — one content word in common. It is looking
    for evidence that is *about something else entirely*, not grading how well
    the quote supports the claim, which is the judgement we are asking the model
    to make in the first place.

    **Synonyms will trip it**: a criterion saying "container orchestration"
    evidenced by "Kubernetes platform" shares no word. That is why a failure
    escalates to a human and never moves a verdict — same reasoning as 10.5(b).
    """
    topic = {t for t in normalize_tokens(criterion_text) if t not in _TOPIC_STOPWORDS}
    if not topic:
        # A criterion made entirely of stopwords tells us nothing to look for.
        return True

    quoted = set(normalize_tokens(evidence))
    return any(_same_word(t, q) for t in topic for q in quoted)


def _same_word(a: str, b: str) -> bool:
    """Exact match, or a shared prefix long enough to be the same word.

    Crude stemming, and necessary: a criterion saying "backend **engineering**"
    evidenced by "backend **engineer**" is obviously about the same thing, and an
    exact-match check would escalate it. Measured against the test corpus, that
    inflection difference alone accounted for several false positives.

    The prefix floor keeps short words exact — `go` must not match `golang`'s
    neighbours by accident, and two-letter overlaps carry no meaning.
    """
    if a == b:
        return True
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    return len(shorter) >= _STEM_FLOOR and longer.startswith(shorter)


# Below this, require an exact match. Four characters is enough to make a prefix
# meaningful without letting `go` match `google`.
_STEM_FLOOR = 4


def _is_verified(alignment: Alignment) -> bool:
    """All three conditions, AND-ed.

    An earlier draft used ``ratio OR 25 chars``, which let a single short
    fragment verify an otherwise fabricated quote.
    """
    return (
        alignment.ratio >= settings.evidence_match_ratio
        and alignment.longest_span >= settings.evidence_min_block_tokens
        and alignment.matched_chars >= settings.evidence_match_min_chars
    )


def quote_verifies(evidence: str, document: str) -> bool:
    """Does this quote survive stage B against this document?

    Public because phase 2 needs it: the verifier's own "I found evidence for a
    criterion you called absent" is put through exactly this check before it is
    allowed to escalate anything (10.6 B). Without that, one unverifiable
    assertion overrides another and the second model's hallucinations are treated
    as corrections of the first's.
    """
    return not is_non_substantive(evidence) and _is_verified(align(evidence, document))


def verify_evidence(output: JudgeOutput, sent_text: str, rubric: Rubric) -> VerificationResult:
    """Check every verdict's evidence against the text the model was actually given.

    ``sent_text`` must be the exact sanitized, post-redaction string sent to the
    model. Matching the pre-redaction original fails on every quote sitting near
    a redaction.
    """
    scored: list[ScoredCriterion] = []
    flags: set[Flag] = set()
    scoreable = True
    review_required = False

    returned: dict[str, CriterionVerdict] = {c.id: c for c in output.criteria}

    for criterion in rubric.criteria:
        cv = returned.get(criterion.id)
        if cv is None:
            # validate_verdicts (10.3) owns this case and will have flagged the
            # candidate already; scoring the gap as absence would inflate nothing
            # here but must never look like a real judgment.
            continue

        alignment = align(cv.evidence, sent_text)
        verified = _is_verified(alignment)
        verdict = cv.verdict

        if cv.verdict != "none" and is_non_substantive(cv.evidence):
            # (a) Self-contradiction. Safe to automate.
            verdict = "none"
            verified = False
            flags.add(Flag.EVIDENCE_CONTRADICTS)
            review_required = True
        elif cv.verdict != "none" and not verified:
            # (b) Cannot confirm the quote. Escalate; do not touch the verdict.
            flags.add(Flag.EVIDENCE_UNVERIFIED)
            scoreable = False
            review_required = True
        elif cv.verdict != "none" and not evidence_mentions_criterion(criterion.text, cv.evidence):
            # (c) The quote is real and correctly copied, but shares no wording
            # with the criterion. Flags for review; **does not unrank**.
            #
            # Measured on a live run with an LLM-extracted rubric: this removed
            # the two strongest candidates from the ranking. The criterion read
            # "Has built production backend services for at least five years"
            # and the evidence read "Led the migration of a payments monolith to
            # microservices in Go" — the right evidence, sharing no word with a
            # generic criterion. The check penalises *concrete* evidence and
            # rewards evidence that parrots the criterion's vocabulary, which is
            # backwards.
            #
            # It still catches the case it was built for (`strong` on "Rust in
            # production" evidenced by a sentence about Go), so it is kept — but
            # a lexical heuristic that misfires this often on real rubrics has
            # not earned the power to take someone out of the ranking. A
            # reviewer sees the flag; the candidate keeps their place.
            flags.add(Flag.EVIDENCE_IRRELEVANT)
            review_required = True

        scored.append(
            ScoredCriterion(
                id=criterion.id,
                verdict=verdict,
                model_verdict=cv.verdict,
                evidence=cv.evidence,
                verified=verified,
                match_ratio=round(alignment.ratio, 4),
                longest_span=alignment.longest_span,
                # Kept even when the quote failed to verify — "which part of the
                # quote *was* in the resume" is precisely the reviewer's
                # question about an unverified quote (15.3).
                match_blocks=alignment.blocks,
                weight=criterion.weight,
                must_have=criterion.must_have,
            )
        )

    return VerificationResult(
        criteria=scored,
        flags=sorted(flags),
        scoreable=scoreable,
        review_required=review_required,
    )
