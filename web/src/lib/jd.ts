import type { JdSource } from '../api/types';

/**
 * A job description on its way into a requisition, with where it came from.
 *
 * Lives here rather than beside `JdInput` because it is shared state between
 * that component and the page that submits it — and a module exporting both a
 * component and a constant defeats fast refresh, which is the rule the linter
 * is enforcing.
 *
 * `filename`, `fileSha256` and `ocrUsed` are null for a pasted description: not
 * applicable, rather than unknown. The hash identifies the *uploaded file* and
 * not `text`, because the reviewer edits the extracted text before submitting
 * it — deliberately, since that correction step is the whole point of reading
 * the document in front of them.
 */
export interface JdValue {
  text: string;
  source: JdSource;
  filename: string | null;
  fileSha256: string | null;
  ocrUsed: boolean | null;
}

export const emptyJd: JdValue = {
  text: '',
  source: 'paste',
  filename: null,
  fileSha256: null,
  ocrUsed: null,
};
