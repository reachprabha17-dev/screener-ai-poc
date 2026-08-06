"""Live Ollama checks (spec 11, build gate 20 step 7 `[assert]`).

Skipped when Ollama is unreachable, so the four build gates stay runnable on a
machine without the model loaded. These are the tests that resolve
**`count_tokens` exact via `prompt_eval_count` [assert→verified]** — the fake in
`test_ollama_client.py` can prove the client calls the right thing, but only the
real server can prove the number is right.

Measured on Ollama 0.32.4 with `granite4.1:8b`.
"""

import pytest

from config.settings import settings
from screener.clients.ollama_client import OllamaClient
from screener.models import JudgeOutput

pytestmark = pytest.mark.live


@pytest.fixture(scope="module")
def llm() -> OllamaClient:
    client = OllamaClient()
    if not client.health():
        pytest.skip("ollama unreachable")
    return client


def test_health_and_digest(llm: OllamaClient) -> None:
    assert llm.health() is True
    assert llm.model_digest  # non-empty, recorded against every decision


def test_token_count_is_exact_and_deterministic(llm: OllamaClient) -> None:
    """The `[assert]`.

    Repeat calls must agree: the count is part of a budget decision, and a
    counter that wobbles turns `BUDGET_EXCEEDED` into a coin flip near the
    boundary.
    """
    text = "Senior Backend Engineer with 7 years of experience in Python and Go."

    counts = {llm.count_tokens(text) for _ in range(3)}

    assert len(counts) == 1
    assert 10 < counts.pop() < 40


def test_token_count_scales_with_input(llm: OllamaClient) -> None:
    short = llm.count_tokens("Asha Nair")
    long = llm.count_tokens("Senior Backend Engineer. Built payment systems. " * 500)

    assert short < long
    assert long > 3000  # a real resume-sized prompt, well past the naive estimate


def test_the_character_heuristic_would_have_under_counted(llm: OllamaClient) -> None:
    """Why the fallback is a fallback.

    Under-counting permits a *silent* context overflow that bypasses
    `BUDGET_EXCEEDED` — the one outcome 10.1 exists to prevent. This records the
    real gap on real text rather than asserting the divisor is fine.
    """
    text = "Python, Go, PostgreSQL, Redis, Kubernetes, Terraform, gRPC, CI/CD. " * 60

    exact = llm.count_tokens(text)
    estimated = OllamaClient.estimate_tokens(text)

    assert exact > 0
    # Not an assertion that the heuristic is safe — it is the measurement that
    # says recalibrate before ever relying on it (10.1).
    assert estimated != exact


def test_prompt_count_exceeds_the_raw_string_count(llm: OllamaClient) -> None:
    """The chat template adds framing the raw strings do not carry.

    This is why `count_prompt_tokens` counts the real two-message shape: a
    pre-flight number below the model's own count lets a prompt pass the budget
    check and then overflow silently.
    """
    system = "You are a resume screener."
    user = "Resume text here. " * 200

    assert llm.count_prompt_tokens(system, user) > llm.count_tokens(f"{system}\n{user}")


def test_structured_output_round_trips_the_pydantic_schema(llm: OllamaClient) -> None:
    """`model_json_schema()` → grammar → parsed back into the same model.

    The `$defs`/`$ref` that `RedFlag` produces are the part worth exercising:
    a schema the decoder cannot compile fails here, not in a batch.
    """
    schema = JudgeOutput.model_json_schema()
    system = (
        "You are a resume screener. Return exactly one verdict object per criterion, "
        "using the ids given. Quote the exact supporting phrase as evidence, or "
        'return verdict "none" with evidence "not found".'
    )
    user = (
        "CRITERIA:\nC1: 5+ years of backend engineering\nC2: Experience with Kubernetes\n\n"
        "RESUME:\nAsha Nair. Senior Backend Engineer with 7 years of experience. "
        "Built payment systems in Python and Go."
    )

    result = llm.chat_structured(system, user, schema)
    out = JudgeOutput.model_validate(result.content)

    assert result.prompt_tokens > 0
    assert result.output_tokens < settings.num_predict  # not truncated
    assert len(out.criteria) >= 1
    assert all(c.verdict in ("strong", "partial", "none") for c in out.criteria)


def test_pre_flight_count_predicts_the_actual_prompt_size(llm: OllamaClient) -> None:
    """The property the whole budget check rests on.

    If the pre-flight number is below the number the model reports, the check can
    pass a prompt that then overflows silently. Equality is the requirement;
    over-counting would be merely wasteful.
    """
    system = "You are a resume screener."
    user = "Senior Backend Engineer with 7 years of experience. " * 40
    schema = JudgeOutput.model_json_schema()

    predicted = llm.count_prompt_tokens(system, user)
    actual = llm.chat_structured(system, user, schema).prompt_tokens

    assert predicted == actual
