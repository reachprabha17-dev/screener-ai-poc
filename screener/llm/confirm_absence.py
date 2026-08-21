"""Stage D(B): is this criterion really absent? (spec 9.4, 10.6 B).

**A `none` verdict is unfalsifiable without a second look.** Every other check in
10.5 examines a quote; a `none` has no quote to examine, so nothing in phase 1
can tell "the resume does not say this" from "the model did not notice". On a
must-have that distinction is the disqualifying outcome, and getting it wrong
drops a qualified person silently.

**This asks the model to prove a negative**, which is the harder direction, so
expect lower reliability than 9.3. It reliably catches the obvious misses — six
years of Python led by a Django platform, judged `none` — and is shakier on
subtle ones. The obvious ones are the cases worth catching.

**Never skipped by `verify_scope`.** Narrowing stage D is the lever for an
unmanageable escalation rate (19.2), but narrowing it here would remove the only
check that exists on the outcome that disqualifies people.

The whole `sent_text` goes in one call, with every `none` criterion, at
`verifier_num_ctx`. Over budget → skipped, flagged, reviewed: never truncated and
answered, which would be the 10.1 failure reintroduced at a different stage.
"""

from dataclasses import dataclass

from config.settings import settings
from screener.clients.ollama_client import parse_or_raise
from screener.llm import load_prompt_with_hash
from screener.llm.judge_resume import RESUME_CLOSE, RESUME_OPEN
from screener.models import AbsenceCheck, Rubric, ScoredCriterion, VerifyOutput
from screener.ports import LLMClient

PROMPT_NAME = "confirm_absence"


@dataclass(frozen=True)
class AbsenceResult:
    checks: list[AbsenceCheck]
    skipped_over_budget: bool = False


def targets(candidate_criteria: list[ScoredCriterion]) -> list[ScoredCriterion]:
    return [c for c in candidate_criteria if c.verdict == "none"]


def build_user_message(criteria: list[ScoredCriterion], rubric: Rubric, sent_text: str) -> str:
    """Criteria first, then the untrusted block, which runs to the end.

    Same ordering as the judging prompt and for the same reason: instructions
    ahead of attacker-controlled text, and nothing trusted after it for injected
    content to appear to override.
    """
    lines = []
    for criterion in criteria:
        source = rubric.by_id(criterion.id)
        lines.append(f"{criterion.id}: {source.text if source else criterion.id}")
    return (
        "CRITERIA JUDGED ABSENT:\n"
        + "\n".join(lines)
        + f"\n\n{RESUME_OPEN}\n{sent_text}\n{RESUME_CLOSE}"
    )


def confirm_absence(
    client: LLMClient,
    criteria: list[ScoredCriterion],
    rubric: Rubric,
    sent_text: str,
) -> AbsenceResult:
    """One batched call over every `none` criterion.

    One call, not one per criterion: the resume is the same document each time,
    so twelve calls would be twelve copies of the whole context for no extra
    information — and at `verifier_num_ctx` that is the most expensive thing this
    system could be made to do.
    """
    if not criteria:
        return AbsenceResult(checks=[])

    system, _ = load_prompt_with_hash(PROMPT_NAME)
    user = build_user_message(criteria, rubric, sent_text)

    # Phase 2 has its own budget (10.1). Over it, the check is skipped and the
    # candidate is flagged — the alternative is Ollama truncating the resume
    # without an error and the model confirming an absence from a document it
    # was only shown half of.
    budget = settings.verifier_num_ctx - settings.verifier_num_predict
    if client.count_prompt_tokens(settings.verifier_model, system, user) > budget:
        return AbsenceResult(checks=[], skipped_over_budget=True)

    schema = VerifyOutput.model_json_schema()
    payload = client.chat_json(settings.verifier_model, system, user, schema)
    parsed = parse_or_raise(VerifyOutput, payload)

    known = {c.id for c in criteria}
    return AbsenceResult(checks=[check for check in parsed.absence_checks if check.id in known])


def absence_prompt_hash() -> str:
    _, digest = load_prompt_with_hash(PROMPT_NAME)
    return digest
