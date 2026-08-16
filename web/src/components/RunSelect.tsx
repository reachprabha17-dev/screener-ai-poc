import { usePositions, useRuns } from '../api/queries';
import { date } from '../lib/format';
import { Field } from '../ui/Field';

/**
 * Pick a run, labelled by the things that identify one to a person.
 *
 * The id alone is not enough — `run-8f21c0` means nothing a week later — so the
 * requisition, the state and the date travel with it.
 */
export function RunSelect({
  value,
  onChange,
  label = 'Run',
}: {
  value: string;
  onChange: (runId: string) => void;
  label?: string;
}) {
  const runs = useRuns();
  // Includes closed requisitions: a run outlives the post it screened for.
  const positions = usePositions(true);
  const byId = new Map((positions.data ?? []).map((p) => [p.id, p]));

  if (runs.isPending) {
    return <p className="text-sm text-neutral-500 dark:text-neutral-400">Loading runs…</p>;
  }
  if (runs.isError) {
    return <p className="text-sm text-neutral-500 dark:text-neutral-400">{runs.error.message}</p>;
  }
  if (runs.data.length === 0) {
    return <p className="text-sm text-neutral-500 dark:text-neutral-400">No runs yet.</p>;
  }

  return (
    <Field label={label}>
      <select
        value={value}
        onChange={(event) => {
          onChange(event.target.value);
        }}
      >
        <option value="">Choose a run…</option>
        {runs.data.map((run) => (
          <option key={run.id} value={run.id}>
            {run.id} · {byId.get(run.position_id)?.reference ?? run.position_id} · {run.status} ·{' '}
            {date(run.created_at)}
          </option>
        ))}
      </select>
    </Field>
  );
}
