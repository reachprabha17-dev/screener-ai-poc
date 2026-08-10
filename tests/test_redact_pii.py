"""Input redaction (spec 9, build gate 20 step 6).

The named gate is "date-range preserved through redaction", and it is the test
that matters most here. A redactor that eats four-digit years to catch a birth
year silently converts every duration-based `strong` into a `partial` — a
system-wide downgrade of every candidate, produced by a privacy control,
invisible in the output.
"""

from screener.core.redact_pii import EMAIL_TOKEN, PHONE_TOKEN, REDACTED_TOKEN, redact_pii

# --- the gate ----------------------------------------------------------------


def test_employment_date_ranges_survive_redaction() -> None:
    """The required fixture from 9.

    `strong` requires evidence of depth, scope, or duration. Duration lives in
    exactly these strings.
    """
    text = (
        "2019–2024 Senior Engineer, Acme Payments\n"
        "2015 - 2019 Backend Engineer, Initech\n"
        "Jan 2024 to Present: Staff Engineer\n"
        "03/2019 – 06/2021 Contractor"
    )

    redacted, report, _ = redact_pii(text)

    assert redacted == text
    assert report.any_redacted is False


def test_a_bare_year_is_never_touched() -> None:
    redacted, _, _ = redact_pii("Graduated 2014. Certified 1998.")

    assert "2014" in redacted
    assert "1998" in redacted


def test_date_of_birth_goes_while_the_range_beside_it_stays() -> None:
    """Both in one document, which is the case a year-shaped regex gets wrong."""
    text = "Date of Birth: 12 March 1985\n2019–2024 Senior Engineer"

    redacted, report, _ = redact_pii(text)

    assert "1985" not in redacted
    assert "2019–2024 Senior Engineer" in redacted
    assert report.counts["date_of_birth"] == 1


# --- direct identifiers ------------------------------------------------------


def test_email_is_redacted() -> None:
    redacted, report, _ = redact_pii("Contact: asha.nair+jobs@example.co.uk")

    assert "asha.nair" not in redacted
    assert EMAIL_TOKEN in redacted
    assert report.counts["email"] == 1


def test_phone_is_redacted() -> None:
    for number in ("+44 20 7946 0958", "(020) 7946 0958", "555-123-4567"):
        redacted, _, _ = redact_pii(f"Tel: {number}")
        assert PHONE_TOKEN in redacted, number


def test_scope_evidence_is_not_mistaken_for_a_phone_number() -> None:
    """The numbers in scope claims are exactly what a `strong` verdict rests on.

    A looser phone pattern eats them and takes the evidence with it.
    """
    text = "Handled 40 million requests per day across 12000 transactions in 2019."

    redacted, report, _ = redact_pii(text)

    assert redacted == text
    assert "phone" not in report.counts


# --- protected-attribute header fields ---------------------------------------


def test_labelled_protected_fields_are_removed() -> None:
    """The header block international CV conventions still put at the top."""
    text = (
        "Nationality: Nigerian\n"
        "Marital Status: Married\n"
        "Gender: Female\n"
        "Age: 34\n"
        "Religion: None\n"
        "Skills: Python, Go"
    )

    redacted, report, _ = redact_pii(text)

    for value in ("Nigerian", "Married", "Female", "34"):
        assert value not in redacted
    assert "Skills: Python, Go" in redacted
    assert report.total >= 5
    assert REDACTED_TOKEN in redacted


def test_unlabelled_age_phrase_is_removed() -> None:
    redacted, _, _ = redact_pii("A 34 years old engineer.")

    assert "34 years old" not in redacted


def test_years_of_experience_is_not_mistaken_for_an_age() -> None:
    """`7 years experience` and `34 years old` differ by one word.

    Getting this wrong deletes the duration evidence the rubric asks for.
    """
    text = "Senior Backend Engineer with 7 years of experience."

    redacted, report, _ = redact_pii(text)

    assert redacted == text
    assert report.any_redacted is False


def test_photo_artefact_is_removed() -> None:
    redacted, report, _ = redact_pii("[image: passport_photo_asha.jpg]\nSkills: Python")

    assert "passport_photo" not in redacted
    assert report.counts["photo"] == 1


# --- reporting ---------------------------------------------------------------


def test_report_counts_by_category() -> None:
    redacted, report, _ = redact_pii("a@b.com and c@d.com\nAge: 30")

    assert report.counts["email"] == 2
    assert report.counts["age"] == 1
    assert report.total == 3
    assert redacted.count(EMAIL_TOKEN) == 2


def test_clean_text_passes_through_unchanged() -> None:
    text = "Asha Nair. Senior Backend Engineer. Python, Go, PostgreSQL."

    redacted, report, _ = redact_pii(text)

    assert redacted == text
    assert report.total == 0
