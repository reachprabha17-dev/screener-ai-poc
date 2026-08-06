"""Prompt assembly, id minting, and the corrective retry (spec 9, build gate 20 step 9).

Driven against a fake client. What is under test here is the *shape* of the
request and the control flow around it — both fully determined by our code. The
model's actual compliance rate is a separate, live measurement
(`test_llm_live.py`), because it is a property of the model rather than of this
module.
"""

from typing import Any

import pytest
from conftest import make_rubric

from screener.clients.ollama_client import SchemaInvalidError
from screener.llm import PROMPT_DIR, PromptNotFoundError, load_prompt, prompt_hash
from screener.llm.extract_rubric import assign_ids, extract_rubric
from screener.llm.judge_resume import (
    RESUME_CLOSE,
    RESUME_OPEN,
    build_user_message,
    judge_prompt_hash,
    judge_resume,
    render_criteria,
)
from screener.models import ExtractedCriterion, ExtractedRubric, Flag

RUBRIC = make_rubric(("C1", True, 3), ("C2", True, 2), ("C3", False, 1), ("C4", False, 1))
RESUME = "Asha Nair. Senior Backend Engineer, 7 years. Python, Go, PostgreSQL."


class FakeLLM:
    """Replays scripted payloads and records the prompts it was given."""

    def __init__(self, *payloads: dict[str, Any]) -> None:
        self._payloads = list(payloads)
        self.calls: list[tuple[str, str]] = []

    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((system, user))
        return self._payloads.pop(0) if self._payloads else {"criteria": []}

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def health(self) -> bool:
        return True

    @property
    def model_digest(self) -> str:
        return "sha256:test"


def verdicts(*ids: str) -> dict[str, Any]:
    return {
        "criteria": [
            {"id": i, "verdict": "partial", "evidence": "Senior Backend Engineer"} for i in ids
        ]
    }


# --- prompts as versioned inputs ---------------------------------------------


def test_both_prompts_exist_on_disk() -> None:
    assert (PROMPT_DIR / "judge_resume.md").is_file()
    assert (PROMPT_DIR / "extract_rubric.md").is_file()


def test_a_missing_prompt_is_fatal() -> None:
    """Never a fallback or an empty string.

    Either would run the batch against instructions nobody wrote, and the output
    would look entirely normal.
    """
    with pytest.raises(PromptNotFoundError):
        load_prompt("no_such_prompt")


def test_prompt_hash_is_stable_and_content_sensitive() -> None:
    """It is a cache-key field. Equal prompts must agree; edited ones must not."""
    text = load_prompt("judge_resume")

    assert prompt_hash(text) == judge_prompt_hash()
    assert prompt_hash(text + " ") != prompt_hash(text)


def test_judge_prompt_carries_its_non_negotiable_clauses() -> None:
    """9.2 lists these as non-negotiable, so their absence is a test failure.

    Not a style check: each clause maps to a downstream control that assumes it
    was said.
    """
    text = load_prompt("judge_resume").casefold()

    assert "not found" in text  # 10.5(a)'s consistency gate depends on this exact string
    assert "instruction_like_text" in text  # 10.2
    assert "300 characters" in text  # 5's evidence cap
    for forbidden in ("gender", "age", "ethnicity", "culture fit", "employment gaps"):
        assert forbidden in text


def test_extract_prompt_forbids_inventing_requirements() -> None:
    text = load_prompt("extract_rubric").casefold()

    assert "must_have" in text
    assert "4 and 12" in text or "4–12" in text
    assert "invent" in text


# --- the judging request -----------------------------------------------------


def test_criteria_block_omits_weight_and_must_have() -> None:
    """Scoring policy is withheld from the model deliberately.

    A model told a criterion is mandatory and heavy-weighted has a reason to be
    generous about it. It judges the resume; `compute_score` applies policy.
    """
    block = render_criteria(RUBRIC)

    assert "C1: Backend engineering experience" in block
    assert "must_have" not in block
    assert "weight" not in block
    assert "3" not in block.replace("C3", "")  # no weights leaked


def test_resume_is_delimited_and_comes_last() -> None:
    """Nothing trusted follows the untrusted block for injected text to override."""
    message = build_user_message(RESUME, RUBRIC)

    assert message.index("CRITERIA:") < message.index(RESUME_OPEN)
    assert message.rstrip().endswith(RESUME_CLOSE)
    assert RESUME in message


def test_every_rubric_id_reaches_the_prompt() -> None:
    message = build_user_message(RESUME, RUBRIC)

    for criterion in RUBRIC.criteria:
        assert criterion.id in message


# --- the corrective retry ----------------------------------------------------


def test_a_compliant_response_is_not_retried() -> None:
    client = FakeLLM(verdicts("C1", "C2", "C3", "C4"))

    result = judge_resume(client, RESUME, RUBRIC)

    assert result.ok is True
    assert result.attempts == 1
    assert result.retried is False
    assert result.flags == []


def test_a_mismatch_retries_once_and_can_recover() -> None:
    client = FakeLLM(verdicts("C1", "C2"), verdicts("C1", "C2", "C3", "C4"))

    result = judge_resume(client, RESUME, RUBRIC)

    assert result.ok is True
    assert result.attempts == 2
    assert result.retried is True


def test_the_retry_names_the_missing_ids() -> None:
    """Restating the rule is the one approach known not to work — it just failed."""
    client = FakeLLM(verdicts("C1", "C2"), verdicts("C1", "C2", "C3", "C4"))

    judge_resume(client, RESUME, RUBRIC)
    _, retry_user = client.calls[1]

    assert "C3" in retry_user
    assert "C4" in retry_user
    assert "previous response" in retry_user


def test_two_mismatches_escalate_and_never_return_a_subset() -> None:
    """A best-effort partial set would shrink the denominator and inflate the score.

    10.3's whole point: the failure has to reach a human rather than be scored
    around.
    """
    client = FakeLLM(verdicts("C1"), verdicts("C1", "C2"))

    result = judge_resume(client, RESUME, RUBRIC)

    assert result.ok is False
    assert result.flags == [Flag.VERDICT_SET_MISMATCH]
    assert result.check.scoreable is False
    assert result.check.review_required is True


def test_duplicate_ids_trigger_the_retry() -> None:
    """The inflating case — one verdict counted twice against a fixed denominator."""
    client = FakeLLM(verdicts("C1", "C1", "C2", "C3", "C4"), verdicts("C1", "C2", "C3", "C4"))

    result = judge_resume(client, RESUME, RUBRIC)

    assert result.ok is True
    assert result.retried is True


def test_schema_violations_raise_rather_than_score() -> None:
    """Contract rules the grammar cannot carry — here, the 300-char evidence cap."""
    client = FakeLLM({"criteria": [{"id": "C1", "verdict": "strong", "evidence": "x" * 400}]})

    with pytest.raises(SchemaInvalidError):
        judge_resume(client, RESUME, RUBRIC)


# --- rubric extraction -------------------------------------------------------


def test_ids_are_minted_in_python_not_by_the_model() -> None:
    """If the model named them, 10.3 would be validating it against itself."""
    extracted = ExtractedRubric(
        criteria=[
            ExtractedCriterion(text="5+ years backend", must_have=True, weight=5),
            ExtractedCriterion(text="Kubernetes in production", weight=3),
            ExtractedCriterion(text="Go or Rust", weight=2),
            ExtractedCriterion(text="Mentoring experience", weight=1),
        ]
    )

    criteria = assign_ids(extracted)

    assert [c.id for c in criteria] == ["C1", "C2", "C3", "C4"]
    assert criteria[0].must_have is True
    assert criteria[0].weight == 5


def test_extraction_returns_criteria_and_the_prompt_hash() -> None:
    client = FakeLLM(
        {
            "criteria": [
                {"text": "5+ years backend", "must_have": True, "weight": 5},
                {"text": "Kubernetes", "must_have": False, "weight": 3},
                {"text": "Go", "must_have": False, "weight": 2},
                {"text": "Mentoring", "must_have": False, "weight": 1},
            ]
        }
    )

    result = extract_rubric(client, "We need a senior backend engineer.")

    assert len(result.criteria) == 4
    assert result.must_have_count == 1
    assert len(result.prompt_hash) == 64


def test_too_few_criteria_is_rejected_at_the_contract() -> None:
    """The 4–12 cap lives in the contract, not "in the editor" — this path is an LLM."""
    client = FakeLLM({"criteria": [{"text": "Only one", "must_have": False, "weight": 1}]})

    with pytest.raises(SchemaInvalidError):
        extract_rubric(client, "A thin job description.")


def test_too_many_criteria_is_rejected_at_the_contract() -> None:
    client = FakeLLM(
        {"criteria": [{"text": f"C{i}", "must_have": False, "weight": 1} for i in range(13)]}
    )

    with pytest.raises(SchemaInvalidError):
        extract_rubric(client, "A very long job description.")
