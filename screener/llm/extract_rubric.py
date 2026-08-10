"""Job description → draft rubric (spec 9.1). The front of the cycle.

This is where a requisition becomes something the screener can measure against,
and it is the **only LLM call outside the worker** — one synchronous ~5 s request
made by `service.py` while a human waits, not a batch (14).

**Everything here is a draft.** The output is edited and explicitly approved by a
reviewer before any candidate is judged against it (23), and the approval is
recorded. That review step is what makes an LLM acceptable at this position at
all: a hallucinated requirement in a rubric would otherwise silently reject every
applicant who lacks something the job never asked for, and unlike a bad verdict
on one resume it would do so across the whole run without leaving a trace in any
individual result.

**Ids are assigned here, in Python.** The model proposes text, weights, and
must-have flags; `C1..Cn` are stamped on afterwards. If the model named the ids,
then the ids in the rubric and the ids it is later asked to return would come
from the same unreliable source, and 10.3's set-equality check would be
validating the model against itself.
"""

from dataclasses import dataclass

from config.settings import settings
from screener.clients.ollama_client import parse_or_raise
from screener.llm import load_prompt_with_hash
from screener.models import Criterion, ExtractedRubric
from screener.ports import LLMClient

PROMPT_NAME = "extract_rubric"

JD_OPEN = "<<<JOB_DESCRIPTION"
JD_CLOSE = "JOB_DESCRIPTION>>>"


@dataclass(frozen=True)
class ExtractionResult:
    criteria: list[Criterion]
    prompt_hash: str

    @property
    def must_have_count(self) -> int:
        return sum(1 for c in self.criteria if c.must_have)


def assign_ids(extracted: ExtractedRubric) -> list[Criterion]:
    """Stamp ``C1..Cn`` in order. The only place criterion ids are minted."""
    return [
        Criterion(
            id=f"C{index}",
            text=item.text.strip(),
            claim=item.claim.strip(),
            must_have=item.must_have,
            weight=item.weight,
        )
        for index, item in enumerate(extracted.criteria, start=1)
    ]


def build_user_message(jd_text: str) -> str:
    """The job description, delimited.

    A job description is written by the hiring side, so it is far less hostile
    than a resume — but it is still text from outside this program, and it is
    pasted into a form by a person who may have copied it from anywhere.
    """
    return f"{JD_OPEN}\n{jd_text}\n{JD_CLOSE}"


def extract_rubric(client: LLMClient, jd_text: str) -> ExtractionResult:
    """Propose 4–12 criteria from a job description.

    The 4–12 bound is enforced by `ExtractedRubric`, so an out-of-range response
    raises `SchemaInvalidError` rather than producing a rubric the `Rubric`
    contract will reject later, further from the cause. The cap lives in the
    contract and not "in the editor" precisely because this path is an LLM.
    """
    system, digest = load_prompt_with_hash(PROMPT_NAME)
    schema = ExtractedRubric.model_json_schema()

    payload = client.chat_json(settings.judge_model, system, build_user_message(jd_text), schema)
    extracted = parse_or_raise(ExtractedRubric, payload)

    return ExtractionResult(criteria=assign_ids(extracted), prompt_hash=digest)


def extract_prompt_hash() -> str:
    _, digest = load_prompt_with_hash(PROMPT_NAME)
    return digest
