import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { Position } from '../api/types';
import { renderWithProviders } from '../test/utils';
import { ClosePositionButton } from './ClosePositionButton';

const OPEN: Position = {
  id: 'pos-1',
  reference: 'ENG-114',
  title: 'Platform Engineer',
  status: 'open',
  closed_at: null,
  created_by: 'someone',
  created_at: '2026-05-01T09:00:00Z',
};

function mockClose(response: { status: number; body: unknown }) {
  const mock = vi.fn<typeof fetch>(() =>
    Promise.resolve(new Response(JSON.stringify(response.body), { status: response.status })),
  );
  vi.stubGlobal('fetch', mock);
  return mock;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('closing a requisition', () => {
  it('confirms first, and says what is kept', async () => {
    // "Close" beside a list of candidates reads like a delete. A reviewer who
    // believes it destroys an adverse-action record will never press it.
    mockClose({ status: 200, body: { ...OPEN, status: 'closed' } });
    const user = userEvent.setup();

    renderWithProviders(<ClosePositionButton position={OPEN} />);
    await user.click(screen.getByRole('button', { name: /close requisition/i }));

    expect(await screen.findByText(/Close ENG-114\?/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing is deleted/)).toBeInTheDocument();
    // The other half of the reassurance: closing is administrative, and work
    // already under way is not thrown away.
    expect(screen.getByText(/carry on/)).toBeInTheDocument();
  });

  it('does not call the API until the confirmation is accepted', async () => {
    const fetchMock = mockClose({ status: 200, body: { ...OPEN, status: 'closed' } });
    const user = userEvent.setup();

    renderWithProviders(<ClosePositionButton position={OPEN} />);
    await user.click(screen.getByRole('button', { name: /close requisition/i }));
    await screen.findByText(/Close ENG-114\?/);

    expect(fetchMock).not.toHaveBeenCalled();

    await user.click(screen.getByRole('button', { name: 'Close it' }));

    expect(fetchMock.mock.calls[0]?.[0]).toBe('/positions/pos-1/close');
    expect(fetchMock.mock.calls[0]?.[1]?.method).toBe('POST');
  });

  it('keeps a refusal on screen rather than closing the panel', async () => {
    // Whatever the server refuses for, the reason is the actionable part and it
    // belongs next to the button that produced it.
    mockClose({
      status: 404,
      body: { detail: 'Not found — it may have been deleted, or the id is wrong.' },
    });
    const user = userEvent.setup();

    renderWithProviders(<ClosePositionButton position={OPEN} />);
    await user.click(screen.getByRole('button', { name: /close requisition/i }));
    await user.click(await screen.findByRole('button', { name: 'Close it' }));

    expect(await screen.findByText(/may have been deleted/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Close it' })).toBeInTheDocument();
  });

  it('offers nothing on a requisition that is already closed', () => {
    renderWithProviders(
      <ClosePositionButton position={{ ...OPEN, status: 'closed', closed_at: '2026-05-02' }} />,
    );

    expect(screen.queryByRole('button', { name: /close requisition/i })).not.toBeInTheDocument();
  });
});
