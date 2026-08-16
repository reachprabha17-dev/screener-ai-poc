import { Briefcase, ClipboardCheck, FileText, Plus } from 'lucide-react';
import { Link } from 'react-router-dom';
import { useDashboard } from '../api/queries';
import type { Dashboard } from '../api/types';
import { QueryState } from '../components/QueryState';
import { StatTile } from '../components/StatTile';
import { Card, CardBody, CardHeader } from '../ui/Card';
import { Table, Td, Th } from '../ui/Table';

/**
 * The three numbers, and the way into the work behind the third.
 *
 * **One request, not one per run.** The counts come from a single endpoint that
 * computes them in one transaction. Assembling them here — `/positions`, then
 * `/runs`, then a candidate list per run — would download every résumé in the
 * database to produce three integers, and would show numbers taken at three
 * different instants that visibly fail to add up.
 *
 * **The review queue is broken out by run.** Review happens inside a run, so
 * "31 candidates to review" is a number until it says which runs hold them —
 * the same reasoning that groups escalations by reason rather than reporting one
 * count (15.4).
 */
export function DashboardPage() {
  const dashboard = useDashboard();

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
        <Link
          to="/requisitions/new"
          className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-3.5 py-2 text-sm font-medium text-white hover:bg-blue-700 dark:bg-blue-500 dark:text-neutral-950 dark:hover:bg-blue-400"
        >
          <Plus className="size-4" aria-hidden />
          New requisition
        </Link>
      </div>

      <QueryState query={dashboard} loading="Counting…">
        {(data) => <Summary data={data} />}
      </QueryState>
    </div>
  );
}

function Summary({ data }: { data: Dashboard }) {
  const nothingYet = data.open_positions === 0 && data.applications === 0;

  return (
    <div className="space-y-4">
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        <StatTile
          label="Active job postings"
          value={data.open_positions}
          icon={Briefcase}
          hint={
            data.open_positions > 0 ? (
              <Link to="/requisitions" className="text-blue-600 hover:underline dark:text-blue-400">
                Open requisitions →
              </Link>
            ) : (
              'No requisitions raised yet.'
            )
          }
        />

        <StatTile
          label="Total applications"
          value={data.applications}
          icon={FileText}
          // Said plainly, because the number is not simply "rows in a table":
          // a re-run of the same folder screens the same CV again, and counting
          // that as a new applicant would overstate every requisition.
          hint={
            data.unscreened_files > 0
              ? `${String(data.unscreened_files)} more waiting to be screened`
              : 'CVs screened, counted once per requisition'
          }
        />

        <StatTile
          label="Candidates to review"
          value={data.awaiting_review}
          icon={ClipboardCheck}
          attention={data.awaiting_review > 0}
          hint={
            data.awaiting_review > 0
              ? 'Undecided, and either escalated or not yet verified'
              : 'Nothing is waiting on a person'
          }
        />
      </div>

      {data.runs_in_progress > 0 ? (
        <p className="text-sm text-neutral-500 dark:text-neutral-400">
          {data.runs_in_progress} run{data.runs_in_progress === 1 ? '' : 's'} in progress — these
          numbers are still moving.{' '}
          <Link to="/runs" className="text-blue-600 hover:underline dark:text-blue-400">
            Watch them →
          </Link>
        </p>
      ) : null}

      {data.queues.length > 0 ? <Queues data={data} /> : null}

      {nothingYet ? (
        <Card className="p-10 text-center">
          <p className="text-sm text-neutral-500 dark:text-neutral-400">
            Nothing has been screened yet. Raise a requisition, approve its rubric, and start a run.
          </p>
        </Card>
      ) : null}
    </div>
  );
}

function Queues({ data }: { data: Dashboard }) {
  const listed = data.queues.reduce((total, queue) => total + queue.awaiting_review, 0);

  return (
    <Card className="overflow-hidden">
      <CardHeader
        title="Where the review queue is"
        hint="Sign-off on a run is blocked until every one of these has been advanced, held or rejected."
      />
      <Table caption="Runs holding candidates that need a decision">
        <thead>
          <tr>
            <Th>Requisition</Th>
            <Th>Run</Th>
            <Th className="w-32">To review</Th>
          </tr>
        </thead>
        <tbody>
          {data.queues.map((queue) => (
            <tr key={queue.run_id} className="hover:bg-neutral-50 dark:hover:bg-neutral-800/50">
              <Td>
                <span className="font-medium">{queue.position_reference}</span>
                {queue.position_title ? (
                  <span className="block text-xs text-neutral-500 dark:text-neutral-400">
                    {queue.position_title}
                  </span>
                ) : null}
              </Td>
              <Td>
                <Link
                  to={`/runs/${queue.run_id}/review`}
                  className="text-blue-600 hover:underline dark:text-blue-400"
                >
                  {queue.run_id}
                </Link>
              </Td>
              <Td className="tabular-nums">{queue.awaiting_review}</Td>
            </tr>
          ))}
        </tbody>
      </Table>

      {listed < data.awaiting_review ? (
        // The table is capped server-side; the headline count is not. Saying so
        // is the difference between a bounded list and a wrong total.
        <CardBody className="pt-0">
          <p className="text-sm text-neutral-500 dark:text-neutral-400">
            Showing the {data.queues.length} largest queues. {data.awaiting_review - listed} more
            candidates are waiting in other runs.
          </p>
        </CardBody>
      ) : null}
    </Card>
  );
}
