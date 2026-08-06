"""Ollama adapter (spec §11, build gate §20 step 7).

Two suites. The default one drives the client against a fake and covers every
error path — those are the branches that must never produce a score, and they
cannot be provoked reliably against a live server.

The `live` suite resolves the step 7 `[assert]`: `count_tokens` exact via
`prompt_eval_count`. It is skipped when Ollama is unreachable, so the gates stay
runnable on a machine without a GPU. Run it with `-m live`.
"""

from typing import Any

import pytest

from config.settings import settings
from screener.clients.ollama_client import (
    BudgetBugError,
    LLMError,
    OllamaClient,
    SchemaInvalidError,
    parse_or_raise,
)
from screener.models import JudgeOutput

SCHEMA: dict[str, Any] = JudgeOutput.model_json_schema()
GOOD = '{"criteria": [{"id": "C1", "verdict": "strong", "evidence": "7 years backend"}]}'


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def model_dump(self) -> dict[str, Any]:
        return self._payload


def chat_payload(
    content: str = GOOD, *, prompt_tokens: int = 500, output_tokens: int = 40
) -> dict[str, Any]:
    return {
        "message": {"content": content},
        "prompt_eval_count": prompt_tokens,
        "eval_count": output_tokens,
    }


class FakeOllama:
    """Records calls and replays scripted responses or raises scripted errors."""

    def __init__(self, *responses: dict[str, Any] | Exception) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []
        self.list_raises: Exception | None = None

    def _next(self, kind: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"kind": kind, **kwargs})
        item = self._responses.pop(0) if self._responses else chat_payload()
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item)

    def chat(self, **kwargs: Any) -> FakeResponse:
        return self._next("chat", **kwargs)

    def generate(self, **kwargs: Any) -> FakeResponse:
        return self._next("generate", **kwargs)

    def list(self) -> dict[str, Any]:
        self.calls.append({"kind": "list"})
        if self.list_raises is not None:
            raise self.list_raises
        # Shaped like Ollama 0.32.4: the digest is here, not on `show()`.
        return {
            "models": [
                {"model": "other-model:1b", "digest": "sha256:wrong"},
                {"model": settings.chat_model, "digest": "sha256:abc123"},
            ]
        }


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Retry timing is not under test and would make the suite sleep for seconds."""
    monkeypatch.setattr("screener.clients.ollama_client.time.sleep", lambda _: None)


def client(*responses: dict[str, Any] | Exception) -> tuple[OllamaClient, FakeOllama]:
    fake = FakeOllama(*responses)
    return OllamaClient(client=fake), fake  # type: ignore[arg-type]


# --- the happy path ----------------------------------------------------------


def test_structured_call_returns_parsed_json() -> None:
    llm, _ = client(chat_payload())

    result = llm.chat_structured("system", "resume", SCHEMA)

    assert result.content["criteria"][0]["verdict"] == "strong"
    assert result.prompt_tokens == 500


def test_decoding_options_are_fixed_by_settings() -> None:
    """Not per-call. They are part of the cache key and the reproducibility claim.

    A caller that could vary `temperature` per request would silently invalidate
    every stored comparison across the boundary.
    """
    llm, fake = client(chat_payload())
    llm.chat_json("system", "resume", SCHEMA)

    options = fake.calls[0]["options"]
    assert options["temperature"] == settings.temperature
    assert options["top_k"] == settings.top_k
    assert options["seed"] == settings.seed
    assert options["num_ctx"] == settings.num_ctx
    assert options["num_predict"] == settings.num_predict


def test_the_schema_is_sent_as_a_grammar_constraint() -> None:
    llm, fake = client(chat_payload())
    llm.chat_json("system", "resume", SCHEMA)

    assert fake.calls[0]["format"] == SCHEMA


# --- no failure path produces a score ---------------------------------------


def test_transient_failure_retries_then_raises() -> None:
    llm, fake = client(
        TimeoutError("connection timed out"),
        TimeoutError("connection timed out"),
        TimeoutError("connection timed out"),
    )

    with pytest.raises(LLMError):
        llm.chat_json("system", "resume", SCHEMA)

    assert len(fake.calls) == settings.max_retries + 1


def test_transient_failure_recovers_on_retry() -> None:
    llm, _ = client(TimeoutError("blip"), chat_payload())

    assert llm.chat_json("system", "resume", SCHEMA)["criteria"]


def test_malformed_output_retries_once_then_raises() -> None:
    llm, fake = client(chat_payload("{not json"), chat_payload("{still not json"))

    with pytest.raises(SchemaInvalidError):
        llm.chat_json("system", "resume", SCHEMA)

    assert len(fake.calls) == 2  # once, not to max_retries


def test_truncated_output_is_not_retried_blindly() -> None:
    """Output hitting `num_predict` cuts the JSON mid-object.

    Retrying reproduces the same overflow at the same cost; `num_predict` is the
    thing to change, so the cause is surfaced instead.
    """
    llm, fake = client(
        chat_payload('{"criteria": [{"id": "C1", "verd', output_tokens=settings.num_predict)
    )

    with pytest.raises(SchemaInvalidError) as excinfo:
        llm.chat_json("system", "resume", SCHEMA)

    assert excinfo.value.truncated is True
    assert "num_predict" in str(excinfo.value)
    assert len(fake.calls) == 1


def test_prompt_over_the_context_limit_is_a_budget_bug() -> None:
    """The pre-check was supposed to make this impossible.

    Raised rather than logged: the model returns a confident verdict on a
    truncated prompt, so a candidate judged this way must not be scored.
    """
    llm, _ = client(chat_payload(prompt_tokens=settings.num_ctx))

    with pytest.raises(BudgetBugError):
        llm.chat_json("system", "resume", SCHEMA)


def test_non_object_json_is_rejected() -> None:
    llm, _ = client(chat_payload("[1, 2, 3]"))

    with pytest.raises(SchemaInvalidError):
        llm.chat_json("system", "resume", SCHEMA)


# --- provenance --------------------------------------------------------------


def test_digest_is_read_from_the_listing_and_cached() -> None:
    """`show()` carries no digest on Ollama 0.32.4.

    Reading it there returns empty and the provenance column fills with blanks
    while everything appears to work.
    """
    llm, fake = client()

    assert llm.model_digest == "sha256:abc123"
    assert llm.model_digest == "sha256:abc123"
    assert sum(1 for c in fake.calls if c["kind"] == "list") == 1


def test_digest_matches_the_configured_model_not_the_first_entry() -> None:
    llm, _ = client()

    assert llm.model_digest != "sha256:wrong"


def test_digest_pin_mismatch_is_reported_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    """The operator needs the run marked non-reproducible more than a dead process."""
    monkeypatch.setattr(settings, "model_digest_pin", "sha256:different")
    llm, _ = client()

    assert llm.check_digest_pin() is False


def test_no_pin_configured_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "model_digest_pin", None)
    llm, _ = client()

    assert llm.check_digest_pin() is True


def test_health_is_true_when_the_server_answers() -> None:
    llm, _ = client()

    assert llm.health() is True


def test_health_is_false_rather_than_raising() -> None:
    """A health check that raises is not a health check.

    `/ready` and the worker's startup gate both call this; an exception there
    takes down the thing that was supposed to report the problem.
    """
    llm, fake = client()
    fake.list_raises = ConnectionError("connection refused")

    assert llm.health() is False


# --- token counting ----------------------------------------------------------


def test_count_tokens_uses_num_predict_one() -> None:
    """Not `num_predict=0`.

    On Ollama 0.32.4 that value is not honoured and the request generates to
    completion — the right count for 50x the price. See `count_tokens`.
    """
    llm, fake = client({"prompt_eval_count": 823})

    assert llm.count_tokens("resume text") == 823
    assert fake.calls[0]["options"]["num_predict"] == 1


def test_empty_text_costs_no_call() -> None:
    llm, fake = client()

    assert llm.count_tokens("") == 0
    assert fake.calls == []


def test_missing_count_is_a_transient_error() -> None:
    llm, _ = client({"prompt_eval_count": None})

    with pytest.raises(LLMError):
        llm.count_tokens("resume text")


def test_prompt_count_measures_the_real_two_message_shape() -> None:
    """Not the concatenated strings plus a template constant.

    That shortcut was tried and measured wrong against live Ollama: the framing
    is constant but the tokenizer's treatment of the system/user *boundary* is
    not, so a calibrated constant under-counted by a token. Under-counting is
    what lets a prompt pass the budget check and then overflow silently.
    """
    llm, fake = client({"prompt_eval_count": 4028})

    assert llm.count_prompt_tokens("system", "resume") == 4028
    assert fake.calls[0]["kind"] == "chat"
    assert fake.calls[0]["messages"][0]["role"] == "system"
    assert fake.calls[0]["options"]["num_predict"] == 1


def test_prompt_count_costs_a_single_call() -> None:
    llm, fake = client({"prompt_eval_count": 4028})

    llm.count_prompt_tokens("system", "resume")

    assert len(fake.calls) == 1


def test_estimate_is_used_when_exact_counting_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "exact_token_count", False)
    llm, fake = client()

    assert llm.count_tokens("x" * 3500) > 0
    assert fake.calls == []


# --- schema validation -------------------------------------------------------


def test_parse_or_raise_enforces_contract_rules_the_grammar_cannot() -> None:
    """`evidence` is capped at 300 chars by the contract, not by the grammar."""
    payload = {"criteria": [{"id": "C1", "verdict": "strong", "evidence": "x" * 400}]}

    with pytest.raises(SchemaInvalidError):
        parse_or_raise(JudgeOutput, payload)


def test_parse_or_raise_accepts_a_valid_payload() -> None:
    out = parse_or_raise(
        JudgeOutput, {"criteria": [{"id": "C1", "verdict": "none", "evidence": "not found"}]}
    )

    assert out.criteria[0].id == "C1"
