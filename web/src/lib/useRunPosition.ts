import { usePositions, useRuns } from '../api/queries';
import type { Position, Run } from '../api/types';

/**
 * The requisition a run belongs to, resolved from the two lists that name it.
 *
 * There is no `GET /runs/{id}` and no position on the ranked response, so this
 * is two `find`s over lists the app already holds. Shared rather than repeated,
 * because the obvious way to write it a second time is `usePositions()` without
 * the flag — and that drops the closed requisitions. A run outlives the post it
 * screened for; closing one does not stop it. Writing it twice buys a screen
 * that quietly loses its job title the day HR closes the vacancy.
 *
 * Both queries are already cached under the same keys by whichever screen loaded
 * first, so calling this a second time inside the same route costs nothing.
 */
export function useRunPosition(runId: string): {
  run: Run | undefined;
  position: Position | undefined;
} {
  const runs = useRuns();
  const positions = usePositions(true);

  const run = runs.data?.find((r) => r.id === runId);
  return { run, position: positions.data?.find((p) => p.id === run?.position_id) };
}
