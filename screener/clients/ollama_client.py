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
from screener.logging import get_logger


class LLMError(RuntimeError):
    """Transient infrastructure failure. Retried, then escalated. Never cached."""


class SchemaInvalidError(RuntimeError):
    """The model returned something the schema could not accommodate.

    Carries ``truncated`` because the most common cause is output hitting
    ``num_predict`` and cutting the JSON mid-object — a configuration problem
    wearing a parsing problem's clothes, which is worth naming rather than
    retrying blindly.

    Carries ``raw_output`` for the same reason, one level up: the text that
    failed is the only thing that distinguishes those causes, and the exception
    message alone leaves a `SCHEMA_INVALID` with nothing to diagnose (18). The
    client does not write it to disk itself — it has no `file_sha256` to attribute
    it to, and a capture nobody can tie to a candidate is a capture that cannot be
    purged.
    """

    def __init__(self, message: str, *, truncated: bool = False, raw_output: str = "") -> None:
        super().__init__(message)
        self.truncated = truncated
        self.raw_output = raw_output


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
    """Two models, one host, one set of decoding options.

    The decoding options are fixed rather than passed per call. They are part of
    the cache key and the reproducibility claim (10.9), so a caller that could
    vary `temperature` per request could silently invalidate every stored
    comparison. The **model** is a per-call argument, because v6 runs two of them
    and an implicit default would make it impossible to tell from a call site
    which weights produced a result.
    """

    def __init__(self, client: ollama.Client | None = None) -> None:
        self._client = client or ollama.Client(
            host=settings.ollama_host,
            timeout=settings.request_timeout_s,
        )
        self._digests: dict[str, str] = {}
        self._loaded: str | None = None

    # --- provenance ---------------------------------------------------------

    def digest(self, model: str) -> str:
        """Digest of a model's weights, recorded against every decision.

        Model tags are mutable: re-pulling `granite4.1:8b` can change the weights
        underneath decisions already stored, which breaks both reproducibility
        and the audit record without changing anything visible.

        Read from ``list()``, not ``show()``. On Ollama 0.32.4 the show response
        carries the template, modelfile, license and parameters but **no digest**
        — an attribute lookup there returns empty and the provenance column
        silently fills with blanks.
        """
        if model not in self._digests:
            self._digests[model] = self._lookup_digest(model)
        return self._digests[model]

    def _lookup_digest(self, model: str) -> str:
        listing = self._client.list()
        models = getattr(listing, "models", None)
        if models is None and isinstance(listing, dict):
            models = listing.get("models", [])
        for entry in models or []:
            name = getattr(entry, "model", None) or (
                entry.get("model") if isinstance(entry, dict) else None
            )
            if name == model:
                found = getattr(entry, "digest", None) or (
                    entry.get("digest") if isinstance(entry, dict) else None
                )
                return str(found or "")
        return ""

    def check_digest_pin(self, model: str, pin: str | None) -> bool:
        """True when a model's weights match its pin, or no pin is configured.

        Deliberately not an exception. Refusing to start would be defensible, but
        the operator needs the run's stored decisions marked non-reproducible
        more than they need the process dead.
        """
        return not pin or self.digest(model) == pin

    # --- phase switching (17.4) ---------------------------------------------

    def ensure_loaded(self, model: str) -> None:
        """Make `model` the resident one, unloading the other.

        **Called once per phase, never per resume.** 12 GB of VRAM does not hold
        `granite4.1:8b` (~5–6 GB) and `gemma4:12b` (~8 GB) at once, and swapping
        per candidate costs a 10–20 s load each time — 2,000 loads on a 1,000-CV
        run instead of two.

        Failures to unload are not fatal: Ollama evicts under memory pressure on
        its own, so the worst case is the load that follows being slower.
        """
        if self._loaded == model:
            return
        if self._loaded is not None:
            self.unload(self._loaded)
        try:
            self._client.generate(model=model, prompt="", keep_alive=settings.keep_alive)
        except Exception as exc:  # noqa: BLE001 — the ollama client raises a wide range
            raise LLMError(f"could not load {model}: {exc}") from exc
        self._loaded = model

    def unload(self, model: str) -> None:
        """Evict a model by asking for it with `keep_alive=0`."""
        try:
            self._client.generate(model=model, prompt="", keep_alive=0)
        except Exception as exc:  # noqa: BLE001 — eviction is best-effort
            # Ollama evicts under memory pressure anyway, so a failed unload
            # costs a slower load next, not a broken run. Logged rather than
            # raised: killing a phase transition over a hint is worse.
            get_logger(__name__).warning("unload_failed", model=model, error=str(exc)[:200])
        if self._loaded == model:
            self._loaded = None

    def health(self) -> bool:
        try:
            self._client.list()
        except Exception:  # noqa: BLE001 — a health check that raises is not a health check
            return False
        return True

    # --- token counting -----------------------------------------------------

    def count_tokens(self, model: str, text: str) -> int:
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
                model=model,
                prompt=text,
                options={"num_predict": 1, "num_ctx": self._context_for(model), "temperature": 0},
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

    def count_prompt_tokens(self, model: str, system: str, user: str) -> int:
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
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            options={"num_predict": 1, "num_ctx": self._context_for(model), "temperature": 0},
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

    @staticmethod
    def _context_for(model: str) -> int:
        """The verifier reads a whole resume plus every `none` criterion (10.6 B).

        Keyed on the model rather than passed by the caller: the context size is
        a property of what that model is asked to do, and a caller free to vary
        it could put a resume through a window it does not fit, which Ollama
        truncates silently.
        """
        return settings.verifier_num_ctx if model == settings.verifier_model else settings.num_ctx

    @staticmethod
    def _num_predict_for(model: str) -> int:
        """The verifier batches a judgment per in-scope criterion into one reply.

        Same reasoning as `_context_for`: a property of what the model is asked
        to produce, not something a caller should be free to vary per call.
        """
        return (
            settings.verifier_num_predict if model == settings.verifier_model else settings.num_predict
        )

    def _options(self, model: str) -> dict[str, Any]:
        return {
            "temperature": settings.temperature,
            "top_k": settings.top_k,
            "seed": settings.seed,
            "num_ctx": self._context_for(model),
            "num_predict": self._num_predict_for(model),
        }

    def chat_json(
        self, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        """Protocol entry point. Returns parsed JSON or raises."""
        return self.chat_structured(model, system, user, schema).content

    def chat_structured(
        self, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> ChatResult:
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
                    model=model,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    format=schema,
                    options=self._options(model),
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
            num_predict = self._num_predict_for(model)
            limit = self._context_for(model) - num_predict
            if prompt_tokens > limit:
                raise BudgetBugError(
                    f"prompt_eval_count {prompt_tokens} exceeds num_ctx - num_predict ({limit})"
                )

            content = payload.get("message", {}).get("content", "")
            truncated = output_tokens >= num_predict

            try:
                parsed = json.loads(content)
            except (json.JSONDecodeError, TypeError) as exc:
                last = SchemaInvalidError(
                    f"unparseable model output ({exc})"
                    + (f"; output hit num_predict={num_predict}" if truncated else ""),
                    truncated=truncated,
                    raw_output=content if isinstance(content, str) else repr(content),
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
                model_digest=self.digest(model),
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
        raise SchemaInvalidError(
            f"schema validation failed: {exc}",
            raw_output=json.dumps(payload, ensure_ascii=False, default=str),
        ) from exc
