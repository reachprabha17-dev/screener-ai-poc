import type { HighlightSpan } from '../api/types';

/** How much of the surrounding document to show either side of a quote. */
const CONTEXT_CHARS = 240;

export interface QuoteInContext {
  before: string;
  matched: string;
  after: string;
  truncatedStart: boolean;
  truncatedEnd: boolean;
}

/**
 * The matched span with the text around it, never the quote alone.
 *
 * A bare quote can be cherry-picked from a sentence that said the opposite, so
 * the reviewer is shown the paragraph it came from with the match marked inside
 * it (15.3).
 *
 * Offsets arrive already translated out of the redacted `sent_text` by the read
 * layer, so this only has to slice. Doing the translation here would put it
 * downstream of the boundary that owns it, in the one process with no server
 * tests.
 */
export function quoteInContext(
  resumeText: string,
  highlights: HighlightSpan[],
): QuoteInContext | null {
  if (!resumeText || highlights.length === 0) return null;

  const start = Math.min(...highlights.map((h) => h.start));
  const end = Math.max(...highlights.map((h) => h.end));
  if (start < 0 || end > resumeText.length || start >= end) return null;

  const left = Math.max(0, start - CONTEXT_CHARS);
  const right = Math.min(resumeText.length, end + CONTEXT_CHARS);

  return {
    before: resumeText.slice(left, start),
    matched: resumeText.slice(start, end),
    after: resumeText.slice(end, right),
    truncatedStart: left > 0,
    truncatedEnd: right < resumeText.length,
  };
}
