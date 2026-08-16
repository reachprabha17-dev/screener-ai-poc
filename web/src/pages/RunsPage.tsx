import { usePositions, useRuns } from '../api/queries';
import { QueryState } from '../components/QueryState';
import { RunList } from '../components/RunList';
import { Card } from '../ui/Card';

/** Every run, newest first. The way back into work already in progress. */
export function RunsPage() {
  const runs = useRuns();
  // Includes closed requisitions: a run outlives the post it screened for.
  const positions = usePositions(true);

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-semibold tracking-tight">Runs</h1>
      <p className="text-sm text-neutral-500 dark:text-neutral-400">
        Each run screened one folder against one approved rubric, and holds the results and the
        decisions recorded against them.
      </p>
      <Card className="overflow-hidden">
        <QueryState query={runs}>
          {(rows) => <RunList runs={rows} positions={positions.data ?? []} />}
        </QueryState>
      </Card>
    </div>
  );
}
