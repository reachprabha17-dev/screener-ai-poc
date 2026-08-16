import { useSearchParams } from 'react-router-dom';
import { useRunStory } from '../../api/queries';
import type { AuditEntry, RunStory } from '../../api/types';
import { QueryState } from '../../components/QueryState';
import { RunSelect } from '../../components/RunSelect';
import { dateTime } from '../../lib/format';
import { describeEvent, HUMAN_GATES } from '../../lib/labels';
import { Alert } from '../../ui/Alert';
import { Badge } from '../../ui/Badge';
import { Card, CardBody } from '../../ui/Card';
import { Markdown } from '../../ui/Markdown';

/** One run, from raised requisition to sign-off, as a sequence of sentences. */
export function RunStoryPage() {
  const [params, setParams] = useSearchParams();
  const runId = params.get('run') ?? '';

  return (
    <div className="space-y-4">
      <Card>
        <CardBody>
          <RunSelect
            value={runId}
            onChange={(next) => {
              setParams(next ? { run: next } : {});
            }}
          />
        </CardBody>
      </Card>

      {runId ? <Story runId={runId} /> : null}
    </div>
  );
}

function Story({ runId }: { runId: string }) {
  const story = useRunStory(runId);

  return (
    <QueryState query={story} loading="Assembling the record…">
      {(data) => (
        <Card>
          <CardBody>
            <div>
              <h2 className="text-lg font-semibold">{data.position_reference}</h2>
              <p className="text-sm text-neutral-500 dark:text-neutral-400">
                Run <code>{data.run_id}</code> · rubric{' '}
                {data.rubric_version === null
                  ? 'unknown version'
                  : `v${String(data.rubric_version)}`}{' '}
                · approved by <strong>{data.approved_by ?? '—'}</strong> · signed off by{' '}
                <strong>{data.signed_off_by ?? 'not yet'}</strong>
              </p>
            </div>

            <SeparationOfDuties story={data} />

            {data.candidate_events_truncated ? (
              <Alert tone="info">
                <p>
                  This run has more candidates than the story shows individually. Per-candidate
                  decisions are truncated; the run-level record is complete.
                </p>
              </Alert>
            ) : null}

            <h3 className="font-semibold">Timeline</h3>
            {data.events.length === 0 ? (
              <p className="text-sm text-neutral-500 dark:text-neutral-400">
                No audit events recorded for this run.
              </p>
            ) : (
              <ol className="space-y-2">
                {data.events.map((event, index) => (
                  <EventRow key={`${event.ts}-${String(index)}`} event={event} />
                ))}
              </ol>
            )}
          </CardBody>
        </Card>
      )}
    </QueryState>
  );
}

/**
 * Whether one person both approved the rubric and accepted its results.
 *
 * Permitted by the system — the flow is demonstrable with one operator — but it is
 * the first thing an auditor checks, so the record says it outright instead of
 * leaving it to be reconstructed from two lines of the log.
 */
function SeparationOfDuties({ story }: { story: RunStory }) {
  if (!story.signed_off_by) {
    return (
      <Alert tone="warn">
        <p>Not signed off yet — no one has accepted these results.</p>
      </Alert>
    );
  }
  if (story.separation_of_duties) {
    return (
      <Alert tone="ok">
        <p>
          Separation of duties: <strong>{story.approved_by}</strong> approved the rubric and{' '}
          <strong>{story.signed_off_by}</strong> signed off the run.
        </p>
      </Alert>
    );
  }
  return (
    <Alert tone="warn">
      <p>
        <strong>{story.signed_off_by}</strong> both approved the rubric and signed off the run.
        Permitted, but a second reviewer is the stronger record.
      </p>
    </Alert>
  );
}

function EventRow({ event }: { event: AuditEntry }) {
  // The human gates and the decisions are what an auditor scans for, so they keep
  // a label; everything else is unmarked so the labels stay meaningful.
  const marker = HUMAN_GATES.has(event.action)
    ? 'human gate'
    : event.action === 'decision'
      ? 'decision'
      : '';

  return (
    <li className="flex flex-wrap items-baseline gap-x-3 gap-y-1 text-sm">
      <span className="w-32 shrink-0 text-xs tabular-nums text-neutral-500 dark:text-neutral-400">
        {dateTime(event.ts)}
      </span>
      <span className="min-w-0 flex-1">
        {marker ? <Badge tone="info">{marker}</Badge> : null}{' '}
        {/* `actor_id` is null for work no person did. Rendering that as a blank
            loses a fact the record is making deliberately. */}
        {event.actor_id ? (
          <strong>{event.actor_id}</strong>
        ) : (
          <em className="text-neutral-500 dark:text-neutral-400">System</em>
        )}{' '}
        {event.entity === 'candidate' ? `candidate #${event.entity_id ?? '?'} ` : ''}—{' '}
        <Markdown>{describeEvent(event.action, event.detail)}</Markdown>
      </span>
    </li>
  );
}
