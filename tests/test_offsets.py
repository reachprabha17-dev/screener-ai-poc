"""Offset translation between `sent_text` and `resume_text` (spec 12.6, 19.4).

Build gate step 4: translation round-trips for every offset, and a highlight
lands correctly across a redaction boundary.

The failure this guards is not a crash. A highlight that drifts by the length of
a redaction still renders, still sits inside plausible-looking text, and shows
the reviewer a phrase the system never verified. Nobody files that as a bug, so
the only place it can be caught is here.
"""

from hypothesis import given
from hypothesis import strategies as st

from screener.core.offsets import translate_block, translate_offset
from screener.core.redact_pii import identity_map, redact_pii
from screener.models import MatchBlock, Span

RESUME = (
    "Asha Nair\n"
    "Email: asha.nair@example.com\n"
    "Date of birth: 12 March 1985\n"
    "Senior Backend Engineer, 2019-2024. Managed EKS clusters for 40 services.\n"
)


def find_block(haystack: str, needle: str) -> MatchBlock:
    start = haystack.index(needle)
    return MatchBlock(ev_start=0, ev_end=len(needle), doc_start=start, doc_end=start + len(needle))


# --- the property the read layer rests on ------------------------------------


@given(text=st.text(min_size=1, max_size=400))
def test_every_offset_translates_into_the_source_document(text: str) -> None:
    """Well-formedness: no offset ever lands outside `resume_text`.

    An out-of-range offset is an exception in the read path, which means one
    malformed highlight takes out the whole candidate detail screen.
    """
    sent, _report, span_map = redact_pii(text)

    for dst in range(len(sent)):
        src = translate_offset(dst, span_map)
        assert 0 <= src <= len(text)


@given(text=st.text(min_size=1, max_size=400))
def test_preserved_characters_translate_to_themselves(text: str) -> None:
    """The strong property: inside a preserved segment, translation is exact.

    Offsets inside a redaction placeholder are excluded deliberately — the
    placeholder stands for text of a different length, so there is no character
    to agree with. Everywhere else, the character the reviewer sees highlighted
    must be the character the model matched.
    """
    sent, _report, span_map = redact_pii(text)

    for span in span_map:
        for dst in range(span.dst_start, span.dst_end):
            assert text[translate_offset(dst, span_map)] == sent[dst]


@given(text=st.text(min_size=1, max_size=400))
def test_translation_never_runs_backwards(text: str) -> None:
    """Monotonic: later in the redacted text is never earlier in the source.

    A non-monotonic map produces highlights whose end precedes their start,
    which renders as an empty or inverted selection depending on the browser.
    """
    sent, _report, span_map = redact_pii(text)

    previous = -1
    for dst in range(len(sent)):
        src = translate_offset(dst, span_map)
        assert src >= previous
        previous = src


# --- the case the property test will not generate ----------------------------


def test_a_highlight_after_a_redaction_lands_on_the_right_words() -> None:
    """The concrete failure: two redactions above the evidence, and it still lands.

    `hypothesis` will not invent an email address followed by a date of birth,
    so the case that actually occurs on every real CV needs stating explicitly.
    """
    sent, report, span_map = redact_pii(RESUME)
    assert report.any_redacted

    quote = "Managed EKS clusters for 40"
    block = find_block(sent, quote)
    assert block.doc_start != RESUME.index(quote), "no drift here means the test proves nothing"

    translated = translate_block(block, span_map)

    assert RESUME[translated.doc_start : translated.doc_end] == quote


def test_the_evidence_offsets_are_carried_through_untouched() -> None:
    """`ev_*` address the evidence string, which redaction never touched."""
    sent, _report, span_map = redact_pii(RESUME)
    block = find_block(sent, "Senior Backend Engineer")

    translated = translate_block(block, span_map)

    assert (translated.ev_start, translated.ev_end) == (block.ev_start, block.ev_end)


def test_an_offset_inside_a_placeholder_maps_to_the_redaction() -> None:
    """No exact answer exists, so it opens at the redaction rather than mid-word.

    The alternative — mapping to the end of the preceding segment — puts the
    highlight's edge on the last real word before the redaction, implying the
    system matched a word it did not.
    """
    sent, _report, span_map = redact_pii(RESUME)
    placeholder = sent.index("[EMAIL]")

    inside = translate_offset(placeholder + 3, span_map)

    assert RESUME[inside : inside + len("asha.nair@example.com")] == "asha.nair@example.com"


def test_translation_is_a_no_op_when_redaction_is_off() -> None:
    """`identity_map` exists so the read path has no branch to forget."""
    span_map = identity_map(RESUME)
    block = find_block(RESUME, "Managed EKS clusters")

    assert translate_block(block, span_map) == block


def test_an_empty_map_leaves_offsets_alone() -> None:
    """Degenerate input from an older row must not raise in the read path."""
    assert translate_offset(17, []) == 17


def test_an_offset_past_the_last_segment_clamps() -> None:
    """Exclusive end bounds legitimately ask about one past the last character."""
    span_map = [Span(src_start=0, src_end=5, dst_start=0, dst_end=5)]

    assert translate_offset(99, span_map) == 5
