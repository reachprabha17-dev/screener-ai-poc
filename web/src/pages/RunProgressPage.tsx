import { Loader2 } from 'lucide-react';
import { Link, useParams } from 'react-router-dom';
import { toast } from 'sonner';
import { isLive, useRunControl, useRunStatus } from '../api/queries';
import type { FailedFile, RunStatus } from '../api/types';
import { EscalationMeter } from '../components/EscalationMeter';
import { QueryState } from '../components/QueryState';
import { duration } from '../lib/format';
import { Alert } from '../ui/Alert';
import { Button } from '../ui/Button';
import { Card, CardBody, CardHeader } from '../ui/Card';
import { Table, Td, Th } from '../ui/Table';

/** What each control did, said as a number rather than as "done". */
const OUTCOME = {
  start: (count: number) => `${String(count)} file(s) queued — the worker will pick them up.`,
  rescan: (count: number) => `${String(count)} new file(s) added.`,
  abort: () => 'Run aborted. It can be started again later.',
};

/**
 * How the run is going, refreshed while it is going.
 *
 * Polled every five seconds, not pushed: a job that updates every few seconds over
 * more than an hour does not justify a persistent connection (22.2). The poll
 * stops when the run does, so a finished run left open on a second monitor costs
 * nothing.
 */
export function RunProgressPage() {
  const { runId = '' } = useParams();
  const status = useRunStatus(runId);
  const control = useRunControl(runId);

  function run(action: 'start' | 'rescan' | 'abort'): void {
    control.mutate(action, {
      onSuccess: (count) => {
        toast.success(OUTCOME[action](count));
      },
      onError: (error) => {
        toast.error(error.message);
      },
    });
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader
          title="Controls"
          hint="Starting queues the snapshotted files for the worker. Nothing is ever silently added to a run in progress — a folder that has grown since the snapshot needs an explicit rescan."
        />
        <CardBody>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="primary"
              busy={control.isPending}
              onClick={() => {
                run('start');
              }}
            >
              Start screening
            </Button>
            <Button
              disabled={control.isPending}
              title="Pick up files added since the snapshot"
              onClick={() => {
                run('rescan');
              }}
            >
              Rescan folder
            </Button>
            <Button
              variant="danger"
              disabled={control.isPending}
              title="Stops the run. Already-screened results are kept."
              onClick={() => {
                run('abort');
              }}
            >
              Abort
            </Button>
          </div>
        </CardBody>
      </Card>

      <QueryState query={status} loading="Reading progress…">
        {(data) => <Progress runId={runId} status={data} />}
      </QueryState>
    </div>
  );
}

function Progress({ runId, status }: { runId: string; status: RunStatus }) {
  const finished = status.done + status.failed;
  const fraction = status.total > 0 ? finished / status.total : 0;
  const heading =
    status.phase === 'judge' ? 'Screening' : status.phase === 'verify' ? 'Verifying' : 'Finished';

  return (
    <>
      <Card>
        <CardHeader
          title={heading}
          aside={
            isLive(status.status) ? (
              <span className="flex items-center gap-2 text-xs text-neutral-500 dark:text-neutral-400">
                <Loader2 className="size-3.5 animate-spin" aria-hidden />
                live, refreshing every 5s
              </span>
            ) : null
          }
        />
        <CardBody>
          <div>
            <div
              className="h-2 w-full overflow-hidden rounded-full bg-neutral-200 dark:bg-neutral-800"
              role="progressbar"
              aria-valuemin={0}
              aria-valuemax={status.total}
              aria-valuenow={finished}
              aria-label="Files screened"
            >
              <div
                className="h-full rounded-full bg-blue-600 transition-[width] dark:bg-blue-500"
                style={{ width: `${String(Math.round(fraction * 100))}%` }}
              />
            </div>
            <p className="mt-2 text-sm text-neutral-500 dark:text-neutral-400">
              {finished} of {status.total} screened
              {status.phase === 'verify'
                ? ` · second-model checks ${String(status.phase_done)} of ${String(status.phase_total)}`
                : null}
            </p>
          </div>

          <dl className="grid grid-cols-2 gap-3 sm:grid-cols-5">
            <Metric label="Screened" value={status.done} />
            <Metric label="Failed" value={status.failed} />
            <Metric label="Remaining" value={status.pending + status.claimed} />
            <Metric label="ETA" value={duration(status.eta_seconds)} />
            <Metric label="Undecided" value={status.undecided_count} />
          </dl>

          {status.queue_depth_ahead > 0 ? (
            <p className="text-sm text-neutral-500 dark:text-neutral-400">
              {status.queue_depth_ahead} file(s) from earlier runs are ahead in the queue.
            </p>
          ) : null}
        </CardBody>
      </Card>

      <Card>
        <CardHeader title="Needing review" />
        <CardBody>
          <EscalationMeter rate={status.escalation_rate} breakdown={status.escalation_breakdown} />
          <Link to="review" className="text-sm text-blue-600 hover:underline dark:text-blue-400">
            Open the review screen →
          </Link>
        </CardBody>
      </Card>

      <FailedFiles files={status.failed_files} runId={runId} />
    </>
  );
}

function Metric({ label, value }: { label: string; value: string | number }) {
  return (
    <div className="rounded-lg bg-neutral-100 px-3 py-2 dark:bg-neutral-800/60">
      <dt className="text-xs font-semibold tracking-wide text-neutral-500 uppercase dark:text-neutral-400">
        {label}
      </dt>
      <dd className="text-xl font-semibold">{value}</dd>
    </div>
  );
}

/**
 * The files behind the `Failed` count, named.
 *
 * These produced no candidate, so they appear in none of the review groups — a
 * count alone leaves a reviewer no way to find out who is missing from the
 * results, or to tell "nobody applied" from "three CVs were never read".
 */
function FailedFiles({ files, runId }: { files: FailedFile[]; runId: string }) {
  if (files.length === 0) return null;

  return (
    <Card>
      <CardBody>
        <Alert tone="error" title={`${String(files.length)} file(s) were never screened`}>
          <p>
            They are missing from the results. This is an infrastructure failure, not a judgement
            about the applicant. Sign-off is blocked until they are resolved — fix the cause and use{' '}
            <strong>Rescan folder</strong>, or abort the run.
          </p>
        </Alert>
        <Table caption={`Files that failed in run ${runId}`}>
          <thead>
            <tr>
              <Th>File</Th>
              <Th>Phase</Th>
              <Th>Attempts</Th>
              <Th>Last error</Th>
            </tr>
          </thead>
          <tbody>
            {files.map((file) => (
              <tr key={file.filename}>
                <Td>{file.filename}</Td>
                <Td>{file.phase}</Td>
                <Td>{file.attempts}</Td>
                <Td className="text-xs text-neutral-500 dark:text-neutral-400">
                  {file.last_error}
                </Td>
              </tr>
            ))}
          </tbody>
        </Table>
      </CardBody>
    </Card>
  );
}
