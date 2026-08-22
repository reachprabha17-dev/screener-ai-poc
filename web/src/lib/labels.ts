/**
 * The words on the screen, in one place.
 *
 * These are not UI strings in the decorative sense. Each one is a decision about
 * what a reviewer is told when the system is uncertain, and several exist to stop
 * a specific way that human oversight quietly stops working — a flag that reads
 * as a judgement about the candidate, a second model presented as the right
 * answer, a count with no reason attached. They are kept together so that
 * changing one is a deliberate edit rather than a rewrite in place.
 */

import type { Band, EvidenceStatus, Verdict, VerificationStatus } from '../api/types';

/**
 * The share of candidates the design expects to need a human decision.
 *
 * Shown against the live rate on every screen that has one: oversight collapses
 * into rubber-stamping the moment the queue exceeds what a person will actually
 * read, and that failure is silent — the control still *looks* like it is working.
 */
export const ESCALATION_BUDGET = 0.03;

export const BAND_HELP: Record<Band, string> = {
  A: 'Strong match against the rubric',
  B: 'Good match, some gaps',
  C: 'Partial match',
  D: 'Weak match',
};

export const FLAG_HELP: Record<string, string> = {
  POSSIBLE_DUPLICATE:
    'Byte-identical to another file in this run. Only catches exact copies — the same CV re-exported from Word has a different hash, so absence of this flag is not evidence of no duplicate.',
  SUSPECTED_INJECTION:
    "The document contains instruction-like text. This is a prompt for a human look, not a judgement about the candidate — a security engineer's CV legitimately trips it.",
  EVIDENCE_UNVERIFIED:
    'The quoted evidence could not be matched back to the document. The system could not verify its own output, so the candidate is not ranked.',
  EVIDENCE_CONTRADICTS:
    "The model claimed support and simultaneously said there was none. The verdict was forced to 'none'.",
  BUDGET_EXCEEDED: 'Too long to judge without truncation. Nothing was truncated.',
  INPUT_REJECTED: 'Rejected before parsing — see the reason in the summary.',
  EXTRACTION_FAILED: 'No text could be read from the file, including by OCR.',
  SANITIZED_TEXT: 'Invisible or bidirectional characters were removed before judging.',
  FREETEXT_SCREENED: 'Non-job-relevant commentary was removed from the summary.',
  MISSING_MUST_HAVE: 'Does not meet a stated hard requirement.',
  VERDICT_SET_MISMATCH: 'The model did not return one verdict per criterion.',
  PARSER_TIMEOUT: 'The parser exceeded its time limit. Transient — retry.',
  PARSER_CRASHED: 'The parser failed on this file. Reported as a security event.',
  LLM_ERROR: 'The model was unreachable. Transient — retry.',
  SCHEMA_INVALID: 'The model returned malformed output. Transient — retry.',
  EVIDENCE_IRRELEVANT:
    'The quote is real and verbatim, but is not about this criterion — e.g. evidence for one skill offered to support a different one.',
  NEGATION_SUSPECTED:
    'A negation appears just before the quote. It may deny what it was offered to support rather than confirm it.',
  JUDGE_DISAGREES:
    'A second model read this quote differently from the first. Recorded as a suggestion — the original verdict was not changed.',
  UNVERIFIED_ABSENCE:
    'The first model said this was absent; a second pass found what looks like real evidence for it. Neither verdict was changed automatically.',
};

export function flagHelp(flag: string): string {
  return FLAG_HELP[flag] ?? 'See the run log.';
}

/**
 * Escalation reasons, grouped so similar cases can be worked in a batch.
 *
 * Keyed on the wire value the backend actually sends — `EscalationReason` is a
 * Python `StrEnum` whose `.value` is the upper-case member name itself (e.g.
 * `"UNVERIFIED_EVIDENCE"`, `screener/models.py`), not a lower-cased form of it.
 * Lower-case keys here would never match, silently falling through to
 * `escalationLabel`'s raw-reason fallback — which is exactly what happened
 * before this was caught: every reason rendered as its raw upper-case name.
 */
export const ESCALATION_LABELS: Record<string, string> = {
  UNVERIFIED_EVIDENCE: 'unverified evidence',
  JUDGE_DISAGREEMENT: 'judge disagreement',
  ABSENCE_FOUND: 'evidence found for an “absent” criterion',
  NEGATION: 'negation suspected',
  PARTIAL_MUST_HAVE: 'partial evidence on a must-have',
  UNPROCESSABLE: 'could not be processed',
  SUSPECTED_INJECTION: 'suspected injection',
};

export function escalationLabel(reason: string): string {
  return ESCALATION_LABELS[reason] ?? reason;
}

export const EVIDENCE_BADGE: Record<EvidenceStatus, string> = {
  verified: 'verified',
  partial: 'partially matched',
  unverified: 'not found in the resume',
  not_applicable: '',
};

export const VERIFICATION_BADGE: Record<VerificationStatus, string> = {
  done: 'verified',
  pending: 'provisional',
  skipped: 'not verified',
};

export const VERDICT_LABEL: Record<Verdict, string> = {
  strong: 'met',
  partial: 'partial',
  none: 'not met',
};

/** The actions the audit search offers as a filter. */
export const KNOWN_ACTIONS = [
  'create_position',
  'close_position',
  'extract_rubric',
  'save_rubric',
  'approve_rubric',
  'create_run',
  'start_run',
  'abort_run',
  'rescan_run',
  'advance_phase',
  'complete_run',
  'decision',
  'sign_off_run',
  'purge_candidate',
  'reclaim_orphaned',
] as const;

/**
 * The two events that record a human accepting responsibility.
 *
 * Marked so they are findable at a glance — they are what "a human was involved"
 * actually means.
 */
export const HUMAN_GATES = new Set(['approve_rubric', 'sign_off_run']);

/**
 * A detail value as text, whatever the server put in `detail_json`.
 *
 * `detail` is untyped JSON by design — a new audited action writes whatever it
 * has — so this must never be `String(value)` on an object. That renders
 * `[object Object]` in a compliance record, which reads as a bug in the audit log
 * rather than as an unexpected shape.
 */
function text(value: unknown, fallback = ''): string {
  if (value === null || value === undefined) return fallback;
  if (typeof value === 'object') return JSON.stringify(value);
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  return fallback;
}

function short(value: unknown, n = 8): string {
  const rendered = text(value);
  return rendered.length > n ? `${rendered.slice(0, n)}…` : rendered;
}

function pct(value: unknown): string {
  const number = typeof value === 'number' ? value : 0;
  return `${(number * 100).toFixed(0)}%`;
}

type Detail = Record<string, unknown>;

const EVENT_TEXT: Record<string, (d: Detail) => string> = {
  create_position: (d) => `Posted job **${text(d.reference, '?')}**.`,
  close_position: (d) => `**Closed job posting ${text(d.reference, '?')}** — no longer recruiting.`,
  extract_rubric: (d) =>
    `Model drafted a rubric of **${text(d.criteria, '?')} criteria** (prompt \`${short(d.prompt_hash)}\`, model \`${short(d.judge_digest)}\`).`,
  save_rubric: (d) =>
    `Edited the rubric → **version ${text(d.version, '?')}** (\`${short(d.rubric_hash)}\`).`,
  approve_rubric: (d) => `**Approved the rubric** (\`${short(d.rubric_hash)}\`).`,
  create_run: (d) =>
    `Created a run over \`${text(d.folder, '?')}\` — **${text(d.queued, '0')} file(s)** snapshotted.`,
  start_run: (d) => `Started screening — ${text(d.queued, '0')} file(s) queued.`,
  advance_phase: (d) => `Advanced to the **${text(d.phase, '?')}** phase.`,
  complete_run: (d) =>
    `Screening complete — ${text(d.done, '0')} judged, ${text(d.failed, '0')} failed, **${pct(d.escalation_rate)} escalated**.`,
  rescan_run: (d) => `Rescanned the folder — ${text(d.added, '0')} new file(s) added.`,
  abort_run: (d) => `Aborted the run — ${text(d.released, '0')} job(s) released.`,
  sign_off_run: () => '**Signed off the run**, accepting the results.',
  reclaim_orphaned: (d) => `Recovered ${text(d.jobs, '0')} job(s) from a stopped worker.`,
  purge_candidate: (d) => `Erased a candidate's stored data (${text(d.reason, 'no reason')}).`,
};

/**
 * Decisions get their own renderer because the reason is the point.
 *
 * That string is the adverse-action justification — the answer to "why was this
 * person rejected" — so it is never truncated and never hidden behind a toggle.
 */
function decisionText(detail: Detail): string {
  const verdict = text(detail.decision, '?').toUpperCase();
  const score = text(detail.old_score);
  const scored = score ? ` (system scored ${score})` : '';
  const reason = text(detail.reason) || '_no reason recorded_';
  return `**${verdict}**${scored} — ${reason}`;
}

/**
 * One audit row as a sentence.
 *
 * An action with no entry falls back to its raw action and detail: a newly
 * audited action must degrade to showing the row, never to a blank line on a
 * compliance screen.
 */
export function describeEvent(action: string, detail: Record<string, unknown> | null): string {
  const d = detail ?? {};
  if (action === 'decision') return decisionText(d);
  const render = EVENT_TEXT[action];
  if (!render) return `\`${action}\`${detail ? ` · \`${JSON.stringify(detail)}\`` : ''}`;
  try {
    return render(d);
  } catch {
    // A malformed detail must not blank the compliance screen.
    return `\`${action}\` · \`${JSON.stringify(d)}\``;
  }
}
