import ReactMarkdown from 'react-markdown';
import type { ReactNode } from 'react';

/**
 * The audit log's sentences, rendered.
 *
 * `describeEvent` composes each row into a sentence with `**bold**` and
 * `` `code` `` in it, and several of those sentences interpolate values that came
 * out of the database — a decision reason, a folder path someone typed.
 * `react-markdown` builds React elements rather than HTML, so no string in this
 * app is ever handed to the DOM as markup and an audit detail containing
 * `<script>` renders as the text it is.
 *
 * Rendered inline: these appear inside table cells and timeline rows, where a
 * block-level `<p>` would break the layout for no benefit.
 */
export function Markdown({ children }: { children: string }): ReactNode {
  return (
    <ReactMarkdown
      components={{
        p: ({ children: content }) => <span>{content}</span>,
        a: ({ children: content }) => <span>{content}</span>,
      }}
      // No `remark-gfm`, no raw HTML: the only marks these strings use are
      // emphasis and code spans, and a wider dialect is a wider parser over
      // untrusted text.
      disallowedElements={['img', 'iframe']}
    >
      {children}
    </ReactMarkdown>
  );
}
