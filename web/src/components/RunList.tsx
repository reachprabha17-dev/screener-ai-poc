import { Link } from 'react-router-dom';
import type { Position, Run } from '../api/types';
import { date, percent } from '../lib/format';
import { Table, Td, Th } from '../ui/Table';
import { RunStatePill } from './RunStatePill';

/**
 * Runs as a table, newest first — the order the API already returns.
 *
 * The escalation rate is a column rather than something found by opening each
 * run: across a page of runs it is the number that says whether the screening is
 * working, and a rate climbing run over run is only visible when they are side by
 * side.
 */
export function RunList({ runs, positions }: { runs: Run[]; positions?: Position[] }) {
  if (runs.length === 0) {
    return (
      <p className="py-8 text-center text-sm text-neutral-500 dark:text-neutral-400">
        No screening runs yet.
      </p>
    );
  }

  const byId = new Map((positions ?? []).map((p) => [p.id, p]));

  return (
    <Table caption="Screening runs">
      <thead>
        <tr>
          <Th>Run</Th>
          {positions ? <Th>Requisition</Th> : null}
          <Th>State</Th>
          <Th>Files</Th>
          <Th>Needing review</Th>
          <Th>Started</Th>
        </tr>
      </thead>
      <tbody>
        {runs.map((run) => {
          const position = byId.get(run.position_id);
          return (
            <tr key={run.id} className="hover:bg-neutral-50 dark:hover:bg-neutral-800/50">
              <Td>
                <Link
                  to={`/runs/${run.id}`}
                  className="font-medium text-blue-600 dark:text-blue-400"
                >
                  {run.id}
                </Link>
              </Td>
              {positions ? (
                <Td>
                  {position ? (
                    <Link
                      to={`/requisitions/${position.id}`}
                      className="text-blue-600 dark:text-blue-400"
                    >
                      {position.reference}
                    </Link>
                  ) : (
                    <span className="text-neutral-500">{run.position_id}</span>
                  )}
                </Td>
              ) : null}
              <Td>
                <RunStatePill state={run.status} />
              </Td>
              <Td>{run.file_count}</Td>
              <Td>{run.escalation_rate === null ? '—' : percent(run.escalation_rate)}</Td>
              <Td className="text-neutral-500 dark:text-neutral-400">{date(run.created_at)}</Td>
            </tr>
          );
        })}
      </tbody>
    </Table>
  );
}
