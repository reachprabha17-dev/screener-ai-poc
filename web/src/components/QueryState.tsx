import type { UseQueryResult } from '@tanstack/react-query';
import { Loader2 } from 'lucide-react';
import type { ReactElement } from 'react';
import { Alert } from '../ui/Alert';
import { Button } from '../ui/Button';

/**
 * Loading and failure, handled once.
 *
 * Every screen here reads from the API, so every screen has the same three
 * states. Written inline they drift: one page renders a spinner, another renders
 * nothing, a third renders a half-built table from `undefined`. The render-prop
 * form also makes the success case take non-nullable data, so TypeScript refuses
 * the "it will be there by then" assumption that produces a blank panel in front
 * of a reviewer.
 */
export function QueryState<T>({
  query,
  children,
  loading = 'Loading…',
}: {
  query: UseQueryResult<T>;
  children: (data: T) => ReactElement | null;
  loading?: string;
}): ReactElement | null {
  if (query.isPending) {
    return (
      <p
        className="flex items-center gap-2 text-sm text-neutral-500 dark:text-neutral-400"
        aria-live="polite"
      >
        <Loader2 className="size-4 animate-spin" aria-hidden />
        {loading}
      </p>
    );
  }

  if (query.isError) {
    return (
      <Alert tone="error" title="That did not work">
        <p>{query.error.message}</p>
        <Button size="sm" onClick={() => void query.refetch()}>
          Try again
        </Button>
      </Alert>
    );
  }

  return children(query.data);
}
