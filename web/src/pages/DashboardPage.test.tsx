import { screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Dashboard } from '../api/types';
import { renderWithProviders } from '../test/utils';
import { DashboardPage } from './DashboardPage';

function mockDashboard(overrides: Partial<Dashboard> = {}): void {
  const body: Dashboard = {
    open_positions: 0,
    applications: 0,
    awaiting_review: 0,
    runs_in_progress: 0,
    unscreened_files: 0,
    queues: [],
    ...overrides,
  };
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('dashboard', () => {
  it('shows the three counts', async () => {
    mockDashboard({ open_positions: 4, applications: 128, awaiting_review: 31 });

    renderWithProviders(<DashboardPage />);

    expect(await screen.findByText('4')).toBeInTheDocument();
    expect(screen.getByText('128')).toBeInTheDocument();
    expect(screen.getByText('31')).toBeInTheDocument();
  });

  it('asks for the counts once rather than assembling them client-side', async () => {
    // Reading /runs and then a candidate list per run would download every
    // résumé in the database to produce three integers.
    mockDashboard({ open_positions: 1 });

    renderWithProviders(<DashboardPage />);
    await screen.findByText('1');

    const paths = vi
      .mocked(fetch)
      .mock.calls.map(([input]) => (typeof input === 'string' ? input : ''));
    expect(paths).toEqual(['/dashboard']);
  });

  it('names the runs holding the queue, because review happens inside a run', async () => {
    mockDashboard({
      awaiting_review: 9,
      queues: [
        {
          run_id: 'run-77',
          position_reference: 'ENG-114',
          position_title: 'Platform Engineer',
          awaiting_review: 9,
        },
      ],
    });

    renderWithProviders(<DashboardPage />);

    expect(await screen.findByRole('link', { name: 'run-77' })).toHaveAttribute(
      'href',
      '/runs/run-77/review',
    );
    expect(screen.getByText('ENG-114')).toBeInTheDocument();
  });

  it('says how many are missing when the queue table is capped', async () => {
    // The table is bounded server-side and the headline count is not. Silently
    // showing a subset would make the two numbers contradict each other.
    mockDashboard({
      awaiting_review: 40,
      queues: [
        { run_id: 'run-1', position_reference: 'A', position_title: '', awaiting_review: 25 },
      ],
    });

    renderWithProviders(<DashboardPage />);

    expect(await screen.findByText(/15 more candidates are waiting/)).toBeInTheDocument();
  });

  it('reads as empty rather than broken on a system with nothing in it', async () => {
    mockDashboard();

    renderWithProviders(<DashboardPage />);

    expect(await screen.findByText(/Nothing has been screened yet/)).toBeInTheDocument();
    expect(screen.getByText('No requisitions raised yet.')).toBeInTheDocument();
    expect(screen.getByText('Nothing is waiting on a person')).toBeInTheDocument();
  });

  it('warns that the numbers are moving while a run is in progress', async () => {
    mockDashboard({ open_positions: 1, runs_in_progress: 2, unscreened_files: 300 });

    renderWithProviders(<DashboardPage />);

    expect(await screen.findByText(/2 runs in progress/)).toBeInTheDocument();
    expect(screen.getByText('300 more waiting to be screened')).toBeInTheDocument();
  });
});
