import { ESCALATION_BUDGET, escalationLabel } from '../lib/labels';
import { percent } from '../lib/format';
import { Alert } from '../ui/Alert';
import { Badge } from '../ui/Badge';

/**
 * The escalation rate, on screen while the run is in progress rather than after it.
 *
 * Human oversight collapses into rubber-stamping the moment the review queue
 * exceeds what a person will actually read, and that failure is silent — the
 * control still *looks* like it is working. The number is shown against its design
 * budget so it is noticed during the run rather than discovered afterwards (18.2).
 */
export function EscalationMeter({
  rate,
  count,
  total,
  breakdown,
}: {
  rate: number;
  count?: number;
  total?: number;
  breakdown?: Record<string, number>;
}) {
  const over = rate > ESCALATION_BUDGET;
  const delta = (rate - ESCALATION_BUDGET) * 100;

  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-4">
        <div>
          <p className="text-xs font-semibold tracking-wide text-neutral-500 uppercase dark:text-neutral-400">
            Needing review
          </p>
          <p className="text-2xl font-semibold">
            {percent(rate)}
            {count !== undefined && total !== undefined ? (
              <span className="ml-1.5 text-sm font-normal text-neutral-500 dark:text-neutral-400">
                ({count} of {total})
              </span>
            ) : null}
          </p>
        </div>
        <Badge tone={over ? 'warn' : 'ok'}>
          {delta >= 0 ? '+' : ''}
          {delta.toFixed(1)} pts vs {percent(ESCALATION_BUDGET)} budget
        </Badge>
        {breakdown ? <EscalationBreakdown counts={breakdown} /> : null}
      </div>

      {over ? (
        <Alert tone="warn">
          <p>
            {percent(rate)} of candidates need a human decision, against a{' '}
            {percent(ESCALATION_BUDGET)} design budget. A queue larger than a person will genuinely
            read is the point at which review stops being meaningful.
          </p>
        </Alert>
      ) : null}
    </div>
  );
}

/**
 * Grouped by reason, never one undifferentiated count (15.4).
 *
 * "23 need review" prompts a shrug. "8 unverified evidence, 7 judge disagreement"
 * tells a reviewer that similar cases can be worked in a batch, which is the
 * difference between a queue that gets cleared and one that does not.
 */
export function EscalationBreakdown({ counts }: { counts: Record<string, number> }) {
  const rows = Object.entries(counts)
    .filter(([, count]) => count > 0)
    .sort((a, b) => b[1] - a[1]);

  if (rows.length === 0) return null;

  return (
    <ul className="flex flex-wrap gap-2">
      {rows.map(([reason, count]) => (
        <li key={reason}>
          <Badge>
            <strong>{count}</strong> {escalationLabel(reason)}
          </Badge>
        </li>
      ))}
    </ul>
  );
}
