import { Disclosure } from '../ui/Disclosure';

/**
 * What the parser actually extracted from the document — distinct from both
 * the per-criterion evidence snippets (a few words of context each) and the
 * "Original document" link (the raw uploaded file, unparsed).
 *
 * Collapsed by default, and rendered with the original line breaks intact
 * rather than reflowed or split into invented sections: the backend has no
 * notion of resume sections, so anything beyond preserving whitespace would
 * be structure this component made up.
 */
export function ParsedResumeText({ resumeText }: { resumeText: string }) {
  return (
    <Disclosure summary="Parsed resume text">
      <p className="text-sm text-neutral-500 dark:text-neutral-400">
        Extracted from the uploaded document. Compare against the original if a quote below looks
        wrong — parsing can occasionally garble a section.
      </p>
      <pre className="max-h-96 overflow-y-auto whitespace-pre-wrap break-words rounded-lg bg-neutral-50 p-3 font-mono text-xs text-neutral-800 dark:bg-neutral-950 dark:text-neutral-200">
        {resumeText}
      </pre>
    </Disclosure>
  );
}
