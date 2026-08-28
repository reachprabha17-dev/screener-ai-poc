/**
 * The HTTP contract and the error handling — the two things in this app that are
 * ours rather than the framework's.
 *
 * These are the tests `tests/test_ui.py` used to hold against the Python client.
 * They are worth keeping in the same shape: every one of them asserts a sentence
 * a recruiter will read, and the reason they exist is that a screen full of
 * `[object Object]` or `Failed to fetch` is indistinguishable from a bug in the
 * screening itself.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, createApi } from './client';

const identity = { actor: 'reviewer-1', roles: ['admin'] };

function respondWith(body: unknown, init: ResponseInit = {}) {
  return vi.fn<typeof fetch>(() =>
    Promise.resolve(
      new Response(body === null ? null : JSON.stringify(body), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
        ...init,
      }),
    ),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('identity headers', () => {
  it('sends the actor on every request', async () => {
    const fetchMock = respondWith([]);
    vi.stubGlobal('fetch', fetchMock);

    await createApi(identity).listPositions();

    const sent = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
    expect(sent.get('X-Actor')).toBe('reviewer-1');
    expect(sent.get('X-Actor-Roles')).toBe('admin');
  });

  it('omits the roles header when the operator has chosen none', async () => {
    // An absent header means "the stub's default roles"; an empty one means "no
    // roles at all", which would lock the operator out of every role-scoped read.
    const fetchMock = respondWith([]);
    vi.stubGlobal('fetch', fetchMock);

    await createApi({ actor: 'nobody', roles: [] }).listPositions();

    const sent = new Headers(fetchMock.mock.calls[0]?.[1]?.headers);
    expect(sent.has('X-Actor-Roles')).toBe(false);
  });
});

describe('error translation', () => {
  it('explains a 409 as an unapproved rubric', async () => {
    vi.stubGlobal('fetch', respondWith({}, { status: 409 }));

    await expect(
      createApi(identity).createRun({ position_id: 'p', rubric_id: 'r' }),
    ).rejects.toThrow(/has not been approved/);
  });

  it('prefers the API’s own sentence when it sent one', async () => {
    vi.stubGlobal(
      'fetch',
      respondWith(
        { detail: 'criteria C2 were edited after their claim was written' },
        { status: 400 },
      ),
    );

    await expect(createApi(identity).approveRubric('r-1')).rejects.toThrow(
      /criteria C2 were edited/,
    );
  });

  it('names the auditor role on a 403', async () => {
    vi.stubGlobal('fetch', respondWith({}, { status: 403 }));

    await expect(createApi(identity).runStory('run-1')).rejects.toThrow(/role/i);
  });

  it('turns an unreachable API into a sentence, not a TypeError', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(() => Promise.reject(new TypeError('Failed to fetch'))),
    );

    const error = await createApi(identity)
      .health()
      .catch((caught: unknown) => caught);

    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).message).toMatch(/Is the API service running/);
  });

  it('carries the status code so callers can distinguish a 403 from a 500', async () => {
    vi.stubGlobal('fetch', respondWith({}, { status: 404 }));

    const error = (await createApi(identity)
      .listCandidates('run-1')
      .catch((caught: unknown) => caught)) as ApiError;

    expect(error.status).toBe(404);
  });
});

describe('a reply that is not from the API', () => {
  it('names the cause when a 200 is not JSON', async () => {
    // The dev server answers a path missing from API_PATHS in vite.config.ts
    // with index.html and a 200. Before this, `JSON.parse` threw a bare
    // SyntaxError that went straight past ApiError to a component, and the
    // reviewer was shown `Unexpected token '<'` as the whole explanation while
    // the API's access log stayed empty. It cost an afternoon once.
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>(() =>
        Promise.resolve(
          new Response('<!doctype html><html><div id="root"></div></html>', { status: 200 }),
        ),
      ),
    );

    await expect(createApi(identity).config()).rejects.toThrow(ApiError);
    await expect(createApi(identity).config()).rejects.toThrow(/not JSON for \/config/);
    await expect(createApi(identity).config()).rejects.toThrow(/vite\.config\.ts/);
  });
});

describe('responses', () => {
  it('reads a 204 as nothing rather than failing to parse it', async () => {
    vi.stubGlobal('fetch', respondWith(null, { status: 204 }));

    await expect(createApi(identity).abortRun('run-1')).resolves.toBeNull();
  });

  it('returns null for a position with no rubric yet', async () => {
    vi.stubGlobal('fetch', respondWith(null));

    await expect(createApi(identity).latestRubric('pos-1')).resolves.toBeNull();
  });

  it('round-trips the rubric claim fields on save', async () => {
    // The server's model defaults missing fields rather than rejecting them, so a
    // save that dropped `claim` would silently blank what phase 2 verifies against.
    const fetchMock = respondWith({});
    vi.stubGlobal('fetch', fetchMock);

    await createApi(identity).saveRubric(
      'pos-1',
      [
        {
          id: 'C1',
          text: 'Python',
          claim: 'The candidate writes Python.',
          claim_stale: false,
          weight: 3,
          must_have: true,
        },
      ],
      2,
    );

    const body = fetchMock.mock.calls[0]?.[1]?.body;
    expect(JSON.parse(typeof body === 'string' ? body : '{}')).toEqual({
      criteria: [
        {
          id: 'C1',
          text: 'Python',
          claim: 'The candidate writes Python.',
          claim_stale: false,
          weight: 3,
          must_have: true,
        },
      ],
      base_version: 2,
    });
  });

  it('builds the candidate file URL without fetching the document', () => {
    // A 5 MB PDF pulled through this process to hand to the browser is memory
    // spent for no purpose — the API streams it.
    expect(createApi(identity).fileUrl(42)).toBe('/candidates/42/file');
  });
});
