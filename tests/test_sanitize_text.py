"""Unicode sanitization (spec §8.6, build gate §20 step 4).

The gate names three fixture families: bidi, zero-width, and homoglyph. The first
two are stripped and counted. The third is only *partly* handled by NFKC, and the
tests below pin exactly where the line falls rather than implying coverage that
does not exist.
"""

from screener.intake.sanitize_text import find_mixed_script_tokens, sanitize

RESUME = "Senior Backend Engineer with 7 years of experience in Python and Go."


# --- zero-width and other invisibles ----------------------------------------


def test_zero_width_characters_are_stripped_and_counted() -> None:
    """The classic evidence-breaker: invisible splitters inside a word.

    A reviewer sees "Python"; the extractor and the evidence matcher see a word
    that matches nothing.
    """
    poisoned = "Py​th‍on‌ and Go"
    cleaned, stripped = sanitize(poisoned)

    assert cleaned == "Python and Go"
    assert stripped == 3


def test_byte_order_mark_is_stripped() -> None:
    cleaned, stripped = sanitize("﻿" + RESUME)

    assert cleaned == RESUME
    assert stripped == 1


def test_private_use_and_surrogate_categories_are_stripped() -> None:
    """Co renders as whatever a font decides; Cs is not text at all."""
    cleaned, stripped = sanitize("Python and\ud800 Go")

    assert cleaned == "Python and Go"
    assert stripped == 2


# --- bidi overrides ----------------------------------------------------------


def test_bidi_override_is_stripped() -> None:
    """U+202E reverses rendering, so a PDF shows one string and delivers another.

    This is the attack that makes human oversight look like it is working while
    the reviewer reads text the model never saw.
    """
    cleaned, stripped = sanitize("Lead ‮regineer dael‬ developer")

    assert "‮" not in cleaned
    assert "‬" not in cleaned
    assert stripped == 2


def test_every_bidi_control_is_removed() -> None:
    from screener.intake.sanitize_text import BIDI

    cleaned, stripped = sanitize("a" + "".join(BIDI) + "b")

    assert cleaned == "ab"
    assert stripped == len(BIDI)


# --- control characters ------------------------------------------------------


def test_layout_whitespace_survives_but_other_controls_do_not() -> None:
    """Newlines and tabs are signal the parser produced deliberately."""
    cleaned, stripped = sanitize("Skills:\n\tPython\r\n\x07Go")

    assert cleaned == "Skills:\n\tPython\nGo"
    assert stripped == 2  # \r and \x07


# --- homoglyphs: what NFKC does ---------------------------------------------


def test_nfkc_folds_compatibility_homoglyphs() -> None:
    """Fullwidth forms, mathematical alphanumerics and ligatures fold to ASCII.

    Without this, honest evidence quoting these characters would be unmatchable
    in §10.5 against a document rendered any other way.
    """
    cleaned, _ = sanitize("Ｐython 𝐆𝐨 certiﬁed")

    assert cleaned == "Python Go certified"


def test_nfkc_normalization_is_not_counted_as_stripping() -> None:
    """``ﬁ`` → ``fi`` lengthens the text; the counter reports removals only.

    A signed delta across normalization and removal would report a negative
    number to a reviewer and mean nothing.
    """
    _, stripped = sanitize("certiﬁed")

    assert stripped == 0


def test_ideographic_space_normalizes_to_a_plain_space() -> None:
    cleaned, stripped = sanitize("Python　and Go")

    assert cleaned == "Python and Go"
    assert stripped == 0


# --- homoglyphs: what NFKC does NOT do --------------------------------------


def test_cyrillic_homoglyph_survives_nfkc() -> None:
    """Pinned deliberately. NFKC does not fold cross-script homoglyphs.

    Anyone reading §8.6 and assuming sanitization neutralizes a Cyrillic ``а``
    is wrong, and this test is where they find out. Folding it would corrupt
    every legitimately Cyrillic name in the corpus, so detection is the answer,
    not transformation.
    """
    cleaned, stripped = sanitize("pаyments")

    assert cleaned == "pаyments"
    assert stripped == 0
    assert cleaned != "payments"


def test_mixed_script_token_is_reported() -> None:
    assert find_mixed_script_tokens("built pаyments systems") == ["pаyments"]


def test_single_script_text_is_quiet() -> None:
    assert find_mixed_script_tokens(RESUME) == []


def test_accented_latin_is_not_mixed_script() -> None:
    """Combining marks carry no script identity — otherwise every accented name fires."""
    assert find_mixed_script_tokens("José García") == []


def test_bilingual_resume_is_not_flagged() -> None:
    """Scripts separated by word boundaries are an ordinary CV, not an attack.

    A homoglyph has to sit *inside* a word to keep it readable.
    """
    assert find_mixed_script_tokens("Python developer مهندس برمجيات") == []


def test_alphanumeric_tokens_are_not_mixed_script() -> None:
    assert find_mixed_script_tokens("Python3 AWS-2019 C1") == []


# --- ordering guarantee ------------------------------------------------------


def test_sanitize_output_is_idempotent() -> None:
    """Downstream stages re-run nothing; a second pass must be a no-op.

    NFKC is idempotent and the removals are unconditional, so this holds — but
    it is the property every later stage assumes when it treats the sanitized
    string as canonical.
    """
    once, _ = sanitize("Py​thon‮ ﬁle　Ｇo")
    twice, stripped = sanitize(once)

    assert twice == once
    assert stripped == 0
