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
import type { CandidateSummary, RankedCandidates } from '../api/types';
import { renderWithProviders } from '../test/utils';
import { ReviewPage } from './ReviewPage';

function candidate(overrides: Partial<CandidateSummary> = {}): CandidateSummary {
  return {
    id: 1,
    filename: 'ada-lovelace.pdf',
    file_sha256: 'sha-1',
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
    escalation_reasons: [],
    verification_status: 'done',
    decision: 'undecided',
    decided_by: null,
    decided_at: null,
    scored_at: '2026-05-04T10:00:00Z',
    ...overrides,
  };
}

function mockCandidates(result: Partial<RankedCandidates>): void {
  const body: RankedCandidates = {
    meets_must_haves: [],
    missing_must_have: [],
    needs_review: [],
    escalation_rate: 0,
    ...result,
  };
  vi.stubGlobal(
    'fetch',
    vi.fn<typeof fetch>(() => Promise.resolve(new Response(JSON.stringify(body), { status: 200 }))),
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
});

describe('review screen', () => {
  it('opens the needs-review group and leaves the ranked ones closed', async () => {
    // At 1,000 applicants a reviewer reads the top of Band A and stops. An
    // escalated candidate anywhere else is invisible in practice (10.5b).
    mockCandidates({
      needs_review: [
        candidate({
          filename: 'escalated.pdf',
          file_sha256: 'sha-esc',
          band: null,
          score: null,
          scoreable: false,
          review_required: true,
          escalation_reasons: ['unverified_evidence'],
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
    mockCandidates({
      needs_review: [
        candidate({ file_sha256: 'a', escalation_reasons: ['judge_disagreement'] }),
        candidate({ file_sha256: 'b', escalation_reasons: ['judge_disagreement'] }),
        candidate({ file_sha256: 'c', escalation_reasons: ['negation'] }),
      ],
      escalation_rate: 0.2,
    });

    renderReview();

    expect(await screen.findByText(/judge disagreement/)).toBeInTheDocument();
    expect(screen.getByText(/negation suspected/)).toBeInTheDocument();
  });

  it('warns when the escalation rate is over the design budget', async () => {
    mockCandidates({ meets_must_haves: [candidate()], escalation_rate: 0.2 });

    renderReview();

    expect(await screen.findByText(/design budget/)).toBeInTheDocument();
  });

  it('shows a band and never the raw score in the list', async () => {
    // Three verdict levels across twelve criteria cannot support a rendered
    // precision of 7.8 (10.6).
    mockCandidates({ meets_must_haves: [candidate({ score: 7.84, band: 'A' })] });

    renderReview();

    await screen.findByRole('button', { name: 'ada-lovelace.pdf' });
    expect(screen.queryByText(/7\.84/)).not.toBeInTheDocument();
    expect(screen.getByTitle('Strong match against the rubric')).toHaveTextContent('A');
  });

  it('blocks sign-off while an escalated candidate is undecided', async () => {
    mockCandidates({
      needs_review: [candidate({ review_required: true, decision: 'undecided' })],
      escalation_rate: 0.01,
    });

    renderReview();

    const button = await screen.findByRole('button', { name: /sign off this run/i });
    expect(button).toBeDisabled();
    expect(screen.getByText(/still need a decision/)).toBeInTheDocument();
  });

  it('allows sign-off once every escalated candidate has been decided', async () => {
    mockCandidates({
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
    mockCandidates({
      missing_must_have: [
        candidate({
          filename: 'ravi-kumar.docx',
          file_sha256: 'sha-ravi',
          must_haves_met: false,
          review_required: true,
          escalation_reasons: ['suspected_injection'],
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

  it('still shows the individual decision box for an escalated missing-must-have candidate', async () => {
    mockCandidates({
      missing_must_have: [
        candidate({
          filename: 'ravi-kumar.docx',
          file_sha256: 'sha-ravi',
          must_haves_met: false,
          review_required: true,
          escalation_reasons: ['suspected_injection'],
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
