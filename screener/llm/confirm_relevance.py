"""Stage: is a flagged-irrelevant quote actually about the same subject? (spec 10.5 C).

**The hole this closes, and the hole it deliberately does not try to close.**
`verify_evidence`'s relevance check (`core/verify_evidence.py`) is a crude,
same-word-or-stem test — measured on a live run to remove the two strongest
candidates from a ranking, because concrete evidence phrased differently from
the criterion ("migration to microservices in Go" vs. "built production
backend services") shares no word with it. That check is kept anyway, because
it still catches the case it was built for (a `strong` quote about Go used to
support a Rust requirement) — so the fix here is not to replace it, it is to
give it a second, more capable opinion on exactly the cases it flagged, before
that flag ever reaches a reviewer.

**Narrower than `verify_support` on purpose.** This asks one question — is the
excerpt about the same subject as the requirement, in different words? — never
whether the excerpt proves enough depth or seniority. That is a different
question `verify_support` asks (when enabled), over a different, broader
scope. Asking only the narrow question here is what makes a batched call over
a handful of flagged criteria cheap and the model's job simple.

**Asymmetric.** `core/reconcile_relevance.py` is the pure half: it can only
clear `EVIDENCE_IRRELEVANT`, never raise it. A model that hallucinates
relevance costs one candidate a review that should have happened; a model that
hallucinates irrelevance is not a thing this call is ever asked to do.
"""

from config.settings import settings
from screener.clients.ollama_client import parse_or_raise
from screener.llm import load_prompt_with_hash
from screener.llm.verify_support import excerpt_context
from screener.models import RelevanceCheck, RelevanceOutput, Rubric, ScoredCriterion
from screener.ports import LLMClient

PROMPT_NAME = "confirm_relevance"


def targets(candidate_criteria: list[ScoredCriterion]) -> list[ScoredCriterion]:
    """Criteria the crude keyword check flagged — nothing else is asked about."""
    return [c for c in candidate_criteria if c.evidence_irrelevant]


def build_user_message(criteria: list[ScoredCriterion], rubric: Rubric, sent_text: str) -> str:
    """One block per criterion: id, requirement, excerpt, context."""
    blocks = []
    for criterion in criteria:
        source = rubric.by_id(criterion.id)
        text = source.text if source else criterion.text
        blocks.append(
            f"ID: {criterion.id}\n"
            f"REQUIREMENT: {text}\n"
            f"EXCERPT: {criterion.evidence}\n"
            f"CONTEXT: {excerpt_context(sent_text, criterion)}"
        )
    return "\n\n---\n\n".join(blocks)


def confirm_relevance(
    client: LLMClient,
    criteria: list[ScoredCriterion],
    rubric: Rubric,
    sent_text: str,
) -> list[RelevanceCheck]:
    """One batched call over every criterion flagged irrelevant for this candidate.

    Runs in phase 1, against `judge_model` — not a second, differently-weighted
    model, and not a separate phase. It does not need either: it is the same
    resident model answering an additional, narrower question about its own
    output, not an independent opinion the way `verify_support` was designed
    to be (10.6 A's rationale for a second model does not apply here).
    """
    if not criteria:
        return []

    system, _ = load_prompt_with_hash(PROMPT_NAME)
    user = build_user_message(criteria, rubric, sent_text)
    schema = RelevanceOutput.model_json_schema()

    payload = client.chat_json(settings.judge_model, system, user, schema)
    parsed = parse_or_raise(RelevanceOutput, payload)

    known = {c.id for c in criteria}
    return [check for check in parsed.checks if check.id in known]


def relevance_prompt_hash() -> str:
    _, digest = load_prompt_with_hash(PROMPT_NAME)
    return digest
