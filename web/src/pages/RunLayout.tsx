import { Link, NavLink, Outlet, useParams } from 'react-router-dom';
import { usePositions, useRunStatus, useRuns } from '../api/queries';
import { RunStatePill } from '../components/RunStatePill';
import { cn } from '../ui/cn';

/**
 * A run's two screens: how it is going, and what it found.
 *
 * They are tabs on one route rather than two places in the navigation because
 * only one of them is ever the answer to "what is happening with this run" — and
 * which one depends on whether it has finished, not on what the reviewer picked
 * from a menu.
 */
export function RunLayout() {
  const { runId = '' } = useParams();
  const runs = useRuns();
  // Includes closed requisitions: a run outlives the post it screened for.
  const positions = usePositions(true);
  const status = useRunStatus(runId);

  const run = runs.data?.find((r) => r.id === runId);
  const position = positions.data?.find((p) => p.id === run?.position_id);
  const state = status.data?.status ?? run?.status;

  const tab = ({ isActive }: { isActive: boolean }) =>
    cn(
      '-mb-px border-b-2 px-4 py-2 text-sm font-medium',
      isActive
        ? 'border-blue-600 text-neutral-900 dark:border-blue-400 dark:text-neutral-50'
        : 'border-transparent text-neutral-500 hover:text-neutral-800 dark:text-neutral-400 dark:hover:text-neutral-200',
    );

  return (
    <div className="space-y-4">
      <p className="text-sm text-neutral-500 dark:text-neutral-400">
        <Link to="/requisitions" className="hover:underline">
          Requisitions
        </Link>
        {position ? (
          <>
            {' / '}
            <Link to={`/requisitions/${position.id}`} className="hover:underline">
              {position.reference}
            </Link>
          </>
        ) : null}
        {' / '}
        <Link to="/runs" className="hover:underline">
          runs
        </Link>{' '}
        / {runId}
      </p>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">
            {position?.title ?? 'Screening run'}
          </h1>
          {state ? <RunStatePill state={state} /> : null}
        </div>
        {run ? (
          <p className="text-sm text-neutral-500 dark:text-neutral-400">
            <code>{run.folder}</code> · {run.file_count} files · started by {run.created_by}
          </p>
        ) : null}
      </div>

      <nav
        className="flex border-b border-neutral-200 dark:border-neutral-800"
        aria-label="Run views"
      >
        <NavLink to="." end className={tab}>
          Progress
        </NavLink>
        <NavLink to="review" className={tab}>
          Review
        </NavLink>
      </nav>

      <Outlet />
    </div>
  );
}
