"""Resume → per-criterion verdicts (spec 9.2, 10.3). The core LLM call.

This module owns the *shape* of the judging request and nothing else. It does not
score, rank, verify evidence, or decide anything about a candidate — those live
in `core/`, deliberately, where they are pure and testable without a GPU. What
happens here is: assemble a prompt, constrain the decoder to a schema, and hand
back what came out.

**The retry is the one piece of control flow that belongs here.** 10.3 allows
exactly one corrective attempt when the returned criterion ids do not match the
rubric, because the grammar cannot express set membership and the failure
inflates scores. Retrying is only meaningful while the request is still in hand,
so the loop lives with the call rather than in the pipeline.

**The rubric block is built from the rubric, never from the model's memory of
it.** Ids are `C1..Cn`, assigned in Python at extraction time, and they are
restated in full on the retry. That is what makes `validate_verdicts` a real
check rather than the model grading its own paperwork.
"""

from dataclasses import dataclass

from screener.clients.ollama_client import SchemaInvalidError, parse_or_raise
from screener.core.validate_verdicts import (
    VerdictSetCheck,
    corrective_instruction,
    validate_verdicts,
)
from screener.llm import load_prompt_with_hash
from screener.models import Flag, JudgeOutput, Rubric
from screener.ports import LLMClient

PROMPT_NAME = "judge_resume"

# The resume sits inside explicit delimiters that appear nowhere else in the
# prompt, so "where the untrusted data starts and stops" is unambiguous to the
# model. This is scaffolding for the 9.2 clause that names them, not a defence
# on its own — a resume can write these markers too, which is exactly why the
# real controls are the 10.5(a) consistency gate and mandatory human review.
RESUME_OPEN = "<<<RESUME"
RESUME_CLOSE = "RESUME>>>"


@dataclass(frozen=True)
class JudgeResult:
    output: JudgeOutput | None
    check: VerdictSetCheck
    attempts: int
    prompt_tokens: int = 0
    output_tokens: int = 0
    duration_s: float = 0.0
    retried: bool = False

    @property
    def ok(self) -> bool:
        return self.output is not None and self.check.ok

    @property
    def flags(self) -> list[Flag]:
        return [] if self.ok else list(self.check.flags)


def render_criteria(rubric: Rubric) -> str:
    """The criteria block, one line per criterion.

    Weights and must-have status are deliberately **not** shown. They are
    scoring policy, and a model that knows a criterion is mandatory and
    heavy-weighted has a reason to be generous about it. It judges each
    criterion on the resume alone; `compute_score` applies the policy afterwards.
    """
    return "\n".join(f"{c.id}: {c.text}" for c in rubric.criteria)


def build_user_message(resume_text: str, rubric: Rubric) -> str:
    """Criteria first, then the untrusted block, which runs to the end.

    Ordering is a small hardening choice: instructions the model needs are ahead
    of attacker-controlled text, and nothing trusted follows the resume for
    injected content to appear to override.
    """
    return f"CRITERIA:\n{render_criteria(rubric)}\n\n{RESUME_OPEN}\n{resume_text}\n{RESUME_CLOSE}"


def judge_resume(client: LLMClient, resume_text: str, rubric: Rubric) -> JudgeResult:
    """One judging call, plus at most one corrective retry on a verdict-set mismatch.

    Raises `LLMError` / `SchemaInvalidError` from the client on infrastructure or
    parsing failure — both transient, both turned into an unscoreable candidate
    with a flag by the caller. **No path here returns a score**, and no path
    returns a partially-repaired output: a second mismatch produces
    `VERDICT_SET_MISMATCH` and a human, never a best-effort subset.
    """
    system, _ = load_prompt_with_hash(PROMPT_NAME)
    user = build_user_message(resume_text, rubric)
    schema = JudgeOutput.model_json_schema()

    output: JudgeOutput | None = None
    check = validate_verdicts(JudgeOutput(criteria=[]), rubric)
    attempts = 0

    for attempt in range(2):
        attempts = attempt + 1
        payload = client.chat_json(system, user, schema)
        output = parse_or_raise(JudgeOutput, payload)
        check = validate_verdicts(output, rubric)

        if check.ok:
            return JudgeResult(output=output, check=check, attempts=attempts, retried=attempt > 0)

        if attempt == 0:
            # Restate the ids rather than repeat the rule. The rule was already
            # in the system prompt and the model broke it; saying it again
            # louder is the one approach known not to work.
            user = f"{user}\n\n{corrective_instruction(check, rubric)}"

    return JudgeResult(output=output, check=check, attempts=attempts, retried=True)


def judge_prompt_hash() -> str:
    """Recorded per run and part of the cache key (6)."""
    _, digest = load_prompt_with_hash(PROMPT_NAME)
    return digest


__all__ = [
    "JudgeResult",
    "SchemaInvalidError",
    "build_user_message",
    "judge_prompt_hash",
    "judge_resume",
    "render_criteria",
]
