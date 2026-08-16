/**
 * The wire contract, mirrored from `screener/schemas.py`.
 *
 * Hand-written rather than generated, and deliberately so for a surface this
 * size: the response models are the API's public boundary (15.2) and a change to
 * one of them should be a decision someone makes on both sides, not a diff that
 * appears in a generated file nobody reads. Every type below names the Python
 * model it mirrors so the pair can be kept honest.
 *
 * Fields the recruiter view omits are optional here. That is not laziness about
 * the shape — `candidate_response` really does return two different objects
 * depending on the actor's roles, and code that reads an auditor-only field has
 * to prove it handled its absence.
 */

export type Verdict = 'strong' | 'partial' | 'none';
export type Band = 'A' | 'B' | 'C' | 'D';
export type Decision = 'undecided' | 'advance' | 'hold' | 'reject';
export type Support = 'supported' | 'insufficient' | 'contradicted';
export type EvidenceStatus = 'verified' | 'partial' | 'unverified' | 'not_applicable';
export type VerificationStatus = 'pending' | 'done' | 'skipped';
export type RunState = 'pending' | 'running' | 'completed' | 'empty' | 'failed' | 'aborted';
export type Phase = 'judge' | 'verify' | 'done';

/** The three outcomes a reviewer can record. */
export const DECISIONS = ['advance', 'hold', 'reject'] as const;
export type RecordableDecision = (typeof DECISIONS)[number];

/**
 * `PositionResponse`.
 *
 * `status` is carried even though the list endpoint returns open requisitions
 * only: a closed one disappearing from the list is indistinguishable from a
 * deleted one, and this screen has to be able to say which happened.
 */
export interface Position {
  id: string;
  reference: string;
  title: string;
  status: 'open' | 'closed';
  closed_at: string | null;
  created_by: string;
  created_at: string;
}

/** `FolderResponse`. Paths are relative to the resume share, never absolute. */
export interface Folder {
  name: string;
  path: string;
  file_count: number;
  has_subfolders: boolean;
}

/** `FolderPageResponse`. */
export interface FolderPage {
  folders: Folder[];
  total: number;
}

/**
 * `Criterion` — the editable unit of a rubric.
 *
 * `claim` is the criterion restated as an assertion about the candidate, which
 * the second model's support check tests directly. It is carried through the
 * editor untouched: the server's model forbids unknown fields but *defaults*
 * missing ones, so a save that dropped `claim` would silently blank the
 * hypothesis phase 2 verifies against and nothing about the result would look
 * wrong. `claim_stale` is set by the server when the text is edited without the
 * claim being regenerated, and blocks approval until it is resolved.
 */
export interface Criterion {
  id: string;
  text: string;
  claim: string;
  claim_stale: boolean;
  weight: number;
  must_have: boolean;
}

/** `RubricResponse`. `approved_at` being null is the whole gate. */
export interface Rubric {
  id: string;
  position_id: string;
  version: number;
  criteria: Criterion[];
  rubric_hash: string;
  created_by: string;
  approved_by: string | null;
  approved_at: string | null;
}

/** `RunResponse`. */
export interface Run {
  id: string;
  position_id: string;
  rubric_id: string;
  folder: string;
  status: RunState;
  judge_digest: string;
  prompt_hash: string;
  app_version: string;
  created_at: string;
  created_by: string;
  file_count: number;
  escalation_rate: number | null;
}

/** `FailedFileResponse` — the files behind the `failed` count, named. */
export interface FailedFile {
  filename: string;
  phase: string;
  attempts: number;
  last_error: string;
}

/** `RunStatusResponse`. */
export interface RunStatus {
  run_id: string;
  status: RunState;
  total: number;
  pending: number;
  claimed: number;
  done: number;
  failed: number;
  phase: Phase;
  phase_done: number;
  phase_total: number;
  escalation_breakdown: Record<string, number>;
  undecided_count: number;
  queue_depth_ahead: number;
  eta_seconds: number;
  failed_files: FailedFile[];
  escalation_rate: number;
}

/** `HighlightSpan` — offsets into `Candidate.resume_text`. */
export interface HighlightSpan {
  start: number;
  end: number;
}

/** `VerifierView` — the second model's disagreement, never presented as truth. */
export interface VerifierView {
  disagrees: boolean;
  suggested_verdict: Verdict | null;
  rationale: string;
  found_evidence: string;
  found_highlights: HighlightSpan[];
}

/** `CriterionView`. */
export interface CriterionView {
  id: string;
  text: string;
  weight: number;
  must_have: boolean;
  verdict: Verdict;
  evidence: string;
  evidence_status: EvidenceStatus;
  highlights: HighlightSpan[];
  negation_suspected: boolean;
  verifier: VerifierView | null;
}

/** `CriterionAuditView` — the same criterion with every pass that touched it. */
export interface CriterionAuditView extends CriterionView {
  model_verdict: Verdict;
  verified: boolean;
  match_ratio: number;
  longest_span: number;
  support: Support | null;
  suggested_verdict: Verdict | null;
  verifier_rationale: string;
  absence_confirmed: boolean | null;
  absence_evidence: string;
}

/** `CandidateResponse`. `score` is null — never 0 — when not scoreable. */
export interface CandidateSummary {
  id: number | null;
  filename: string;
  file_sha256: string;
  score: number | null;
  band: Band | null;
  must_haves_met: boolean;
  resume_text: string;
  criteria: CriterionView[];
  notable_strengths: string[];
  red_flags: string[];
  summary: string;
  flags: string[];
  scoreable: boolean;
  review_required: boolean;
  escalation_reasons: string[];
  verification_status: VerificationStatus;
  decision: Decision;
  decided_by: string | null;
  decided_at: string | null;
  scored_at: string;
}

/** `RankedResponse` — three groups that are never ranked against each other. */
export interface RankedCandidates {
  meets_must_haves: CandidateSummary[];
  missing_must_have: CandidateSummary[];
  needs_review: CandidateSummary[];
  escalation_rate: number;
}

/** `BulkDecisionResponse` — names the skipped ids, not just a count. */
export interface BulkDecisionResult {
  decided: number[];
  skipped: number[];
}

/** `ReviewQueueResponse` — one run's outstanding queue, named so it can be worked. */
export interface ReviewQueue {
  run_id: string;
  position_reference: string;
  position_title: string;
  awaiting_review: number;
}

/**
 * `DashboardResponse`.
 *
 * Counts only, and no field here names a person, a file or a reason — which is
 * why this is the one read surface with no role gate on it. A field that would
 * name one belongs behind the `auditor` check instead.
 */
export interface Dashboard {
  open_positions: number;
  applications: number;
  awaiting_review: number;
  runs_in_progress: number;
  unscreened_files: number;
  queues: ReviewQueue[];
}

/** `HealthResponse`. */
export interface Health {
  ok: boolean;
  llm_reachable: boolean;
  model_digest_matches_pin: boolean;
  migrations_current: boolean;
  free_disk_gb: number;
  disk_ok: boolean;
  app_version: string;
  detail: Record<string, string>;
}

/** `AuditEntryResponse`. `actor_id` is null for work no person did. */
export interface AuditEntry {
  ts: string;
  actor_id: string | null;
  action: string;
  entity: string | null;
  entity_id: string | null;
  detail: Record<string, unknown> | null;
}

/** `AuditPageResponse`. */
export interface AuditPage {
  entries: AuditEntry[];
  total: number;
}

/** `RunStoryResponse`. */
export interface RunStory {
  run_id: string;
  position_reference: string;
  rubric_version: number | null;
  approved_by: string | null;
  signed_off_by: string | null;
  events: AuditEntry[];
  separation_of_duties: boolean;
  candidate_events_truncated: boolean;
}

/** `DecisionRecordResponse` — one step of a decision's history. */
export interface DecisionRecord {
  actor_id: string;
  from_decision: string;
  to_decision: string;
  old_score: number | null;
  old_band: string | null;
  reason: string;
  at: string;
}

/** `AdverseActionResponse` — the artefact handed to a regulator. */
export interface AdverseActionRecord {
  candidate_id: number;
  filename: string;
  run_id: string;
  position_reference: string;
  decision: Decision;
  decided_by: string | null;
  decided_at: string | null;
  history: DecisionRecord[];
  score: number | null;
  band: Band | null;
  must_haves_met: boolean;
  scoreable: boolean;
  verification_status: VerificationStatus;
  criteria: CriterionAuditView[];
  flags: string[];
  escalation_reasons: string[];
  summary: string;
  rubric_version: number | null;
  rubric_hash: string;
  judge_digest: string;
  verifier_digest: string | null;
  prompt_hash: string;
  app_version: string;
  redaction_on: boolean;
  scored_at: string | null;
  rubric_approved_by: string | null;
  run_signed_off_by: string | null;
}

/** Who is acting, and with which roles. Sent as headers on every request. */
export interface Identity {
  actor: string;
  roles: string[];
}
