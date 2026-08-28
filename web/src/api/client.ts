/**
 * The only module in this app that performs network I/O (spec 2, decision #11).
 *
 * **The UI is an HTTP client and nothing else.** There is no database driver and
 * no shared domain code with the server; the boundary is a process boundary. In
 * the Streamlit implementation that rule was enforced by `tests/test_layering.py`
 * asserting no `ui/` module imported `sqlite3` or `screener.*`. The same test now
 * asserts that no module outside `src/api/` calls `fetch`, so every request in the
 * app passes through the error handling below rather than around it.
 *
 * **Every call can fail, and none of them may raise a raw failure at a reviewer.**
 * The API is a separate process that gets restarted, and `TypeError: Failed to
 * fetch` in the middle of a screen is both useless to a recruiter and
 * indistinguishable from a bug in the screening itself. Failures come back as
 * `ApiError` with a sentence a person can act on.
 *
 * **Same-origin, always.** The bundle is served by the API process at `/ui/`, so
 * requests go to the process that served the page. There is no configurable API
 * host: an operator who could point this at another machine could point candidate
 * data at another machine.
 */

import type {
  AdverseActionRecord,
  AppConfig,
  AuditPage,
  BulkDecisionResult,
  Criterion,
  Dashboard,
  FolderPage,
  Health,
  Identity,
  JdDocument,
  JdSource,
  Position,
  RankedCandidates,
  RecordableDecision,
  Rubric,
  Run,
  RunStatus,
  RunStory,
} from './types';

/** Something the user needs told, phrased for a person rather than a log. */
export class ApiError extends Error {
  readonly status: number;

  constructor(message: string, status = 0) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

const DEFAULT_TIMEOUT_MS = 30_000;

/**
 * Rubric extraction is a live LLM call — ~5 s typical, but a cold model load can
 * take considerably longer, and timing out mid-generation looks like a failure
 * when it was only slow.
 */
const EXTRACT_TIMEOUT_MS = 180_000;

/**
 * Reading a document is bounded server-side by `jd_parse_timeout_s`, which is
 * seconds rather than minutes. This only has to outlast that plus the upload
 * itself — a much shorter wait than a cold model load, and one where failing
 * fast is the kinder behaviour because the paste box is right there.
 */
const UPLOAD_TIMEOUT_MS = 60_000;

interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'DELETE';
  /** `FormData` is sent as-is; anything else is JSON-encoded. */
  body?: unknown;
  timeoutMs?: number;
  /** The query's cancellation signal, so an abandoned page stops fetching. */
  signal?: AbortSignal | undefined;
}

/**
 * Identity, and roles only when the operator has chosen some.
 *
 * Omitting `X-Actor-Roles` entirely is not the same as sending it empty: the API
 * reads an absent header as the stub's default role set, and an empty one as
 * "this actor has no roles at all" — which would lock the operator out of every
 * role-scoped read.
 */
function headers(identity: Identity, body: unknown): HeadersInit {
  const result: Record<string, string> = { 'X-Actor': identity.actor };
  if (identity.roles.length > 0) result['X-Actor-Roles'] = identity.roles.join(',');
  // `FormData` is the one body we must NOT type ourselves: the browser has to
  // set `multipart/form-data` *with its generated boundary*, and a header we
  // wrote would replace it with one that has no boundary at all. The server
  // then cannot split the parts and answers 422 for a request that was fine.
  if (body !== undefined && !(body instanceof FormData)) {
    result['Content-Type'] = 'application/json';
  }
  return result;
}

/**
 * Turn an HTTP error into something a recruiter can act on.
 *
 * A 409 in particular has one cause in this system and a clear next step, and
 * saying so beats echoing a status code at someone who is trying to fill a
 * vacancy.
 */
async function explain(response: Response): Promise<string> {
  let detail: unknown = null;
  try {
    const body: unknown = await response.json();
    if (body !== null && typeof body === 'object' && 'detail' in body) {
      detail = body.detail;
    }
  } catch {
    // A non-JSON error body is normal from a proxy or a crashed worker process.
  }

  switch (response.status) {
    case 403:
      return typeof detail === 'string' && detail
        ? detail
        : 'You do not have the role this needs. Add it under your name, top right.';
    case 404:
      return 'Not found — it may have been deleted, or the id is wrong.';
    case 409:
      return typeof detail === 'string' && detail
        ? detail
        : 'That rubric has not been approved yet. Approve it before starting a run.';
    case 413:
      return typeof detail === 'string' && detail
        ? detail
        : 'That file is too large. Try a smaller one, or paste the text instead.';
    case 422:
      return `The request was rejected as invalid: ${typeof detail === 'string' ? detail : 'check the fields above.'}`;
    case 503:
      return typeof detail === 'string' && detail
        ? detail
        : 'The screener is not ready — check the model, disk space and migrations.';
    default:
      return typeof detail === 'string' && detail
        ? detail
        : `The API returned ${String(response.status)}.`;
  }
}

function encode(body: unknown): BodyInit | null {
  if (body === undefined) return null;
  if (body instanceof FormData) return body;
  return JSON.stringify(body);
}

async function request<T>(
  path: string,
  identity: Identity,
  { method = 'GET', body, timeoutMs = DEFAULT_TIMEOUT_MS, signal }: RequestOptions = {},
): Promise<T> {
  const timeout = AbortSignal.timeout(timeoutMs);
  const abort = signal ? AbortSignal.any([signal, timeout]) : timeout;

  let response: Response;
  try {
    response = await fetch(path, {
      method,
      headers: headers(identity, body),
      body: encode(body),
      signal: abort,
      // Responses carry candidate names and verdicts. Nothing caches them; the
      // API sends `Cache-Control: no-store` and this is the request-side half.
      cache: 'no-store',
      credentials: 'same-origin',
    });
  } catch (error) {
    if (signal?.aborted) throw error; // The caller navigated away; not a failure.
    if (timeout.aborted) {
      throw new ApiError(
        `The API did not respond within ${String(Math.round(timeoutMs / 1000))}s. It may be busy loading the model.`,
      );
    }
    throw new ApiError('Cannot reach the screener API. Is the API service running?');
  }

  if (!response.ok) throw new ApiError(await explain(response), response.status);
  if (response.status === 204) return null as T;

  const text = await response.text();
  if (!text) return null as T;
  try {
    return JSON.parse(text) as T;
  } catch {
    // A 200 that is not JSON means something answered that was not the API.
    // In development that is the Vite dev server serving `index.html` for a
    // path missing from `API_PATHS` in `vite.config.ts`; in a deployment it is
    // a proxy or a login page in front of the API. Both look identical to this
    // function, and neither is a `SyntaxError` anybody can act on — which is
    // what escaped from here before, straight past `ApiError` and out to a
    // component, carrying `Unexpected token '<'` as its whole explanation.
    throw new ApiError(
      `The API returned something that is not JSON for ${path}. In development, check that the path is listed in API_PATHS in web/vite.config.ts.`,
      response.status,
    );
  }
}

function query(params: Record<string, string | number>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) search.set(key, String(value));
  return search.toString();
}

/**
 * Every call the reviewer interface can make, bound to one identity.
 *
 * Built per identity rather than read from a module-level singleton so that
 * changing "reviewing as" cannot leave a stale actor on an in-flight request —
 * the two-person approval flow depends on that name being exact.
 */
export function createApi(identity: Identity) {
  return {
    // --- positions ---------------------------------------------------------
    /**
     * Open requisitions by default.
     *
     * `includeClosed` is for the screens that name a *run's* requisition: a run
     * outlives the requisition it belongs to — closing one does not stop it —
     * and a closed one must not become a raw `pos-…` id beside candidate
     * results.
     */
    listPositions: (includeClosed = false, signal?: AbortSignal) =>
      request<Position[]>(
        includeClosed ? '/positions?include_closed=true' : '/positions',
        identity,
        { signal },
      ),

    /** One page of the folder picker. Paths are relative to the resume share. */
    listFolders: (
      args: { path: string; q: string; offset: number; limit: number },
      signal?: AbortSignal,
    ) => request<FolderPage>(`/positions/folders?${query(args)}`, identity, { signal }),

    createPosition: (input: {
      reference: string;
      title: string;
      jd_text: string;
      jd_source?: JdSource;
      jd_filename?: string | null;
      jd_file_sha256?: string | null;
      jd_ocr_used?: boolean | null;
    }) => request<Position>('/positions', identity, { method: 'POST', body: input }),

    /**
     * A job-description document → the text inside it. Creates nothing.
     *
     * The reviewer reads what comes back, fixes whatever the parser got wrong,
     * and only then calls `createPosition`. That ordering is the point: a
     * two-column PDF that interleaves produces a perfectly plausible rubric, and
     * the approval step in front of that rubric cannot catch it because there is
     * nothing to compare it against.
     */
    extractJdDocument: (file: File) => {
      const form = new FormData();
      form.append('file', file);
      return request<JdDocument>('/jd-documents', identity, {
        method: 'POST',
        body: form,
        timeoutMs: UPLOAD_TIMEOUT_MS,
      });
    },

    /** Take a filled requisition off the working list. Deletes nothing. */
    closePosition: (positionId: string) =>
      request<Position>(`/positions/${encodeURIComponent(positionId)}/close`, identity, {
        method: 'POST',
      }),

    // --- rubrics -----------------------------------------------------------
    extractRubric: (positionId: string) =>
      request<Rubric>(`/positions/${encodeURIComponent(positionId)}/rubric/extract`, identity, {
        method: 'POST',
        timeoutMs: EXTRACT_TIMEOUT_MS,
      }),

    /**
     * `base_version` is the version the editor was looking at.
     *
     * Two recruiters tuning the same rubric in adjacent tabs is the ordinary
     * case, not the exotic one. Without it the second save silently supersedes
     * the first — no conflict, no error, just one person's edits gone and a
     * higher version number to suggest everything worked.
     */
    saveRubric: (positionId: string, criteria: Criterion[], baseVersion: number | null) =>
      request<Rubric>(`/positions/${encodeURIComponent(positionId)}/rubric`, identity, {
        method: 'PUT',
        body: { criteria, base_version: baseVersion },
      }),

    approveRubric: (rubricId: string) =>
      request<Rubric>(`/rubrics/${encodeURIComponent(rubricId)}/approve`, identity, {
        method: 'POST',
      }),

    latestRubric: (positionId: string, signal?: AbortSignal) =>
      request<Rubric | null>(`/positions/${encodeURIComponent(positionId)}/rubric`, identity, {
        signal,
      }),

    approvedRubric: (positionId: string, signal?: AbortSignal) =>
      request<Rubric | null>(
        `/positions/${encodeURIComponent(positionId)}/rubric/approved`,
        identity,
        { signal },
      ),

    // --- runs --------------------------------------------------------------
    listRuns: (signal?: AbortSignal) => request<Run[]>('/runs', identity, { signal }),

    createRun: (input: { position_id: string; rubric_id: string }) =>
      request<Run>('/runs', identity, { method: 'POST', body: input }),

    startRun: (runId: string) =>
      request<{ count: number }>(`/runs/${encodeURIComponent(runId)}/start`, identity, {
        method: 'POST',
      }),

    rescanRun: (runId: string) =>
      request<{ count: number }>(`/runs/${encodeURIComponent(runId)}/rescan`, identity, {
        method: 'POST',
      }),

    abortRun: (runId: string) =>
      request<null>(`/runs/${encodeURIComponent(runId)}/abort`, identity, { method: 'POST' }),

    runStatus: (runId: string, signal?: AbortSignal) =>
      request<RunStatus>(`/runs/${encodeURIComponent(runId)}/status`, identity, { signal }),

    listCandidates: (runId: string, signal?: AbortSignal) =>
      request<RankedCandidates>(`/runs/${encodeURIComponent(runId)}/candidates`, identity, {
        signal,
      }),

    signOff: (runId: string) =>
      request<null>(`/runs/${encodeURIComponent(runId)}/sign-off`, identity, { method: 'POST' }),

    // --- candidates --------------------------------------------------------
    decide: (input: { candidateId: number; decision: RecordableDecision; reason: string }) =>
      request<null>(`/candidates/${String(input.candidateId)}/decision`, identity, {
        method: 'POST',
        body: { decision: input.decision, reason: input.reason },
      }),

    decideBulk: (input: {
      candidate_ids: number[];
      decision: RecordableDecision;
      reason: string;
    }) =>
      request<BulkDecisionResult>('/candidates/decisions', identity, {
        method: 'POST',
        body: input,
      }),

    /**
     * A URL for the browser to follow, not a body for this app to hold.
     *
     * The original document is served as a stream; pulling a 5 MB PDF through
     * `fetch` to hand it to an object URL would put every document a reviewer
     * opens into this tab's memory for no purpose.
     */
    fileUrl: (candidateId: number) => `/candidates/${String(candidateId)}/file`,

    // --- audit -------------------------------------------------------------
    searchAudit: (
      filters: {
        actor_id: string;
        action: string;
        entity: string;
        entity_id: string;
        since: string;
        until: string;
        offset: number;
        limit: number;
      },
      signal?: AbortSignal,
    ) => request<AuditPage>(`/audit?${query(filters)}`, identity, { signal }),

    runStory: (runId: string, signal?: AbortSignal) =>
      request<RunStory>(`/runs/${encodeURIComponent(runId)}/story`, identity, { signal }),

    adverseActionRecord: (candidateId: number, signal?: AbortSignal) =>
      request<AdverseActionRecord>(`/candidates/${String(candidateId)}/record`, identity, {
        signal,
      }),

    // --- ops ---------------------------------------------------------------
    health: (signal?: AbortSignal) => request<Health>('/health', identity, { signal }),

    /** The overview counts, in one request rather than assembled client-side. */
    dashboard: (signal?: AbortSignal) => request<Dashboard>('/dashboard', identity, { signal }),

    /** What this deployment allows. Changes only when the API restarts. */
    config: (signal?: AbortSignal) => request<AppConfig>('/config', identity, { signal }),
  };
}

export type Api = ReturnType<typeof createApi>;
