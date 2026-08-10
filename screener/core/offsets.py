"""Offset translation between the two stored text versions (spec 12.6). Pure.

`match_blocks` are offsets into `sent_text` — the redacted string the model was
given and the only string a quote can honestly be matched against. HR reads
`resume_text`, which still has names in it. Redaction changes the length of the
string at every substitution, so the same character sits at a different index in
each version, and the drift accumulates down the document.

**This is the most likely thing in the read path to be silently wrong.** A
highlight landing two words late looks like a rendering quirk, not a bug, and the
text either side of a redaction still reads plausibly — so nobody reports it, and
the reviewer ends up being shown a different phrase from the one the system
verified. That is why translation is a pure function with a property test under
it before anything depends on it (19.4).

The map is a list of *preserved* segments (`Span`), in both coordinate systems.
Anything not covered by a segment is a redaction placeholder: text that exists in
`sent_text` and has no counterpart in `resume_text`.
"""

from screener.models import MatchBlock, Span


def translate_offset(dst: int, span_map: list[Span]) -> int:
    """A `sent_text` offset to its `resume_text` offset.

    Three cases, and the third is the one that matters:

    * inside a preserved segment — shift by that segment's delta;
    * inside a placeholder — there is no exact answer, because the placeholder
      stands for text of a different length. Map to the **start** of the
      redacted region, so a highlight that clips a placeholder opens at the
      redaction rather than somewhere arbitrary inside the surrounding text;
    * past the end — clamp. Callers pass `end - 1` for exclusive bounds, and an
      offset one past the last character is a legitimate thing to ask about.
    """
    if not span_map:
        return dst

    previous: Span | None = None
    for span in span_map:
        if span.dst_start <= dst < span.dst_end:
            return span.src_start + (dst - span.dst_start)
        if dst < span.dst_start:
            # Before this segment and after the previous one: inside a
            # placeholder. The redacted region begins in the source exactly
            # where the previous preserved segment ended, so that is the answer
            # — not this segment's `src_start`, which is where the redacted
            # region *ends* and would put the highlight after the removed text
            # rather than at it.
            return previous.src_end if previous is not None else 0
        previous = span

    return span_map[-1].src_end


def translate_block(block: MatchBlock, span_map: list[Span]) -> MatchBlock:
    """Translate a match block into `resume_text` coordinates.

    `doc_end` is exclusive, so it is translated as "the offset after the last
    included character" — translate `end - 1` and add one. Translating the
    exclusive bound directly would map the character *after* the block, which at
    a segment boundary belongs to the next segment and stretches the highlight
    across a redaction.
    """
    start = translate_offset(block.doc_start, span_map)
    end = translate_offset(max(block.doc_start, block.doc_end - 1), span_map) + 1
    return MatchBlock(
        ev_start=block.ev_start,
        ev_end=block.ev_end,
        doc_start=start,
        doc_end=max(start, end),
    )
