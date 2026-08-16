/**
 * Server state, owned by TanStack Query rather than by components.
 *
 * **Nothing here is fetched in an effect.** `useEffect` + `useState` around a
 * `fetch` is the shape that produces the two failures this screen cannot afford:
 * a request that keeps running after the reviewer navigated away, and a stale
 * candidate list rendering as if it were current. The cache is the single source
 * of truth for anything the server owns; components hold view state only.
 *
 * **Every key is scoped to the identity.** Roles change what the API returns —
 * `candidate_response` serves a recruiter view or an auditor view from the same
 * URL, and three reads answer 403 without the `auditor` role. A cache keyed
 * without the actor would hand the second person the first person's view.
 *
 * **Progress is polled, not pushed.** A job that updates every few seconds over
 * more than an hour does not justify a WebSocket (22.2). `useRunStatus` polls
 * while the run is live and stops when it is not, so a finished run left open on
 * someone's second monitor costs nothing.
 */

import {
  useMutation,
  useQuery,
  useQueryClient,
  type UseMutationResult,
  type UseQueryResult,
} from '@tanstack/react-query';
import { useMemo } from 'react';
import { useSession } from '../session/context';
import { createApi, type Api } from './client';
import type {
  AdverseActionRecord,
  AuditPage,
  BulkDecisionResult,
  CandidateSummary,
  Criterion,
  Dashboard,
  FolderPage,
  Health,
  Identity,
  Position,
  RankedCandidates,
  RecordableDecision,
  Rubric,
  Run,
  RunStatus,
  RunStory,
} from './types';

/** The API bound to the current identity. Rebuilt only when that changes. */
export function useApi(): Api {
  const { actor, roles } = useSession();
  return useMemo(() => createApi({ actor, roles }), [actor, roles]);
}

function useScope(): Identity {
  const { actor, roles } = useSession();
  return useMemo(() => ({ actor, roles }), [actor, roles]);
}

/**
 * Query keys in one place.
 *
 * Invalidation is the part that rots when keys are written inline: a mutation
 * invalidates `['runs']` while a component subscribed to `['run-list']`, and the
 * screen silently shows yesterday's data.
 */
export const keys = {
  health: (id: Identity) => ['health', id] as const,
  dashboard: (id: Identity) => ['dashboard', id] as const,
  positions: (id: Identity, includeClosed: boolean) =>
    ['positions', id, { includeClosed }] as const,
  folders: (id: Identity, path: string, q: string, offset: number) =>
    ['folders', id, path, q, offset] as const,
  latestRubric: (id: Identity, positionId: string) => ['rubric', 'latest', id, positionId] as const,
  approvedRubric: (id: Identity, positionId: string) =>
    ['rubric', 'approved', id, positionId] as const,
  runs: (id: Identity) => ['runs', id] as const,
  runStatus: (id: Identity, runId: string) => ['run-status', id, runId] as const,
  candidates: (id: Identity, runId: string) => ['candidates', id, runId] as const,
  audit: (id: Identity, filters: AuditFilters) => ['audit', id, filters] as const,
  runStory: (id: Identity, runId: string) => ['run-story', id, runId] as const,
  record: (id: Identity, candidateId: number) => ['record', id, candidateId] as const,
};

const POLL_MS = 5_000;
const FOLDER_PAGE_SIZE = 15;

// --- reads -------------------------------------------------------------------

export function useHealth(): UseQueryResult<Health> {
  const api = useApi();
  const scope = useScope();
  return useQuery({
    queryKey: keys.health(scope),
    queryFn: ({ signal }) => api.health(signal),
    // Health is a banner, not a screen. Refreshing it every half minute is
    // enough to notice a restarted API without adding traffic to every click.
    refetchInterval: 30_000,
    retry: false,
  });
}

/**
 * The overview counts.
 *
 * Polled on the same cadence as a live run, because that is when the numbers
 * move: a screening in progress changes `applications` and `awaiting_review`
 * underneath whoever is watching. `refetchOnWindowFocus` (on by default) covers
 * the ordinary case of coming back to a tab left open over lunch.
 */
export function useDashboard(): UseQueryResult<Dashboard> {
  const api = useApi();
  const scope = useScope();
  return useQuery({
    queryKey: keys.dashboard(scope),
    queryFn: ({ signal }) => api.dashboard(signal),
    refetchInterval: (query) => (query.state.data?.runs_in_progress ? POLL_MS : false),
  });
}

/**
 * Requisitions. Open ones by default; all of them where a run has to name its own.
 *
 * The flag is in the query key rather than filtered from one cached list: the
 * two answers are different responses from the server, and sharing a key would
 * mean whichever screen loaded first decided what the other one saw.
 */
export function usePositions(includeClosed = false): UseQueryResult<Position[]> {
  const api = useApi();
  const scope = useScope();
  return useQuery({
    queryKey: keys.positions(scope, includeClosed),
    queryFn: ({ signal }) => api.listPositions(includeClosed, signal),
  });
}

/** One page of the resume share. Server-side paging and filtering, deliberately:
 * counting a folder's resumes is a recursive walk, so an unbounded listing costs
 * thousands of filesystem operations on a large share. */
export function useFolders(path: string, q: string, offset: number): UseQueryResult<FolderPage> {
  const api = useApi();
  const scope = useScope();
  return useQuery({
    queryKey: keys.folders(scope, path, q, offset),
    queryFn: ({ signal }) => api.listFolders({ path, q, offset, limit: FOLDER_PAGE_SIZE }, signal),
    // The share is a network mount; keeping the previous page on screen while
    // the next one loads stops the picker flickering to empty between clicks.
    placeholderData: (previous) => previous,
  });
}

export const folderPageSize = FOLDER_PAGE_SIZE;

export function useLatestRubric(positionId: string): UseQueryResult<Rubric | null> {
  const api = useApi();
  const scope = useScope();
  return useQuery({
    queryKey: keys.latestRubric(scope, positionId),
    queryFn: ({ signal }) => api.latestRubric(positionId, signal),
  });
}

export function useApprovedRubric(positionId: string): UseQueryResult<Rubric | null> {
  const api = useApi();
  const scope = useScope();
  return useQuery({
    queryKey: keys.approvedRubric(scope, positionId),
    queryFn: ({ signal }) => api.approvedRubric(positionId, signal),
  });
}

export function useRuns(): UseQueryResult<Run[]> {
  const api = useApi();
  const scope = useScope();
  return useQuery({
    queryKey: keys.runs(scope),
    queryFn: ({ signal }) => api.listRuns(signal),
  });
}

/** A run is live while work can still change its status. */
export function isLive(status: string): boolean {
  return status === 'pending' || status === 'running';
}

export function useRunStatus(runId: string): UseQueryResult<RunStatus> {
  const api = useApi();
  const scope = useScope();
  return useQuery({
    queryKey: keys.runStatus(scope, runId),
    queryFn: ({ signal }) => api.runStatus(runId, signal),
    refetchInterval: (query) => (isLive(query.state.data?.status ?? 'pending') ? POLL_MS : false),
  });
}

export function useCandidates(runId: string): UseQueryResult<RankedCandidates> {
  const api = useApi();
  const scope = useScope();
  return useQuery({
    queryKey: keys.candidates(scope, runId),
    queryFn: ({ signal }) => api.listCandidates(runId, signal),
  });
}

export interface AuditFilters {
  actor_id: string;
  action: string;
  entity: string;
  entity_id: string;
  since: string;
  until: string;
  offset: number;
  limit: number;
}

export function useAudit(filters: AuditFilters): UseQueryResult<AuditPage> {
  const api = useApi();
  const scope = useScope();
  return useQuery({
    queryKey: keys.audit(scope, filters),
    queryFn: ({ signal }) => api.searchAudit(filters, signal),
    placeholderData: (previous) => previous,
    retry: false, // A 403 for want of the auditor role is not worth three tries.
  });
}

export function useRunStory(runId: string): UseQueryResult<RunStory> {
  const api = useApi();
  const scope = useScope();
  return useQuery({
    queryKey: keys.runStory(scope, runId),
    queryFn: ({ signal }) => api.runStory(runId, signal),
    retry: false,
  });
}

export function useAdverseActionRecord(
  candidateId: number | null,
): UseQueryResult<AdverseActionRecord> {
  const api = useApi();
  const scope = useScope();
  return useQuery({
    queryKey: keys.record(scope, candidateId ?? -1),
    queryFn: ({ signal }) => api.adverseActionRecord(candidateId ?? -1, signal),
    enabled: candidateId !== null,
    retry: false,
  });
}

// --- writes ------------------------------------------------------------------

/**
 * Invalidate rather than patch the cache by hand.
 *
 * Optimistic edits are the wrong trade on this screen: the server rejects
 * decisions on escalated candidates, refuses sign-off with an unread queue, and
 * versions a rubric on save. A cache written from what the client *hoped*
 * happened would show a reviewer an outcome the system did not record.
 */
export function useCreatePosition(): UseMutationResult<
  Position,
  Error,
  { reference: string; title: string; jd_text: string }
> {
  const api = useApi();
  const scope = useScope();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input) => api.createPosition(input),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['positions', scope] });
      void client.invalidateQueries({ queryKey: keys.dashboard(scope) });
    },
  });
}

/**
 * Closing a requisition removes it from every list that reads `/positions`, so
 * the position list, the dashboard's count and any screen resolving a run's
 * requisition label all have to be refetched together.
 */
export function useClosePosition(): UseMutationResult<Position, Error, string> {
  const api = useApi();
  const scope = useScope();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (positionId) => api.closePosition(positionId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['positions', scope] });
      void client.invalidateQueries({ queryKey: keys.dashboard(scope) });
    },
  });
}

export function useExtractRubric(positionId: string): UseMutationResult<Rubric, Error, void> {
  const api = useApi();
  const scope = useScope();
  const client = useQueryClient();
  return useMutation({
    mutationFn: () => api.extractRubric(positionId),
    onSuccess: (rubric) => {
      client.setQueryData(keys.latestRubric(scope, positionId), rubric);
    },
  });
}

export function useSaveRubric(
  positionId: string,
): UseMutationResult<Rubric, Error, { criteria: Criterion[]; baseVersion: number | null }> {
  const api = useApi();
  const scope = useScope();
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ criteria, baseVersion }) => api.saveRubric(positionId, criteria, baseVersion),
    onSuccess: (rubric) => {
      client.setQueryData(keys.latestRubric(scope, positionId), rubric);
      void client.invalidateQueries({ queryKey: keys.approvedRubric(scope, positionId) });
    },
  });
}

export function useApproveRubric(positionId: string): UseMutationResult<Rubric, Error, string> {
  const api = useApi();
  const scope = useScope();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (rubricId) => api.approveRubric(rubricId),
    onSuccess: (rubric) => {
      client.setQueryData(keys.latestRubric(scope, positionId), rubric);
      void client.invalidateQueries({ queryKey: keys.approvedRubric(scope, positionId) });
    },
  });
}

export function useCreateRun(): UseMutationResult<
  Run,
  Error,
  { position_id: string; rubric_id: string }
> {
  const api = useApi();
  const scope = useScope();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input) => api.createRun(input),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.runs(scope) });
      void client.invalidateQueries({ queryKey: keys.dashboard(scope) });
    },
  });
}

type RunAction = 'start' | 'rescan' | 'abort';

/**
 * The three run controls as one mutation.
 *
 * They differ only in the verb and all three invalidate the same two queries;
 * three near-identical hooks would be three places to forget one of them.
 */
export function useRunControl(runId: string): UseMutationResult<number, Error, RunAction> {
  const api = useApi();
  const scope = useScope();
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (action: RunAction) => {
      if (action === 'start') return (await api.startRun(runId)).count;
      if (action === 'rescan') return (await api.rescanRun(runId)).count;
      await api.abortRun(runId);
      return 0;
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.runStatus(scope, runId) });
      void client.invalidateQueries({ queryKey: keys.runs(scope) });
      void client.invalidateQueries({ queryKey: keys.dashboard(scope) });
    },
  });
}

export function useDecide(
  runId: string,
): UseMutationResult<
  void,
  Error,
  { candidateId: number; decision: RecordableDecision; reason: string }
> {
  const api = useApi();
  const scope = useScope();
  const client = useQueryClient();
  return useMutation({
    mutationFn: async (input) => {
      await api.decide(input);
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.candidates(scope, runId) });
      void client.invalidateQueries({ queryKey: keys.runStatus(scope, runId) });
      // A decision empties part of the review queue the dashboard is counting.
      void client.invalidateQueries({ queryKey: keys.dashboard(scope) });
    },
  });
}

export function useDecideBulk(
  runId: string,
): UseMutationResult<
  BulkDecisionResult,
  Error,
  { candidate_ids: number[]; decision: RecordableDecision; reason: string }
> {
  const api = useApi();
  const scope = useScope();
  const client = useQueryClient();
  return useMutation({
    mutationFn: (input) => api.decideBulk(input),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.candidates(scope, runId) });
      void client.invalidateQueries({ queryKey: keys.runStatus(scope, runId) });
      // A decision empties part of the review queue the dashboard is counting.
      void client.invalidateQueries({ queryKey: keys.dashboard(scope) });
    },
  });
}

export function useSignOff(runId: string): UseMutationResult<void, Error, void> {
  const api = useApi();
  const scope = useScope();
  const client = useQueryClient();
  return useMutation({
    mutationFn: async () => {
      await api.signOff(runId);
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.runs(scope) });
      void client.invalidateQueries({ queryKey: keys.runStatus(scope, runId) });
      void client.invalidateQueries({ queryKey: keys.runStory(scope, runId) });
    },
  });
}

/** Everyone in a run, in the order a reviewer should meet them. */
export function everyone(result: RankedCandidates | undefined): CandidateSummary[] {
  if (!result) return [];
  return [...result.needs_review, ...result.meets_must_haves, ...result.missing_must_have];
}
