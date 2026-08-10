"""Negation detection over verified evidence (spec 10.5 C). Pure — no I/O, no model.

**The hole this closes.** `"no production Kubernetes experience"` contains the
substring `"production Kubernetes experience"`. Quoted, it verifies at
`match_ratio = 1.00` — the quote is real, verbatim, and in the document — while
meaning the exact opposite of what it was offered to support. Stage B cannot
catch this: it is checking provenance, not meaning, and it is right not to.

**This flags and never downgrades.** A window of preceding tokens is a heuristic:
it cannot tell `"no experience with Kubernetes, but strong Docker"` from
`"no Kubernetes"`, and the cost of getting it wrong in the downgrading direction
is an adverse outcome for a candidate produced by a keyword list. So the verdict
is left exactly as it was, the candidate keeps their place in the ranking, and a
human is told to look (1.23).

The weak markers are a different claim from the negation markers — `"familiar
with Kubernetes"` does not deny the experience, it describes it as shallow, which
is a `partial` dressed as a `strong`. Both routes end at the same place: a
reviewer, not a penalty.
"""

from bisect import bisect_right
from dataclasses import dataclass, field

from config.settings import settings
from screener.core.verify_evidence import tokenize_with_offsets
from screener.models import Flag, ScoredCriterion

# Outright denial: the quote is inside a statement that the thing is absent.
NEGATION_MARKERS = frozenset(
    {
        "no",
        "not",
        "never",
        "without",
        "lacking",
        "lacks",
        "none",
        "excluding",
        "besides",
        "unfamiliar",
        "minimal",
        "limited",
    }
)

# Not denial — depth. "Familiar with Kubernetes" is evidence *against* a `strong`
# verdict on a criterion asking for production depth, and evidence *for* a
# `partial`. Kept separate from the markers above because the reviewer's question
# is different, and because a future change may want to treat them differently.
WEAK_MARKERS = frozenset({"familiar", "exposure", "basic", "beginner", "learning"})


@dataclass
class NegationResult:
    criteria: list[ScoredCriterion]
    flags: list[Flag] = field(default_factory=list)
    review_required: bool = False


def _is_marker(token: str, span: tuple[int, int], text: str) -> bool:
    """Is this token a negation marker *as written*?

    The hyphen check is the whole subtlety. Tokenization drops punctuation, so
    `"no-code platform"` arrives here as the tokens `no`, `code`, `platform` and
    is indistinguishable from `"no code platform"` — a product category read as a
    denial. A marker immediately followed by a hyphen in the source is part of a
    compound word, not a negation of what follows it.
    """
    if token not in NEGATION_MARKERS and token not in WEAK_MARKERS:
        return False
    end = span[1]
    return not (end < len(text) and text[end] == "-")


def detect_negation(
    criteria: list[ScoredCriterion], sent_text: str, window: int | None = None
) -> NegationResult:
    """Flag verified evidence whose surrounding context inverts it.

    Runs against `sent_text` for the same reason stage B does: those are the
    coordinates `match_blocks` are expressed in, and looking the tokens up in any
    other version of the text would read the window from the wrong place.

    Only verified criteria are examined. An unverified quote is already
    escalating for a stronger reason, and a window around an offset we do not
    trust is not worth reading.
    """
    span = settings.negation_window_tokens if window is None else window
    tokens, offsets = tokenize_with_offsets(sent_text)
    starts = [start for start, _end in offsets]

    scored: list[ScoredCriterion] = []
    suspected_any = False

    for criterion in criteria:
        suspected = False
        if criterion.verified and criterion.match_blocks:
            for block in criterion.match_blocks:
                # The token index the block opens at: the first token starting
                # at or after the block's offset.
                index = bisect_right(starts, block.doc_start - 1)
                preceding = range(max(0, index - span), index)
                if any(_is_marker(tokens[i], offsets[i], sent_text) for i in preceding):
                    suspected = True
                    break

        suspected_any |= suspected
        scored.append(
            criterion.model_copy(update={"negation_suspected": True}) if suspected else criterion
        )

    return NegationResult(
        criteria=scored,
        flags=[Flag.NEGATION_SUSPECTED] if suspected_any else [],
        review_required=suspected_any,
    )
