/**
 * The review screen's load-bearing behaviours, not its markup.
 *
 * Each test below corresponds to a way human oversight stops working: an
 * escalated candidate that is not the first thing on screen, a score rendered
 * with a precision the system cannot justify, and a sign-off button that works
 * while the queue is unread.
 */

import { screen, waitFor } from '@testing-library/react';
import { Route, Routes } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { CandidateSummary, Position, RankedCandidates, Run } from '../api/types';
import type * as Format from '../lib/format';
import { renderWithProviders } from '../test/utils';
import { ReviewPage } from './ReviewPage';

// Only `downloadCsv` is replaced. `toCsv` stays real, because the quoting is
// half of what the assertions below are checking.
const { downloadCsv } = vi.hoisted(() => ({
  downloadCsv: vi.fn<(filename: string, csv: string) => void>(),
}));

vi.mock('../lib/format', async (importOriginal) => ({
  ...(await importOriginal<typeof Format>()),
  downloadCsv,
}));

function candidate(overrides: Partial<CandidateSummary> = {}): CandidateSummary {
  return {
    id: 1,
    filename: 'ada-lovelace.pdf',
    file_sha256: 'sha-1',
    candidate_name: 'Ada Lovelace',
    email: 'ada@example.com',
    phone: '+44 20 7946 0958',
    score: 7.84,
    band: 'A',
    must_haves_met: true,
    resume_text: '',
    criteria: [],
    notable_strengths: [],
    red_flags: [],
    summary: '',
    flags: [],
    scoreable: true,
    review_required: false,
    injection_findings: [],
    escalation_reasons: [],
    verification_status: 'done',
    decision: 'undecided',
    decided_by: null,
    decided_at: null,
    scored_at: '2026-05-04T10:00:00Z',
    ...overrides,
  };
}

const POSITION = { id: 'pos-1', title: 'Rolling Stock Technician' } as Position;
const RUN = { id: 'run-1', position_id: 'pos-1' } as Run;

/**
 * Routed by URL, not one body for every request.
 *
 * The page reads three endpoints — its candidates, plus the run and position
 * lists it needs to name the requisition in the export. A single blind handler
 * answers `/positions` with a ranked-candidates object, and the `.find` over it
 * throws inside a render rather than failing an assertion.
 */
function mockApi(result: Partial<RankedCandidates>, positions: Position[] = [POSITION]): void {
  const candidates: RankedCandidates = {
    meets_must_haves: [],
    missing_must_have: [],
    needs_review: [],
    escalation_rate: 0,
    ...result,
  };

  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(async (input) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      const forCandidates = url.includes('/candidates');
      const body = forCandidates
        ? candidates
        : url.startsWith('/positions')
          ? positions
          : url.startsWith('/runs')
            ? [RUN]
            : null;

      if (body === null) return new Response('unexpected request', { status: 404 });

      // The candidate list lands last, so the table — and the export button with
      // it — appears on a page whose requisition has already resolved. That is
      // the real order: `RunLayout` fetches both lists before this page mounts,
      // and without it a test would be racing its own fixtures.
      if (forCandidates) await new Promise((resolve) => setTimeout(resolve, 0));

      return new Response(JSON.stringify(body), { status: 200 });
    }),
  );
}

function renderReview() {
  return renderWithProviders(
    <Routes>
      <Route path="/runs/:runId/review" element={<ReviewPage />} />
    </Routes>,
    { route: '/runs/run-1/review' },
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
  downloadCsv.mockClear();
});

describe('review screen', () => {
  it('opens the needs-review group and leaves the ranked ones closed', async () => {
    // At 1,000 applicants a reviewer reads the top of Band A and stops. An
    // escalated candidate anywhere else is invisible in practice (10.5b).
    mockApi({
      needs_review: [
        candidate({
          filename: 'escalated.pdf',
          file_sha256: 'sha-esc',
          band: null,
          score: null,
          scoreable: false,
          review_required: true,
          escalation_reasons: ['UNVERIFIED_EVIDENCE'],
        }),
      ],
      meets_must_haves: [candidate({ filename: 'ranked.pdf', file_sha256: 'sha-ok' })],
      escalation_rate: 0.5,
    });

    renderReview();

    const escalated = await screen.findByRole('button', { name: 'escalated.pdf' });
    const ranked = screen.getByRole('button', { name: 'ranked.pdf' });

    // `<details>` keeps its content in the DOM; `open` is what a reviewer sees.
    expect(escalated.closest('details')?.open).toBe(true);
    expect(ranked.closest('details')?.open).toBe(false);
  });

  it('names the escalation reasons rather than showing one count', async () => {
    mockApi({
      needs_review: [
        candidate({ file_sha256: 'a', escalation_reasons: ['JUDGE_DISAGREEMENT'] }),
        candidate({ file_sha256: 'b', escalation_reasons: ['JUDGE_DISAGREEMENT'] }),
        candidate({ file_sha256: 'c', escalation_reasons: ['NEGATION'] }),
      ],
      escalation_rate: 0.2,
    });

    renderReview();

    expect(await screen.findByText(/judge disagreement/)).toBeInTheDocument();
    expect(screen.getByText(/negation suspected/)).toBeInTheDocument();
  });

  it('shows the raw count behind the escalation rate, not only the percentage', async () => {
    mockApi({
      meets_must_haves: [candidate({ file_sha256: 'a' }), candidate({ file_sha256: 'b' })],
      needs_review: [candidate({ file_sha256: 'c' }), candidate({ file_sha256: 'd' })],
      escalation_rate: 0.5,
    });

    renderReview();

    expect(await screen.findByText('50%')).toBeInTheDocument();
    expect(screen.getByText('(2 of 4)')).toBeInTheDocument();
  });

  it('highlights a row that needs a human decision, even inside a ranked group', async () => {
    // `review_required` can be true on a candidate placed in `meets_must_haves`
    // (a partial must-have, say) — the highlight cannot depend on which group
    // rendered the row.
    mockApi({
      meets_must_haves: [
        candidate({ filename: 'needs-a-look.pdf', file_sha256: 'sha-look', review_required: true }),
        candidate({ filename: 'clean.pdf', file_sha256: 'sha-clean' }),
      ],
      escalation_rate: 0.01,
    });

    renderReview();

    const flagged = await screen.findByRole('button', { name: 'needs-a-look.pdf' });
    const clean = screen.getByRole('button', { name: 'clean.pdf' });

    expect(flagged.closest('tr')?.className).toMatch(/bg-amber-50/);
    expect(clean.closest('tr')?.className).not.toMatch(/bg-amber-50/);
  });

  it('warns when the escalation rate is over the design budget', async () => {
    mockApi({ meets_must_haves: [candidate()], escalation_rate: 0.2 });

    renderReview();

    expect(await screen.findByText(/design budget/)).toBeInTheDocument();
  });

  it('shows a band and never the raw score in the list', async () => {
    // Three verdict levels across twelve criteria cannot support a rendered
    // precision of 7.8 (10.6).
    mockApi({ meets_must_haves: [candidate({ score: 7.84, band: 'A' })] });

    renderReview();

    await screen.findByRole('button', { name: 'ada-lovelace.pdf' });
    expect(screen.queryByText(/7\.84/)).not.toBeInTheDocument();
    expect(screen.getByTitle('Strong match against the rubric')).toHaveTextContent('A');
  });

  it('blocks sign-off while an escalated candidate is undecided', async () => {
    mockApi({
      needs_review: [candidate({ review_required: true, decision: 'undecided' })],
      escalation_rate: 0.01,
    });

    renderReview();

    const button = await screen.findByRole('button', { name: /sign off this run/i });
    expect(button).toBeDisabled();
    expect(screen.getByText(/still need a decision/)).toBeInTheDocument();
  });

  it('allows sign-off once every escalated candidate has been decided', async () => {
    mockApi({
      needs_review: [candidate({ review_required: true, decision: 'reject', decided_by: 'me' })],
      escalation_rate: 0.01,
    });

    renderReview();

    await waitFor(() => {
      expect(screen.getByRole('button', { name: /sign off this run/i })).toBeEnabled();
    });
  });

  it('explains why the bulk box is absent when every candidate needs individual review', async () => {
    // Silence here used to look identical to a broken control — a reviewer
    // opening the group and finding nothing below the table, with no way to
    // tell "nothing to bulk-act on" from "this is broken" (11.11).
    mockApi({
      missing_must_have: [
        candidate({
          filename: 'ravi-kumar.docx',
          file_sha256: 'sha-ravi',
          must_haves_met: false,
          review_required: true,
          escalation_reasons: ['SUSPECTED_INJECTION'],
        }),
      ],
      escalation_rate: 0.5,
    });

    renderReview();

    await screen.findByRole('button', { name: 'ravi-kumar.docx' });
    expect(screen.queryByRole('button', { name: /decide on all/i })).not.toBeInTheDocument();
    expect(
      await screen.findByText(/every candidate in this group needs review/i),
    ).toBeInTheDocument();
  });

  it('exports the candidate and the requisition, not only the file', async () => {
    // The sheet used to identify a file: filename, hash, band, score. Acting on
    // it meant reopening every resume to find a phone number, and nothing in
    // `run-1-qualified.csv` said which job had been screened.
    mockApi({
      meets_must_haves: [
        candidate({
          candidate_name: 'Geofrey Semakula',
          email: 'geofrey@example.com',
          phone: '+971 50 123 4567',
          filename: '84637_GEOFREY_SEMAKULA_resume.pdf',
        }),
      ],
    });

    renderReview();

    const button = await screen.findByRole('button', { name: /export csv/i });
    button.click();

    await waitFor(() => {
      expect(downloadCsv).toHaveBeenCalledOnce();
    });

    expect(downloadCsv).toHaveBeenCalledWith(
      'run-1-qualified.csv',
      expect.stringContaining(
        'position_title,candidate_name,email,phone,filename,file_sha256,band',
      ) as string,
    );
    expect(downloadCsv).toHaveBeenCalledWith(
      'run-1-qualified.csv',
      expect.stringContaining(
        '\nRolling Stock Technician,Geofrey Semakula,geofrey@example.com,+971 50 123 4567,84637_GEOFREY_SEMAKULA_resume.pdf,',
      ) as string,
    );
  });

  it('still exports when the requisition cannot be resolved', async () => {
    // A run outlives its post, and a lookup the reviewer did not ask for must
    // not be what stops them getting their sheet.
    mockApi({ meets_must_haves: [candidate()] }, []);

    renderReview();

    const button = await screen.findByRole('button', { name: /export csv/i });
    button.click();

    await waitFor(() => {
      expect(downloadCsv).toHaveBeenCalledOnce();
    });

    // A leading empty cell, and every other column still filled.
    expect(downloadCsv).toHaveBeenCalledWith(
      'run-1-qualified.csv',
      expect.stringContaining('\n,Ada Lovelace,ada@example.com,') as string,
    );
  });

  it('still shows the individual decision box for an escalated missing-must-have candidate', async () => {
    mockApi({
      missing_must_have: [
        candidate({
          filename: 'ravi-kumar.docx',
          file_sha256: 'sha-ravi',
          must_haves_met: false,
          review_required: true,
          escalation_reasons: ['SUSPECTED_INJECTION'],
        }),
      ],
      escalation_rate: 0.5,
    });

    renderReview();

    const row = await screen.findByRole('button', { name: 'ravi-kumar.docx' });
    row.click();

    await screen.findByText('Your decision');
    expect(screen.getAllByRole('radio')).toHaveLength(3);
    screen.getByRole('textbox', { name: /reason/i });
    screen.getByRole('button', { name: /record decision/i });
  });
});
