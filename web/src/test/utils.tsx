import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, type RenderResult } from '@testing-library/react';
import type { ReactElement } from 'react';
import { MemoryRouter } from 'react-router-dom';
import { SessionProvider } from '../session/SessionProvider';

/**
 * Render a component with the three providers the app always has.
 *
 * Retries off and no caching between tests: a retried failure turns a one-line
 * assertion into a five-second timeout, and a cache shared across tests makes the
 * order they run in significant.
 */
export function renderWithProviders(
  ui: ReactElement,
  { route = '/' }: { route?: string } = {},
): RenderResult {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0, staleTime: 0 } },
  });

  return render(
    <QueryClientProvider client={client}>
      <SessionProvider>
        <MemoryRouter initialEntries={[route]}>{ui}</MemoryRouter>
      </SessionProvider>
    </QueryClientProvider>,
  );
}
