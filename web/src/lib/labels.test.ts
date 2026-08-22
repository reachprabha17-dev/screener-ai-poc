import { describe, expect, it } from 'vitest';
import { describeEvent, escalationLabel, flagHelp } from './labels';

describe('describeEvent', () => {
  it('renders a known action as a sentence', () => {
    expect(describeEvent('create_run', { folder: 'ENG-114', queued: 42 })).toBe(
      'Created a run over `ENG-114` — **42 file(s)** snapshotted.',
    );
  });

  it('never truncates a decision reason — it is the adverse-action record', () => {
    const reason = 'Rejected: no evidence of the Kubernetes must-have anywhere in the document.';
    expect(describeEvent('decision', { decision: 'reject', old_score: 4.2, reason })).toContain(
      reason,
    );
  });

  it('says so when no reason was recorded, rather than rendering an empty sentence', () => {
    expect(describeEvent('decision', { decision: 'advance' })).toContain('_no reason recorded_');
  });

  it('degrades an unknown action to its raw row instead of blanking the screen', () => {
    // A newly audited action must appear here as data until it earns a sentence.
    const rendered = describeEvent('quarantine_file', { path: 'x.pdf' });
    expect(rendered).toContain('quarantine_file');
    expect(rendered).toContain('x.pdf');
  });

  it('survives a detail of the wrong shape', () => {
    // `detail_json` is untyped by design. `[object Object]` in a compliance
    // record reads as a bug in the audit log rather than an unexpected shape.
    const rendered = describeEvent('create_position', { reference: { nested: true } });
    expect(rendered).not.toContain('[object Object]');
    expect(rendered).toContain('nested');
  });

  it('handles a missing detail', () => {
    expect(describeEvent('sign_off_run', null)).toContain('Signed off the run');
  });
});

describe('reviewer-facing vocabulary', () => {
  it('explains a flag rather than showing only its constant name', () => {
    expect(flagHelp('SUSPECTED_INJECTION')).toMatch(/not a judgement about the candidate/);
  });

  it('falls back to the run log for a flag it does not know', () => {
    expect(flagHelp('SOMETHING_NEW')).toBe('See the run log.');
  });

  it('explains every flag the backend can actually send, not just the common ones', () => {
    // Regression: EVIDENCE_IRRELEVANT, NEGATION_SUSPECTED, JUDGE_DISAGREES and
    // UNVERIFIED_ABSENCE had no entry and silently fell back to "See the run
    // log." — a real `Flag` reading as an unhandled one.
    for (const flag of [
      'EVIDENCE_IRRELEVANT',
      'NEGATION_SUSPECTED',
      'JUDGE_DISAGREES',
      'UNVERIFIED_ABSENCE',
    ]) {
      expect(flagHelp(flag)).not.toBe('See the run log.');
    }
  });

  it('spells out an escalation reason', () => {
    // Regression: `EscalationReason` is a Python StrEnum whose wire value is
    // the upper-case member name itself — the backend sends "JUDGE_DISAGREEMENT",
    // never "judge_disagreement". A lower-case lookup key here matched nothing
    // and silently fell through to the raw reason on every real response.
    expect(escalationLabel('JUDGE_DISAGREEMENT')).toBe('judge disagreement');
  });

  it('falls back to the raw reason for one it does not know', () => {
    expect(escalationLabel('SOMETHING_NEW')).toBe('SOMETHING_NEW');
  });
});
