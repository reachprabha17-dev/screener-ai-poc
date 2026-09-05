"""Contact details for the export (15.6).

Two things are being guarded here. One is that the columns are actually usable —
international dialling formats and the applicant tracker's filename convention
are what the real corpus contains, not the tidy shapes a regex author imagines.
The other is that sharing the phone rule with `redact_pii` did not loosen it: the
`2015 - 2019` case below is the same fixture the redaction gate rests on, and a
change here that eats it converts every duration-based `strong` verdict into a
`partial` across every candidate.
"""

from screener.core.extract_contact import extract_contact, name_from_filename

# --- names, from the tracker's filenames -------------------------------------
#
# Every fixture in this block is a real filename from `data/resumes`.


def test_strips_the_requisition_prefix_and_the_document_suffix() -> None:
    assert name_from_filename("84637_Kumar_Narinder_resume.pdf") == "Kumar Narinder"


def test_normalises_a_shouted_name() -> None:
    assert name_from_filename("84637_GEOFREY_SEMAKULA_resume.pdf") == "Geofrey Semakula"


def test_normalises_a_lowercased_name() -> None:
    assert name_from_filename("84637_adil_Mohamed_resume.pdf") == "Adil Mohamed"


def test_keeps_an_initial_as_an_initial() -> None:
    assert name_from_filename("84637_S_Dharshan_resume.pdf") == "S Dharshan"


def test_handles_a_name_of_more_than_two_parts() -> None:
    assert (
        name_from_filename("84637_Addanthadka_Gangadhara_DIVIN_resume.pdf")
        == "Addanthadka Gangadhara Divin"
    )
    assert (
        name_from_filename("84637_Rajivi_shivamurthy_Lekhak_resume.pdf")
        == "Rajivi Shivamurthy Lekhak"
    )


def test_leaves_a_deliberately_mixed_case_name_alone() -> None:
    """`.title()` would render this `Mcdonald`. Mixed case is somebody's choice."""
    assert name_from_filename("84637_McDonald_Alan_resume.pdf") == "McDonald Alan"


def test_a_hyphen_inside_a_name_is_not_a_separator() -> None:
    """Underscores separate; a hyphen in that convention is part of the name."""
    assert name_from_filename("84637_Anne-Marie_Dubois_resume.pdf") == "Anne-Marie Dubois"


def test_falls_back_to_the_other_separator_when_there_is_no_underscore() -> None:
    """`jane-doe.pdf` — the demo fixture, and anything else off-convention."""
    assert name_from_filename("jane-doe.pdf") == "Jane Doe"
    assert name_from_filename("john.smith.docx") == "John Smith"


def test_a_filename_with_no_name_in_it_still_yields_something() -> None:
    """Stripping everything would leave the row with no handle at all."""
    assert name_from_filename("84637_resume.pdf") == "Resume"


def test_a_case_only_suffix_difference_is_still_a_suffix() -> None:
    assert name_from_filename("84637_Uy_Benito_RESUME.pdf") == "Uy Benito"
    assert name_from_filename("84637_Uy_Benito_CV.pdf") == "Uy Benito"


# --- email and phone, from the resume text -----------------------------------


def test_finds_an_email_and_a_phone_number() -> None:
    contact = extract_contact(
        "Ada Lovelace\nada@example.com | +44 20 7946 0958\n\nEXPERIENCE\n",
        "84637_Lovelace_Ada_resume.pdf",
    )

    assert contact.name == "Lovelace Ada"
    assert contact.email == "ada@example.com"
    assert contact.phone == "+44 20 7946 0958"


def test_reads_international_dialling_formats() -> None:
    formats = [
        "+971 50 123 4567",
        "+44 (0)20 7946 0958",
        "(555) 010-4422",
        "+91-98765-43210",
        "00971501234567",
    ]

    for number in formats:
        assert extract_contact(f"Contact: {number}", "x.pdf").phone, number


def test_a_date_range_is_not_a_phone_number() -> None:
    """The `redact_pii` gate, re-asserted from the side that reads the pattern.

    Eight digits. A looser rule reports it as a phone number here and eats it
    there, and the second half of that is a system-wide verdict downgrade.
    """
    assert extract_contact("2015 - 2019 Backend Engineer, Initech", "x.pdf").phone == ""


def test_a_bare_year_is_not_a_phone_number() -> None:
    assert extract_contact("Graduated 2014. Certified 1998.", "x.pdf").phone == ""


def test_the_first_email_wins() -> None:
    """A resume can carry a referee's address; the applicant's is at the top."""
    text = "ada@example.com\n\nREFEREES\nCharles Babbage — babbage@example.org"

    assert extract_contact(text, "x.pdf").email == "ada@example.com"


def test_a_phone_number_split_across_whitespace_is_normalised() -> None:
    """The cell should hold a number, not the resume's line wrapping."""
    assert extract_contact("Tel:  020   7946   0958", "x.pdf").phone == "020 7946 0958"


def test_returns_empty_strings_when_the_resume_has_neither() -> None:
    contact = extract_contact("EXPERIENCE\nRolling stock technician, 2015-2019.", "x.pdf")

    assert contact.email == ""
    assert contact.phone == ""


def test_a_purged_candidate_has_no_contact_details() -> None:
    """`purge_candidate` blanks `resume_text`; erasure has to reach these too.

    It does so without being told to, because they are derived rather than
    stored — which is the whole reason they are derived.
    """
    contact = extract_contact("", "[redacted]")

    assert contact.email == ""
    assert contact.phone == ""
