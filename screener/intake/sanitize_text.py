"""Unicode sanitization (spec §8.6). Pure — no I/O, no model.

Applied to extracted text **before** injection detection, redaction, budgeting or
evidence matching, and the string it returns is the same one §10.5 later matches
against. Everything downstream assumes it has already run.

**Why this is load-bearing.** Zero-width characters and bidi overrides let a PDF
display one thing to a reviewer and deliver another to the extractor — so a
reviewer performing human oversight is reading text that is not what the model
judged. That is oversight failing while appearing to work, which is the same
failure mode §18.2 describes for the review queue. NFKC additionally folds the
compatibility homoglyphs (fullwidth forms, mathematical alphanumerics,
ligatures) that would otherwise make honest evidence unmatchable in §10.5.

**What NFKC does not do.** It does not fold cross-script homoglyphs: Cyrillic
``а`` (U+0430) survives normalization looking exactly like Latin ``a``. Mapping
it would corrupt every legitimately Cyrillic name in the corpus, so this module
does not transform it — ``find_mixed_script_tokens`` reports it instead, for the
same reason §10.2 escalates rather than excludes. A word mixing scripts is worth
a human look; it is never grounds for an adverse outcome on its own.
"""

import unicodedata

# Explicit rather than relying on the Cf sweep below: these are the characters
# that reorder rendered text, and naming them keeps the intent legible if the
# category rules are ever narrowed. U+200E/U+200F are marks, not overrides, but
# carry the same reader/extractor divergence.
BIDI = frozenset(
    {
        "‪",  # LEFT-TO-RIGHT EMBEDDING
        "‫",  # RIGHT-TO-LEFT EMBEDDING
        "‬",  # POP DIRECTIONAL FORMATTING
        "‭",  # LEFT-TO-RIGHT OVERRIDE
        "‮",  # RIGHT-TO-LEFT OVERRIDE
        "⁦",  # LEFT-TO-RIGHT ISOLATE
        "⁧",  # RIGHT-TO-LEFT ISOLATE
        "⁨",  # FIRST STRONG ISOLATE
        "⁩",  # POP DIRECTIONAL ISOLATE
        "‎",  # LEFT-TO-RIGHT MARK
        "‏",  # RIGHT-TO-LEFT MARK
    }
)

# Cf: invisible formatting, including every zero-width character and the BOM.
# Co: private use — renders as whatever a font decides, means nothing to a parser.
# Cs: unpaired surrogates, which are not text at all.
_INVISIBLE_CATEGORIES = frozenset({"Cf", "Co", "Cs"})

# Layout is signal a resume parser produces deliberately; the rest of the control
# characters are not.
_KEEP_CONTROL = frozenset({"\n", "\t"})

# Combining marks and digits carry no script identity of their own, so counting
# them would make every accented Latin word look mixed.
_SCRIPTLESS_NAME_PREFIXES = ("COMBINING", "DIGIT", "MODIFIER")


def sanitize(raw: str) -> tuple[str, int]:
    """Return ``(cleaned_text, chars_stripped)``.

    NFKC first, then the invisible and control characters are dropped.
    ``chars_stripped`` counts removals only — it is measured after normalization,
    because NFKC itself can *lengthen* text (``ﬁ`` → ``fi``) and a signed delta
    across both steps would mean nothing to a reviewer.

    A non-zero count sets ``SANITIZED_TEXT`` and is logged. A high count on a
    single CV is worth a human look: legitimate documents rarely carry many
    invisible characters.
    """
    text = unicodedata.normalize("NFKC", raw)
    kept = [
        ch
        for ch in text
        if ch not in BIDI
        and unicodedata.category(ch) not in _INVISIBLE_CATEGORIES
        and (ch in _KEEP_CONTROL or unicodedata.category(ch) != "Cc")
    ]
    cleaned = "".join(kept)
    return cleaned, len(text) - len(cleaned)


def _script_of(ch: str) -> str | None:
    """Script name for a letter, from its Unicode name's first word.

    A deliberate approximation. The alternative is vendoring the script property
    table or adding a dependency for one heuristic whose only consequence is a
    reviewer taking a second look.
    """
    if not ch.isalpha():
        return None
    name = unicodedata.name(ch, "")
    if not name or name.startswith(_SCRIPTLESS_NAME_PREFIXES):
        return None
    return name.split(" ", 1)[0]


def find_mixed_script_tokens(text: str) -> list[str]:
    """Words whose letters come from more than one script.

    A homoglyph attack has to mix scripts *within a word* to keep the word
    readable, so this fires on ``pаyments`` (Cyrillic а) and stays quiet on a CV
    that is Latin in one paragraph and Arabic in the next — which is an ordinary
    bilingual resume, not an attack.

    Reported, never stripped. See the module docstring.
    """
    suspicious = []
    for token in text.split():
        scripts = {s for s in (_script_of(ch) for ch in token) if s is not None}
        if len(scripts) > 1:
            suspicious.append(token)
    return suspicious
