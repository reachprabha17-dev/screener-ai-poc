"""Token budget (spec §10.1). Pure — no I/O, no model.

Ollama truncates at ``num_ctx`` **silently**. Overflow is not an error: the model
judges a partial resume and returns a well-formed, entirely plausible verdict set
about the half of the document it was given. Downstream, that is
indistinguishable from a genuinely weak candidate.

So the budget is checked *before* every judge call, against the exact string that
will be sent, and **over budget never truncates**. Truncating and judging would
reintroduce at our own boundary precisely the failure this section exists to
prevent — the difference being that ours would be documented, which does not make
it better. Over budget produces ``BUDGET_EXCEEDED``, ``scoreable=False`` and a
human review.

Counting itself lives in the LLM client, because the exact count comes from the
weights doing the judging (``prompt_eval_count``). This module owns only the
arithmetic and the decision, which is what keeps it testable without a GPU.

Component estimates behind ``max_resume_tokens`` (§10.1):

===========================  ========
System prompt                 ~600
Rubric block, 12 criteria     ~500
Schema/grammar overhead       ~350
Reserved output               1536
**Remaining for resume**      **~4200**
===========================  ========
"""

from dataclasses import dataclass, field

from config.settings import settings
from screener.models import Flag


@dataclass(frozen=True)
class BudgetCheck:
    """Verdict on whether a prompt can be judged at all."""

    prompt_tokens: int
    limit: int
    fits: bool
    headroom: int
    flags: list[Flag] = field(default_factory=list)

    @property
    def scoreable(self) -> bool:
        return self.fits

    @property
    def review_required(self) -> bool:
        """Over budget is a human's problem, not a zero.

        Nobody drops out of a run because their CV was long.
        """
        return not self.fits


def context_limit() -> int:
    """Tokens available to the prompt: ``num_ctx`` minus the reserved output.

    ``num_predict`` is subtracted rather than trusted to fit in the slack.
    Generation shares the context window, so a prompt sized to ``num_ctx``
    truncates the JSON mid-object and surfaces as a schema error whose real
    cause is invisible.
    """
    return settings.num_ctx - settings.num_predict


def estimate_tokens(text: str) -> int:
    """Character heuristic. **A fallback**, and only after recalibration.

    Exact counting via ``prompt_eval_count`` is the default (``exact_token_count``).
    This exists for the case where the model is unreachable and a rough answer
    beats no answer.

    The failure under-counting permits is a *silent* context overflow that
    bypasses ``BUDGET_EXCEEDED`` — the one outcome this module exists to prevent
    — so the divisor is deliberately paired with a safety margin, giving an
    effective ~2.6 chars/token. BPE tokenizers process code, jargon, non-Latin
    names, tables and bullet glyphs at 2.0–2.5, so even this margin can
    under-count a dense CV. Recalibrate the divisor against 30 real CVs from the
    actual corpus before relying on it.
    """
    return int(len(text) / 3.5 * settings.token_estimate_safety_margin)


def check_budget(prompt_tokens: int) -> BudgetCheck:
    """Decide whether the assembled prompt fits, before it is sent.

    ``prompt_tokens`` must be counted over the full string — system prompt,
    rubric, schema and resume together. Checking the resume alone leaves the
    other three components as unmeasured slack, and slack is exactly what
    overflows.
    """
    limit = context_limit()
    fits = prompt_tokens <= limit
    return BudgetCheck(
        prompt_tokens=prompt_tokens,
        limit=limit,
        fits=fits,
        headroom=limit - prompt_tokens,
        flags=[] if fits else [Flag.BUDGET_EXCEEDED],
    )


def resume_fits(resume_tokens: int) -> bool:
    """Component check against ``max_resume_tokens``, for use before assembly.

    Cheaper than building the full prompt to discover it will not fit, and it
    localises the cause: this says *the resume* is too long, where
    ``check_budget`` only says the prompt is. Never a substitute for
    ``check_budget``.
    """
    return resume_tokens <= settings.max_resume_tokens


def exceeds_context(actual_prompt_tokens: int) -> bool:
    """True when the count the model reported after the call broke the limit.

    Called against ``prompt_eval_count`` from the response. A hit here is a bug
    in this module or in the counter, not a property of the input: the pre-check
    was supposed to make it impossible. Invalidate the candidate and log at
    ``error`` — a wrong budget silently truncates every subsequent resume.
    """
    return actual_prompt_tokens > context_limit()


def drift(estimated: int, actual: int) -> int:
    """``actual - estimated``, logged after every call.

    Positive drift means the pre-check under-counted, which is the direction
    that overflows. Tracking it is how the fallback divisor gets recalibrated
    against real documents instead of assumptions.
    """
    return actual - estimated
