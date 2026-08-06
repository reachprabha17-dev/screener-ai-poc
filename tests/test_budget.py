"""Token budget (spec 10.1, build gate 20 step 4).

The gate is "overflow → unscoreable". The tests that matter here are the ones
asserting what does *not* happen: nothing is truncated, and nothing over budget
comes back with a score.
"""

import pytest

from config.settings import settings
from screener.core.budget import (
    BudgetCheck,
    check_budget,
    context_limit,
    drift,
    estimate_tokens,
    exceeds_context,
    resume_fits,
)
from screener.models import Flag


def test_context_limit_reserves_the_output() -> None:
    """Generation shares the window; a prompt sized to num_ctx truncates the JSON."""
    assert context_limit() == settings.num_ctx - settings.num_predict
    assert context_limit() < settings.num_ctx


def test_prompt_within_budget_carries_no_flag() -> None:
    check = check_budget(context_limit() - 100)

    assert check.fits is True
    assert check.flags == []
    assert check.scoreable is True
    assert check.review_required is False
    assert check.headroom == 100


def test_prompt_exactly_at_the_limit_fits() -> None:
    """The limit is inclusive — an off-by-one here rejects valid resumes."""
    check = check_budget(context_limit())

    assert check.fits is True
    assert check.headroom == 0


def test_overflow_is_unscoreable_and_escalates() -> None:
    """The gate. Over budget produces no score, ever.

    Truncating and judging would reproduce at our own boundary the silent
    overflow 10.1 exists to prevent.
    """
    check = check_budget(context_limit() + 1)

    assert check.fits is False
    assert check.flags == [Flag.BUDGET_EXCEEDED]
    assert check.scoreable is False
    assert check.review_required is True
    assert check.headroom == -1


def test_budget_check_reports_and_never_truncates() -> None:
    """There is no path from this module to a shortened prompt.

    A truncating helper is the obvious "fix" for a long CV and it is the bug.
    The check returns a decision; the caller stops.
    """
    check = check_budget(999_999)

    assert isinstance(check, BudgetCheck)
    assert check.prompt_tokens == 999_999  # reported as-is, not clamped to the limit


def test_resume_component_check_matches_the_settings_ceiling() -> None:
    assert resume_fits(settings.max_resume_tokens) is True
    assert resume_fits(settings.max_resume_tokens + 1) is False


def test_resume_component_check_is_not_a_substitute_for_the_prompt_check() -> None:
    """A resume inside its own ceiling can still overflow once assembled.

    The component budget assumes the other three components stay near their
    estimates; the prompt check is what actually holds.
    """
    assert resume_fits(settings.max_resume_tokens) is True
    assert check_budget(settings.num_ctx).fits is False


# --- post-call reconciliation ------------------------------------------------


def test_actual_count_over_the_limit_is_a_bug_not_an_input_property() -> None:
    """The pre-check was supposed to make this impossible.

    A hit means the counter or this module is wrong, and a wrong budget silently
    truncates every subsequent resume — hence `error`, not a candidate flag.
    """
    assert exceeds_context(context_limit() + 1) is True
    assert exceeds_context(context_limit()) is False


def test_drift_is_signed_toward_the_dangerous_direction() -> None:
    """Positive drift means the pre-check under-counted, which is what overflows."""
    assert drift(estimated=1000, actual=1200) == 200
    assert drift(estimated=1200, actual=1000) == -200


# --- the fallback heuristic --------------------------------------------------


def test_estimate_applies_the_safety_margin() -> None:
    text = "x" * 3500
    assert estimate_tokens(text) == int(3500 / 3.5 * settings.token_estimate_safety_margin)


def test_estimate_over_counts_plain_prose() -> None:
    """Over-counting is the safe direction: it escalates, it does not overflow."""
    prose = "Senior backend engineer with seven years of experience. " * 40
    assert estimate_tokens(prose) > len(prose.split())


@pytest.mark.parametrize("text", ["", "a"])
def test_estimate_handles_degenerate_input(text: str) -> None:
    assert estimate_tokens(text) >= 0
