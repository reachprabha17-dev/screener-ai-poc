import type { CriterionView } from '../api/types';
import { quoteInContext } from '../lib/highlight';
import { EVIDENCE_BADGE, VERDICT_LABEL } from '../lib/labels';
import { Alert } from '../ui/Alert';
import { Badge } from '../ui/Badge';

/**
 * One criterion: the verdict, the quote in its context, and any disagreement
 * about it (15.5).
 */
export function CriterionBlock({
  criterion,
  resumeText,
}: {
  criterion: CriterionView;
  resumeText: string;
}) {
  const context = quoteInContext(resumeText, criterion.highlights);
  const badge = EVIDENCE_BADGE[criterion.evidence_status];

  return (
    <article className="space-y-2 border-t border-neutral-100 py-3 first:border-t-0 dark:border-neutral-800">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={criterion.verdict === 'strong' ? 'ok' : 'neutral'}>
          {VERDICT_LABEL[criterion.verdict]}
        </Badge>
        <span className="font-medium">{criterion.text}</span>
        <span className="text-xs text-neutral-500 dark:text-neutral-400">
          weight {criterion.weight}
        </span>
        {criterion.must_have ? <Badge tone="warn">must-have</Badge> : null}
        {badge ? (
          <span className="text-xs text-neutral-500 dark:text-neutral-400">{badge}</span>
        ) : null}
      </div>

      {criterion.evidence ? (
        context ? (
          // The quote inside its surrounding paragraph, not alone: a bare quote
          // can be cherry-picked from a sentence that said the opposite (15.3).
          <blockquote>
            {context.truncatedStart ? '…' : ''}
            {context.before}
            <mark>{context.matched}</mark>
            {context.after}
            {context.truncatedEnd ? '…' : ''}
          </blockquote>
        ) : (
          <blockquote>{criterion.evidence}</blockquote>
        )
      ) : null}

      {criterion.negation_suspected ? (
        <p className="text-xs text-neutral-500 dark:text-neutral-400">
          A negation appears just before this quote — read the full sentence.
        </p>
      ) : null}

      {criterion.verifier ? (
        // "A second model disagrees — you decide", never "the correct answer is".
        // Presented as an answer, reviewers defer to it, and automated
        // decision-making returns through the interface (15.5).
        <Alert tone="info" title="A second model disagrees — you decide">
          <p>
            It suggests <code>{(criterion.verifier.suggested_verdict ?? '—').toUpperCase()}</code>.
          </p>
          {criterion.verifier.rationale ? <p>{criterion.verifier.rationale}</p> : null}
          {criterion.verifier.found_evidence ? (
            <blockquote>{criterion.verifier.found_evidence}</blockquote>
          ) : null}
        </Alert>
      ) : null}
    </article>
  );
}
