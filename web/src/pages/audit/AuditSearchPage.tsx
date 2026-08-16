import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useSearchParams } from 'react-router-dom';
import { useAudit, type AuditFilters } from '../../api/queries';
import { QueryState } from '../../components/QueryState';
import { dateTime } from '../../lib/format';
import { describeEvent, KNOWN_ACTIONS } from '../../lib/labels';
import { Button } from '../../ui/Button';
import { Card, CardBody } from '../../ui/Card';
import { Field } from '../../ui/Field';
import { Markdown } from '../../ui/Markdown';
import { Table, Td, Th } from '../../ui/Table';

const PAGE_SIZE = 50;

/**
 * Every recorded action, filterable. Use this to follow a person or a date.
 *
 * The filters live in the URL rather than in component state: an auditor who has
 * narrowed to one actor over one week needs to be able to send that view to
 * somebody, and to still have it after a reload.
 */
export function AuditSearchPage() {
  const [params, setParams] = useSearchParams();

  const filters: AuditFilters = {
    actor_id: params.get('actor_id') ?? '',
    action: params.get('action') ?? '',
    entity: params.get('entity') ?? '',
    entity_id: params.get('entity_id') ?? '',
    since: params.get('since') ?? '',
    // The end date advances a day so the day itself is included rather than cut
    // off at midnight, which would silently drop everything done that day.
    until: nextDay(params.get('until') ?? ''),
    offset: Number(params.get('offset') ?? 0),
    limit: PAGE_SIZE,
  };

  const page = useAudit(filters);

  function set(key: string, value: string): void {
    const next = new URLSearchParams(params);
    if (value) next.set(key, value);
    else next.delete(key);
    if (key !== 'offset') next.delete('offset'); // A new filter starts at page one.
    setParams(next);
  }

  return (
    <div className="space-y-4">
      <Card>
        <CardBody>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
            <Field label="Actor">
              <input
                value={params.get('actor_id') ?? ''}
                placeholder="Any"
                onChange={(event) => {
                  set('actor_id', event.target.value);
                }}
              />
            </Field>

            <Field label="Action">
              <select
                value={params.get('action') ?? ''}
                onChange={(event) => {
                  set('action', event.target.value);
                }}
              >
                <option value="">Any</option>
                {KNOWN_ACTIONS.map((action) => (
                  <option key={action} value={action}>
                    {action}
                  </option>
                ))}
              </select>
            </Field>

            <Field label="Entity type">
              <input
                value={params.get('entity') ?? ''}
                placeholder="run, rubric, candidate…"
                onChange={(event) => {
                  set('entity', event.target.value);
                }}
              />
            </Field>

            <Field label="Entity id">
              <input
                value={params.get('entity_id') ?? ''}
                placeholder="Any"
                onChange={(event) => {
                  set('entity_id', event.target.value);
                }}
              />
            </Field>

            <Field label="From">
              <input
                type="date"
                value={params.get('since') ?? ''}
                onChange={(event) => {
                  set('since', event.target.value);
                }}
              />
            </Field>

            <Field label="To">
              <input
                type="date"
                value={params.get('until') ?? ''}
                onChange={(event) => {
                  set('until', event.target.value);
                }}
              />
            </Field>
          </div>

          <Button
            size="sm"
            onClick={() => {
              setParams({});
            }}
          >
            Clear filters
          </Button>
        </CardBody>
      </Card>

      <QueryState query={page} loading="Searching the log…">
        {(data) =>
          data.entries.length === 0 ? (
            <p className="py-8 text-center text-sm text-neutral-500 dark:text-neutral-400">
              No audit records match those filters.
            </p>
          ) : (
            <Card className="overflow-hidden">
              <Table caption="Audit log">
                <thead>
                  <tr>
                    <Th className="w-36">When</Th>
                    <Th className="w-32">Who</Th>
                    <Th>What</Th>
                    <Th className="w-40">Entity</Th>
                  </tr>
                </thead>
                <tbody>
                  {data.entries.map((entry, index) => (
                    <tr key={`${entry.ts}-${String(index)}`}>
                      <Td className="text-xs tabular-nums text-neutral-500 dark:text-neutral-400">
                        {dateTime(entry.ts)}
                      </Td>
                      <Td>
                        {entry.actor_id ?? (
                          <em className="text-neutral-500 dark:text-neutral-400">system</em>
                        )}
                      </Td>
                      <Td>
                        <Markdown>{describeEvent(entry.action, entry.detail)}</Markdown>
                      </Td>
                      <Td className="text-xs text-neutral-500 dark:text-neutral-400">
                        {[entry.entity, entry.entity_id].filter(Boolean).join(' ')}
                      </Td>
                    </tr>
                  ))}
                </tbody>
              </Table>

              <div className="flex items-center justify-between border-t border-neutral-200 p-3 dark:border-neutral-800">
                <Button
                  size="sm"
                  disabled={filters.offset === 0}
                  onClick={() => {
                    set('offset', String(Math.max(0, filters.offset - PAGE_SIZE)));
                  }}
                >
                  <ChevronLeft className="size-3.5" aria-hidden /> Prev
                </Button>
                <span className="text-sm text-neutral-500 dark:text-neutral-400">
                  {filters.offset + 1}–{Math.min(filters.offset + PAGE_SIZE, data.total)} of{' '}
                  {data.total}
                </span>
                <Button
                  size="sm"
                  disabled={filters.offset + PAGE_SIZE >= data.total}
                  onClick={() => {
                    set('offset', String(filters.offset + PAGE_SIZE));
                  }}
                >
                  Next <ChevronRight className="size-3.5" aria-hidden />
                </Button>
              </div>
            </Card>
          )
        }
      </QueryState>
    </div>
  );
}

function nextDay(isoDate: string): string {
  if (!isoDate) return '';
  const parsed = new Date(`${isoDate}T00:00:00Z`);
  if (Number.isNaN(parsed.getTime())) return '';
  parsed.setUTCDate(parsed.getUTCDate() + 1);
  return parsed.toISOString().slice(0, 10);
}
