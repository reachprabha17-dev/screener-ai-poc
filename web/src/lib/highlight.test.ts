import { describe, expect, it } from 'vitest';
import { quoteInContext } from './highlight';

const RESUME = `${'a'.repeat(300)}Led the migration to Kubernetes${'b'.repeat(300)}`;
const MATCH = { start: 300, end: 331 };

describe('quoteInContext', () => {
  it('returns the quote with the document either side of it', () => {
    // A bare quote can be cherry-picked from a sentence that said the opposite,
    // so the reviewer sees the paragraph it came from (15.3).
    const context = quoteInContext(RESUME, [MATCH]);

    expect(context?.matched).toBe('Led the migration to Kubernetes');
    expect(context?.before).toHaveLength(240);
    expect(context?.after).toHaveLength(240);
    expect(context?.truncatedStart).toBe(true);
    expect(context?.truncatedEnd).toBe(true);
  });

  it('spans every highlight when the evidence matched in several places', () => {
    const context = quoteInContext(RESUME, [
      { start: 300, end: 310 },
      { start: 320, end: 331 },
    ]);

    expect(context?.matched).toBe('Led the migration to Kubernetes');
  });

  it('marks nothing as truncated when the whole document is shown', () => {
    const context = quoteInContext('Python and Go', [{ start: 0, end: 6 }]);

    expect(context).toEqual({
      before: '',
      matched: 'Python',
      after: ' and Go',
      truncatedStart: false,
      truncatedEnd: false,
    });
  });

  it('falls back to nothing when the offsets do not fit the text', () => {
    // The redaction map translates offsets out of `sent_text` server-side. If
    // that ever produces a span past the end, the reviewer gets the plain quote
    // rather than a sliced-to-empty blockquote.
    expect(quoteInContext('short', [{ start: 10, end: 40 }])).toBeNull();
    expect(quoteInContext('', [MATCH])).toBeNull();
    expect(quoteInContext(RESUME, [])).toBeNull();
  });
});
