"""Free-text output screening (spec §10.7, build gate §20 step 6).

The premise under test: omitting a sentiment *field* from the schema does not
stop the model putting sentiment into `summary`. These two unconstrained strings
are the entire remaining exposure, and the fixtures below are the phrasings
models actually produce.
"""

from screener.core.screen_freetext import screen_freetext
from screener.models import Flag, JudgeOutput, RedFlag


def judged(summary: str = "", strengths: list[str] | None = None) -> JudgeOutput:
    return JudgeOutput(criteria=[], summary=summary, notable_strengths=strengths or [])


# --- career gaps and tenure --------------------------------------------------


def test_career_gap_commentary_is_stripped() -> None:
    """The specific fairness hazard §5 names.

    Models reliably emit this, and it is a proxy for parental leave and
    disability.
    """
    out = judged(summary="Strong Python background, though there is an employment gap in 2021.")

    screened = screen_freetext(out)

    assert screened.output.summary == ""
    assert screened.flags == [Flag.FREETEXT_SCREENED]
    assert screened.removed[0].category == "career_gap"


def test_job_hopping_commentary_is_stripped() -> None:
    screened = screen_freetext(judged(summary="Capable engineer but a frequent job changer."))

    assert screened.output.summary == ""


# --- other protected categories ----------------------------------------------


def test_demographic_inference_is_stripped() -> None:
    screened = screen_freetext(judged(summary="She has led three backend teams."))

    assert screened.output.summary == ""
    assert screened.removed[0].category == "demographics"


def test_age_language_is_stripped() -> None:
    screened = screen_freetext(judged(summary="An energetic young developer, recent graduate."))

    assert screened.output.summary == ""


def test_family_and_health_language_is_stripped() -> None:
    screened = screen_freetext(judged(summary="Took maternity leave during the last role."))

    assert screened.output.summary == ""
    assert screened.removed[0].category == "family_health"


def test_appearance_language_is_stripped() -> None:
    screened = screen_freetext(judged(summary="Well-groomed and presentable in the photo."))

    assert screened.output.summary == ""


def test_personality_and_culture_fit_are_stripped() -> None:
    screened = screen_freetext(judged(summary="Comes across as a great culture fit for the team."))

    assert screened.output.summary == ""
    assert screened.removed[0].category == "personality"


# --- what survives -----------------------------------------------------------


def test_job_relevant_summary_survives() -> None:
    text = "Seven years of backend engineering, with payment systems at scale in Python and Go."

    screened = screen_freetext(judged(summary=text))

    assert screened.output.summary == text
    assert screened.flags == []
    assert screened.removed == []


# --- field-level behaviour ---------------------------------------------------


def test_summary_is_cleared_entirely_not_edited() -> None:
    """Excising the clause would leave reasoning built on a premise the reviewer
    can no longer see — worse than no summary, because it still reads complete."""
    screened = screen_freetext(
        judged(summary="Excellent Kubernetes depth. He also seems to be a team player.")
    )

    assert screened.output.summary == ""
    assert "Kubernetes" not in screened.output.summary


def test_only_the_offending_strengths_are_dropped() -> None:
    """Unlike `summary`, these are independent items, so the rest survive."""
    screened = screen_freetext(
        judged(
            strengths=[
                "Built payment systems at 40M requests/day",
                "Enthusiastic and passionate about the mission",
                "Led a monolith-to-microservices migration",
            ]
        )
    )

    assert screened.output.notable_strengths == [
        "Built payment systems at 40M requests/day",
        "Led a monolith-to-microservices migration",
    ]
    assert len(screened.removed) == 1
    assert screened.removed[0].field_name == "notable_strengths"


def test_removed_text_is_retained_for_the_audit_log() -> None:
    """The raw value goes to `audit_log`, never to `candidates` (§17).

    It is the evidence for correcting the prompt; storing it on the candidate
    row would re-import the content this function exists to remove.
    """
    original = "Appears to be a mature candidate with a career break."

    screened = screen_freetext(judged(summary=original))

    assert screened.removed[0].value == original
    assert original not in screened.output.summary


def test_scoring_fields_are_untouched() -> None:
    """Neither screened field affects the score, and screening must not either."""
    out = JudgeOutput(
        criteria=[],
        summary="A passionate young engineer.",
        red_flags=[RedFlag.UNVERIFIABLE_CLAIM],
    )

    screened = screen_freetext(out)

    assert screened.output.red_flags == [RedFlag.UNVERIFIABLE_CLAIM]
    assert screened.output.criteria == []


def test_empty_output_is_a_no_op() -> None:
    screened = screen_freetext(judged())

    assert screened.removed == []
    assert screened.flags == []
