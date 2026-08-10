"""Stage D(A): does the quote actually support the claim? (spec 9.3, 10.6 A).

**The hole this closes.** `"Python listed in skills"` is a real, verbatim,
correctly-copied quote. It verifies at `match_ratio = 1.00` against stage B and
means nothing at all for a criterion about *production Python experience*. Stage
B is checking provenance; it is right not to care what the words mean, and
nothing before this stage does.

**Premise and hypothesis are both bounded.** The model is given the quote plus a
window of `sent_text` around it — not the whole résumé — because the question is
whether *this excerpt* establishes the claim. Given the full document a model
will happily justify the claim from somewhere else entirely, which is the judge's
job over again rather than a second opinion on its answer.

This module builds the request and parses the reply. What the answer *does* to a
candidate is `core/reconcile_judge.py`, which is pure and cannot change a verdict.
"""

from config.settings import settings
from screener.clients.ollama_client import parse_or_raise
from screener.llm import load_prompt_with_hash
from screener.models import Rubric, ScoredCriterion, SupportCheck, VerifyOutput
from screener.ports import LLMClient

PROMPT_NAME = "verify_support"

# Characters of `sent_text` either side of the quote. Enough for the sentence the
# quote sits in and its neighbour — which is where a negation or a "we did not"
# lives — without becoming the whole document.
CONTEXT_CHARS = 400


def in_scope(criterion: ScoredCriterion) -> bool:
    """Which criteria stage D looks at (10.6, scope selection).

    Under `verify_scope="all"` every verified criterion. Under
    `must_have_and_borderline`, the ones where being wrong costs most: a hard
    requirement, or a quote that only just cleared stage B.

    Absence checking is **not** governed by this — see `confirm_absence`.
    """
    if settings.verify_scope == "all":
        return True
    return criterion.must_have or criterion.match_ratio <= settings.borderline_ratio_max


def targets(candidate_criteria: list[ScoredCriterion]) -> list[ScoredCriterion]:
    """Verified criteria in scope, with a quote to check.

    Unverified evidence is skipped: the candidate is already unscoreable and in
    the review queue for a stronger reason, and asking a second model to reason
    about a quote we could not find in the document invites it to confirm one
    that was never there.
    """
    return [c for c in candidate_criteria if c.verified and c.evidence and in_scope(c)]


def excerpt_context(sent_text: str, criterion: ScoredCriterion) -> str:
    """The window of `sent_text` the quote was found in.

    Anchored on the match blocks rather than by searching for the evidence
    string: the quote is usually a paraphrase, so `sent_text.find(evidence)`
    fails on exactly the criteria most worth checking. The blocks are where
    stage B actually aligned it.
    """
    if not criterion.match_blocks:
        return ""
    start = min(b.doc_start for b in criterion.match_blocks)
    end = max(b.doc_end for b in criterion.match_blocks)
    return sent_text[max(0, start - CONTEXT_CHARS) : end + CONTEXT_CHARS]


def build_user_message(criteria: list[ScoredCriterion], rubric: Rubric, sent_text: str) -> str:
    """One block per criterion: id, claim, quote, context."""
    blocks = []
    for criterion in criteria:
        source = rubric.by_id(criterion.id)
        claim = source.claim if source and source.claim else source.text if source else ""
        blocks.append(
            f"ID: {criterion.id}\n"
            f"CLAIM: {claim}\n"
            f"EXCERPT: {criterion.evidence}\n"
            f"CONTEXT: {excerpt_context(sent_text, criterion)}"
        )
    return "\n\n---\n\n".join(blocks)


def verify_support(
    client: LLMClient,
    criteria: list[ScoredCriterion],
    rubric: Rubric,
    sent_text: str,
) -> list[SupportCheck]:
    """One batched call over every in-scope criterion. Returns what came back.

    Batched rather than one call per criterion: the context is the same document
    each time, so twelve calls would be twelve times the prompt for no additional
    information.
    """
    if not criteria:
        return []

    system, _ = load_prompt_with_hash(PROMPT_NAME)
    user = build_user_message(criteria, rubric, sent_text)
    schema = VerifyOutput.model_json_schema()

    payload = client.chat_json(settings.verifier_model, system, user, schema)
    parsed = parse_or_raise(VerifyOutput, payload)

    # Ids the rubric does not contain are dropped here rather than in
    # reconciliation, so a model inventing criteria cannot inflate the count of
    # checks that appear to have run.
    known = {c.id for c in criteria}
    return [check for check in parsed.support_checks if check.id in known]


def support_prompt_hash() -> str:
    _, digest = load_prompt_with_hash(PROMPT_NAME)
    return digest
