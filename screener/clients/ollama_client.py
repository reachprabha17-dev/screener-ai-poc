"""Ollama adapter (spec 11). Satisfies ``ports.LLMClient``.

Structured output is a **grammar constraint**, not a request: `format=<json schema>`
compiles the schema into a llama.cpp grammar, so the decoder cannot emit a token
that would make the output invalid. That removes a whole class of parsing
failure, and it is why nothing here tries to repair malformed JSON.

**The grammar constrains shape, not meaning.** It cannot express "exactly these
eight criterion ids, each once" — see `core/validate_verdicts.py`. Everything
this client guarantees is structural.

**No failure path here ever produces a score.** Every one raises, and the caller
turns it into `scoreable=False` plus a flag. All four of those flags are
transient (12.5): an Ollama timeout cached as a permanent verdict would sideline
a real candidate forever on a network blip.

Token counting notes are in `count_tokens` — the spec's `num_predict=0` recipe
does not do what it claims on the deployed version, and the correction matters
enough to be load-bearing.
"""

import json
import time
from dataclasses import dataclass
from typing import Any

import ollama
from pydantic import ValidationError

from config.settings import settings


class LLMError(RuntimeError):
    """Transient infrastructure failure. Retried, then escalated. Never cached."""


class SchemaInvalidError(RuntimeError):
    """The model returned something the schema could not accommodate.

    Carries ``truncated`` because the most common cause is output hitting
    ``num_predict`` and cutting the JSON mid-object — a configuration problem
    wearing a parsing problem's clothes, which is worth naming rather than
    retrying blindly.
    """

    def __init__(self, message: str, *, truncated: bool = False) -> None:
        super().__init__(message)
        self.truncated = truncated


class BudgetBugError(RuntimeError):
    """The prompt was larger than the context allows *after* the pre-check passed.

    Not a property of the input — the budget check (10.1) was supposed to make
    this impossible. Logged at ``error``: a wrong budget silently truncates every
    resume that follows.
    """


@dataclass(frozen=True)
class ChatResult:
    content: dict[str, Any]
    prompt_tokens: int
    output_tokens: int
    duration_s: float
    model_digest: str


class OllamaClient:
    """One model, one host, one set of decoding options.

    The options are fixed at construction rather than passed per call. They are
    part of the cache key and the reproducibility claim (10.8), so a caller that
    could vary `temperature` per request could silently invalidate every stored
    comparison.
    """

    def __init__(self, client: ollama.Client | None = None) -> None:
        self._client = client or ollama.Client(
            host=settings.ollama_host,
            timeout=settings.request_timeout_s,
        )
        self._digest: str | None = None

    # --- provenance ---------------------------------------------------------

    @property
    def model_digest(self) -> str:
        """Digest of the loaded weights, recorded against every decision.

        Model tags are mutable: re-pulling `granite4.1:8b` can change the weights
        underneath decisions already stored, which breaks both reproducibility
        and the audit record without changing anything visible.

        Read from ``list()``, not ``show()``. On Ollama 0.32.4 the show response
        carries the template, modelfile, license and parameters but **no digest**
        — an attribute lookup there returns empty and the provenance column
        silently fills with blanks.
        """
        if self._digest is None:
            self._digest = self._lookup_digest()
        return self._digest

    def _lookup_digest(self) -> str:
        listing = self._client.list()
        models = getattr(listing, "models", None)
        if models is None and isinstance(listing, dict):
            models = listing.get("models", [])
        for entry in models or []:
            name = getattr(entry, "model", None) or (
                entry.get("model") if isinstance(entry, dict) else None
            )
            if name == settings.judge_model:
                digest = getattr(entry, "digest", None) or (
                    entry.get("digest") if isinstance(entry, dict) else None
                )
                return str(digest or "")
        return ""

    def check_digest_pin(self) -> bool:
        """True when the loaded weights match the pin, or no pin is configured.

        Deliberately not an exception. Refusing to start would be defensible, but
        the operator needs the run's stored decisions marked non-reproducible
        more than they need the process dead.
        """
        pin = settings.judge_digest_pin
        return not pin or self.model_digest == pin

    def health(self) -> bool:
        try:
            self._client.list()
        except Exception:  # noqa: BLE001 — a health check that raises is not a health check
            return False
        return True

    # --- token counting -----------------------------------------------------

    def count_tokens(self, text: str) -> int:
        """Exact prompt tokens, from the weights that will do the judging.

        **Correction to 10.1, measured on Ollama 0.32.4.** The spec's recipe
        passes ``num_predict=0`` and reads ``prompt_eval_count``. On this version
        ``num_predict=0`` is not honoured: the request generates to completion
        (573 tokens, 7.4 s observed) and returns the right count for entirely the
        wrong price. ``num_predict=1`` returns the identical count in 0.15 s and
        is what this uses. Verified deterministic across repeat calls.

        No tokenizer is vendored. A HuggingFace vocabulary a major version behind
        the deployed model is exactly the mismatch this control exists to catch,
        and `prompt_eval_count` is exact by construction (22.2).
        """
        if not settings.exact_token_count:
            return self.estimate_tokens(text)
        if not text:
            return 0

        try:
            response = self._client.generate(
                model=settings.judge_model,
                prompt=text,
                options={"num_predict": 1, "num_ctx": settings.num_ctx, "temperature": 0},
                keep_alive=settings.keep_alive,
            )
        except Exception as exc:  # noqa: BLE001 — every client failure is transient here
            raise LLMError(f"token count failed: {exc}") from exc

        count = response.model_dump().get("prompt_eval_count")
        if count is None:
            raise LLMError("ollama returned no prompt_eval_count")
        return int(count)

    @staticmethod
    def estimate_tokens(text: str) -> int:
        """Calibrated fallback. See `core.budget.estimate_tokens` for the caveats."""
        from screener.core.budget import estimate_tokens

        return estimate_tokens(text)

    def count_prompt_tokens(self, system: str, user: str) -> int:
        """Exact size of the assembled chat prompt, for the 10.1 pre-flight check.

        Counts the **real two-message shape**, not the concatenated strings plus a
        template constant. That shortcut was tried and measured wrong: the
        template's framing is constant, but the tokenizer's treatment of the
        *boundary* between the system and user text is not, so a constant
        calibrated on one pair of strings under-counts another by a token or two.

        Small, and in the direction that matters. Under-counting is what lets a
        prompt pass the budget check and then overflow `num_ctx` silently, which
        is the single outcome 10.1 exists to prevent. Counting the actual shape
        costs the same one prompt-eval and cannot drift.
        """
        response = self._client.chat(
            model=settings.judge_model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options={"num_predict": 1, "num_ctx": settings.num_ctx, "temperature": 0},
            keep_alive=settings.keep_alive,
        )
        count = response.model_dump().get("prompt_eval_count")
        if count is None:
            raise LLMError("ollama returned no prompt_eval_count")
        return int(count)

    # --- the judging call ---------------------------------------------------

    @staticmethod
    def _thinking() -> dict[str, Any]:
        """Turn reasoning off on models that have it.

        A reasoning model spends the generation budget on chain-of-thought
        before emitting any JSON, so the schema-constrained answer never
        arrives. Measured on `gemma4:12b`: `num_predict=1536` consumed entirely
        by `thinking`, `content` empty — indistinguishable from a malformed
        response, and reported as `SCHEMA_INVALID`.

        It also invalidates 10.1's budget, which reserves `num_predict` for
        output. Reasoning is not output; nothing downstream scores it.

        Passed only when enabled, so models without the capability are not sent
        an argument they will reject.
        """
        return {"think": False} if settings.disable_thinking else {}

    def _options(self) -> dict[str, Any]:
        return {
            "temperature": settings.temperature,
            "top_k": settings.top_k,
            "seed": settings.seed,
            "num_ctx": settings.num_ctx,
            "num_predict": settings.num_predict,
        }

    def chat_json(self, system: str, user: str, schema: dict[str, Any]) -> dict[str, Any]:
        """Protocol entry point. Returns parsed JSON or raises."""
        return self.chat_structured(system, user, schema).content

    def chat_structured(self, system: str, user: str, schema: dict[str, Any]) -> ChatResult:
        """One grammar-constrained completion, with the 11 retry policy.

        Timeouts and connection errors retry with backoff to `max_retries`; a
        malformed body retries exactly once, because a second failure against a
        grammar that cannot express invalid JSON says something is wrong with the
        model or the schema, not with this attempt.
        """
        last: Exception | None = None

        for attempt in range(settings.max_retries + 1):
            started = time.monotonic()
            try:
                response = self._client.chat(
                    model=settings.judge_model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    format=schema,
                    options=self._options(),
                    keep_alive=settings.keep_alive,
                    **self._thinking(),
                )
            except Exception as exc:  # noqa: BLE001 — the ollama client raises a wide range
                last = LLMError(f"chat failed: {exc}")
                self._backoff(attempt)
                continue

            payload = response.model_dump()
            duration = round(time.monotonic() - started, 3)
            prompt_tokens = int(payload.get("prompt_eval_count") or 0)
            output_tokens = int(payload.get("eval_count") or 0)

            # 10.1: reconcile against the pre-check. Raised rather than logged
            # because a candidate judged on a truncated prompt must not be
            # scored — the model returns a confident verdict either way.
            limit = settings.num_ctx - settings.num_predict
            if prompt_tokens > limit:
                raise BudgetBugError(
                    f"prompt_eval_count {prompt_tokens} exceeds num_ctx - num_predict ({limit})"
                )

            content = payload.get("message", {}).get("content", "")
            truncated = output_tokens >= settings.num_predict

            try:
                parsed = json.loads(content)
            except (json.JSONDecodeError, TypeError) as exc:
                last = SchemaInvalidError(
                    f"unparseable model output ({exc})"
                    + (f"; output hit num_predict={settings.num_predict}" if truncated else ""),
                    truncated=truncated,
                )
                # A truncation is not retried: the same prompt produces the same
                # overflow, and `num_predict` is the thing to change.
                if truncated or attempt >= 1:
                    raise last from exc
                continue

            if not isinstance(parsed, dict):
                raise SchemaInvalidError(f"expected a JSON object, got {type(parsed).__name__}")

            return ChatResult(
                content=parsed,
                prompt_tokens=prompt_tokens,
                output_tokens=output_tokens,
                duration_s=duration,
                model_digest=self.model_digest,
            )

        raise last or LLMError("chat failed with no recorded cause")

    @staticmethod
    def _backoff(attempt: int) -> None:
        time.sleep(min(2**attempt, 8))


def parse_or_raise[T](model: type[T], payload: dict[str, Any]) -> T:
    """Validate a grammar-constrained payload against its Pydantic model.

    The grammar makes this near-redundant and it is kept anyway: `extra="forbid"`
    and the field constraints (`evidence` max 300 chars, `notable_strengths` max
    5) are contract rules the grammar does not carry, and a silent widening of
    the schema would otherwise reach the scoring code as a loose dict.
    """
    try:
        return model.model_validate(payload)  # type: ignore[attr-defined,no-any-return]
    except ValidationError as exc:
        raise SchemaInvalidError(f"schema validation failed: {exc}") from exc
