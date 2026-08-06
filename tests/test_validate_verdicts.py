"""Verdict set integrity (spec §10.3, build gate §20 step 4).

Gate: missing, extra and duplicate ids are all caught. The inflation cases —
duplicates and invented ids — are the ones with no downstream check, because
nothing about an inflated score looks wrong.
"""

from conftest import make_rubric

from screener.core.validate_verdicts import corrective_instruction, validate_verdicts
from screener.models import CriterionVerdict, Flag, JudgeOutput

RUBRIC = make_rubric(("C1", True, 3), ("C2", True, 2), ("C3", False, 1), ("C4", False, 1))


def judge(*ids: str) -> JudgeOutput:
    return JudgeOutput(
        criteria=[CriterionVerdict(id=i, verdict="partial", evidence="evidence") for i in ids]
    )


def test_exact_match_passes() -> None:
    check = validate_verdicts(judge("C1", "C2", "C3", "C4"), RUBRIC)

    assert check.ok is True
    assert check.flags == []
    assert check.scoreable is True
    assert check.review_required is False


def test_order_is_not_checked() -> None:
    """Every criterion is scored by its own id and weight; order carries nothing."""
    assert validate_verdicts(judge("C4", "C2", "C1", "C3"), RUBRIC).ok is True


def test_missing_id_is_caught() -> None:
    check = validate_verdicts(judge("C1", "C2", "C3"), RUBRIC)

    assert check.ok is False
    assert check.missing == ["C4"]
    assert check.unexpected == []


def test_duplicate_id_is_caught() -> None:
    """The inflating case: one verdict counted twice against a fixed denominator."""
    check = validate_verdicts(judge("C1", "C1", "C2", "C3", "C4"), RUBRIC)

    assert check.ok is False
    assert check.duplicated == ["C1"]
    assert check.missing == []
    assert check.unexpected == []


def test_invented_id_is_caught() -> None:
    """The other inflating case: weight the rubric never granted."""
    check = validate_verdicts(judge("C1", "C2", "C3", "C4", "C9"), RUBRIC)

    assert check.ok is False
    assert check.unexpected == ["C9"]
    assert check.missing == []


def test_swapped_id_reports_both_sides() -> None:
    """A renamed criterion is a miss and an invention at once; a reviewer needs both."""
    check = validate_verdicts(judge("C1", "C2", "C3", "criterion_4"), RUBRIC)

    assert check.missing == ["C4"]
    assert check.unexpected == ["criterion_4"]


def test_empty_response_is_caught() -> None:
    """Schema-valid and completely empty. Without this check it scores 0.0."""
    check = validate_verdicts(JudgeOutput(criteria=[]), RUBRIC)

    assert check.ok is False
    assert check.missing == ["C1", "C2", "C3", "C4"]


def test_terminal_failure_is_unscoreable_and_escalates() -> None:
    """After the corrective retry also fails, no score is produced."""
    check = validate_verdicts(judge("C1"), RUBRIC)

    assert check.flags == [Flag.VERDICT_SET_MISMATCH]
    assert check.scoreable is False
    assert check.review_required is True


# --- diagnosis and retry text ------------------------------------------------


def test_describe_names_every_defect() -> None:
    check = validate_verdicts(judge("C1", "C1", "C2", "C9"), RUBRIC)
    described = check.describe()

    assert "C3" in described and "C4" in described  # missing
    assert "C9" in described  # unexpected
    assert "C1" in described  # duplicated


def test_describe_is_quiet_when_clean() -> None:
    assert validate_verdicts(judge("C1", "C2", "C3", "C4"), RUBRIC).describe() == (
        "verdict set matches the rubric"
    )


def test_corrective_instruction_names_the_required_ids() -> None:
    """Restating the rule verbatim is the one thing known not to work — it already failed."""
    check = validate_verdicts(judge("C1", "C2"), RUBRIC)
    instruction = corrective_instruction(check, RUBRIC)

    for criterion in RUBRIC.criteria:
        assert criterion.id in instruction
    assert "not found" in instruction  # tells it what to do with an unsupported criterion


def test_corrective_instruction_states_what_went_wrong() -> None:
    check = validate_verdicts(judge("C1", "C2", "C3", "C4", "C9"), RUBRIC)

    assert "C9" in corrective_instruction(check, RUBRIC)
