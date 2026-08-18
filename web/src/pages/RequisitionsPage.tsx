import { Plus } from 'lucide-react';
import { Link } from 'react-router-dom';
import { usePositions } from '../api/queries';
import { QueryState } from '../components/QueryState';
import { date } from '../lib/format';
import { Card } from '../ui/Card';
import { Table, Td, Th } from '../ui/Table';

/**
 * The list of open requisitions, and the way into a new one.
 *
 * A requisition is the unit everything else hangs off — a rubric belongs to one,
 * a run screens one folder against one approved rubric — so it is the first screen
 * and the default route.
 */
export function RequisitionsPage() {
  const positions = usePositions();

  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Job Postings</h1>
        <Link
          to="/requisitions/new"
          className="inline-flex items-center gap-2 rounded-lg bg-blue-600 px-3.5 py-2 text-sm font-medium text-white hover:bg-blue-700 dark:bg-blue-500 dark:text-neutral-950 dark:hover:bg-blue-400"
        >
          <Plus className="size-4" aria-hidden />
          New job posting
        </Link>
      </div>

      <QueryState query={positions}>
        {(rows) =>
          rows.length === 0 ? (
            <Card className="p-10 text-center">
              <p className="mb-3 text-sm text-neutral-500 dark:text-neutral-400">
                No open job postings yet.
              </p>
              <Link
                to="/requisitions/new"
                className="inline-flex rounded-lg bg-blue-600 px-3.5 py-2 text-sm font-medium text-white hover:bg-blue-700 dark:bg-blue-500 dark:text-neutral-950"
              >
                Post the first one
              </Link>
            </Card>
          ) : (
            <Card className="overflow-hidden">
              <Table caption="Open job postings">
                <thead>
                  <tr>
                    <Th>Reference</Th>
                    <Th>Title</Th>
                    <Th>Posted by</Th>
                    <Th>Posted</Th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((position) => (
                    <tr
                      key={position.id}
                      className="hover:bg-neutral-50 dark:hover:bg-neutral-800/50"
                    >
                      <Td>
                        <Link
                          to={`/requisitions/${position.id}`}
                          className="font-medium text-blue-600 dark:text-blue-400"
                        >
                          {position.reference}
                        </Link>
                      </Td>
                      <Td>{position.title}</Td>
                      <Td className="text-neutral-500 dark:text-neutral-400">
                        {position.created_by}
                      </Td>
                      <Td className="text-neutral-500 dark:text-neutral-400">
                        {date(position.created_at)}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>
            </Card>
          )
        }
      </QueryState>
    </div>
  );
}
