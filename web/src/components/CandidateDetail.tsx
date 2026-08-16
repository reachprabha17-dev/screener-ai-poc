import { ExternalLink } from 'lucide-react';
import { useEffect, useRef } from 'react';
import { useApi } from '../api/queries';
import type { CandidateSummary } from '../api/types';
import { escalationLabel, flagHelp, VERIFICATION_BADGE } from '../lib/labels';
import { Alert } from '../ui/Alert';
import { BandPill } from './BandPill';
import { CriterionBlock } from './CriterionBlock';
import { DecisionForm } from './DecisionForm';

/**
 * One candidate, in the order the decision is actually made.
 *
 * Band first, then why the system is unsure, then the criteria with their
 * evidence, then the decision. The numeric score is not the headline: three
 * verdict levels across at most twelve criteria cannot support a rendered
 * precision of `7.8`, and a decimal implies resolution the system cannot justify
 * to that precision. It is shown here, where its provenance is beside it, and
 * exported for audit (10.6).
 */
export function CandidateDetail({
  candidate,
  runId,
}: {
  candidate: CandidateSummary;
  runId: string;
}) {
  const api = useApi();
  const heading = useRef<HTMLHeadingElement>(null);

  // Choosing a candidate replaces this whole panel while the reviewer's focus is
  // still back in the list. Moving it to the heading is what makes the keyboard
  // and screen-reader path work: the next Tab lands in the assessment they just
  // opened rather than on the following name in the list.
  useEffect(() => {
    heading.current?.focus();
  }, [candidate.file_sha256]);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex items-center gap-3">
          <BandPill band={candidate.band} />
          <div>
            <h2 ref={heading} tabIndex={-1} className="text-lg font-semibold outline-none">
              {candidate.filename}
            </h2>
            <p className="text-xs text-neutral-500 dark:text-neutral-400">
              {VERIFICATION_BADGE[candidate.verification_status]}
              {candidate.score === null
                ? ' · not scored'
                : ` · internal score ${candidate.score.toFixed(1)}/10, exported for audit`}
            </p>
          </div>
        </div>
        {candidate.id === null ? null : (
          <a
            className="inline-flex items-center gap-1.5 rounded-lg border border-neutral-300 px-2.5 py-1 text-xs font-medium hover:bg-neutral-100 dark:border-neutral-700 dark:hover:bg-neutral-800"
            href={api.fileUrl(candidate.id)}
            target="_blank"
            rel="noreferrer"
          >
            <ExternalLink className="size-3.5" aria-hidden />
            Original document
          </a>
        )}
      </div>

      {candidate.verification_status === 'pending' ? (
        // A half-verified result that renders like a finished one is how someone
        // signs off on work that has not happened yet (17.6).
        <Alert tone="warn" title="Provisional">
          <p>
            The second model has not checked this candidate yet. Escalations it would raise are not
            shown below.
          </p>
        </Alert>
      ) : null}

      {candidate.score === null ? (
        // None, never 0.0 — a resume that could not be read is not a weak
        // candidate, and showing 0 would put them among genuinely weak ones.
        <Alert tone="info">
          <p>
            Not scored, and not ranked. That is a statement about the system, not about the
            candidate — see the flags below.
          </p>
        </Alert>
      ) : null}

      {candidate.escalation_reasons.length > 0 ? (
        <Alert tone="warn" title="Needs a human decision because">
          <ul className="list-disc pl-4">
            {candidate.escalation_reasons.map((reason) => (
              <li key={reason}>{escalationLabel(reason)}</li>
            ))}
          </ul>
        </Alert>
      ) : null}

      {candidate.flags.map((flag) => (
        <Alert key={flag} tone="warn" title={flag}>
          <p>{flagHelp(flag)}</p>
        </Alert>
      ))}

      {candidate.summary ? <p className="text-sm">{candidate.summary}</p> : null}
      {candidate.notable_strengths.length > 0 ? (
        <p className="text-sm">
          <strong>Notable:</strong> {candidate.notable_strengths.join('; ')}
        </p>
      ) : null}
      {candidate.red_flags.length > 0 ? (
        <p className="text-sm">
          <strong>Flagged in the document:</strong> {candidate.red_flags.join(', ')}
        </p>
      ) : null}

      <section>
        <h3 className="font-semibold">Criteria</h3>
        <p className="mb-2 text-sm text-neutral-500 dark:text-neutral-400">
          Evidence is quoted from the candidate&apos;s own document. It confirms the model read the
          resume faithfully — it cannot tell a true claim from a false one, and it is not fraud
          detection.
        </p>
        {candidate.criteria.map((criterion) => (
          <CriterionBlock
            key={criterion.id}
            criterion={criterion}
            resumeText={candidate.resume_text}
          />
        ))}
      </section>

      <section className="border-t border-neutral-200 pt-4 dark:border-neutral-800">
        <h3 className="font-semibold">Your decision</h3>
        <p className="mb-2 text-sm text-neutral-500 dark:text-neutral-400">
          Recorded against your name with the reason, and appended to this candidate&apos;s history
          rather than replacing it.
        </p>
        <DecisionForm candidate={candidate} runId={runId} />
      </section>
    </div>
  );
}
