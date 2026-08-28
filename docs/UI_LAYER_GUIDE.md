# The reviewer interface, end to end

A complete walkthrough of `web/` — every file, what triggers it, what goes in,
what comes out, what gets checked, and where it hands off.

Written for someone who has never opened this folder. No prior knowledge of the
Python side is assumed; where the UI depends on something the server does, that
is stated rather than referenced.

Companion to `CODEBASE_GUIDE.md`, which covers the whole system. This one covers
the browser only.

**The diagrams are ASCII on purpose** — the same convention the specs use — so
they render identically in a terminal, an editor, a diff and on GitHub, and so a
`git diff` of one is readable.

---

## Contents

1. [What this thing actually is](#part-1--what-this-thing-actually-is)
2. [Running it](#part-2--running-it)
3. [The file map](#part-3--the-file-map)
4. [Boot: from URL to first paint](#part-4--boot-from-url-to-first-paint)
5. [The four rules](#part-5--the-four-rules-this-app-is-held-to)
6. [The data layer](#part-6--the-data-layer-three-files)
7. [Identity and the session](#part-7--identity-and-the-session)
8. [The route tree](#part-8--the-route-tree)
9. [Screen by screen](#part-9--screen-by-screen)
10. [Every validation, in one table](#part-10--every-validation-in-one-table)
11. [The shared vocabulary](#part-11--the-shared-vocabulary-srclib)
12. [The UI primitives](#part-12--the-ui-primitives-srcui)
13. [What happens when things fail](#part-13--what-happens-when-things-fail)
14. [Polling and caching, summarised](#part-14--polling-and-caching-summarised)
15. [Tests](#part-15--tests)
16. [Adding an endpoint](#part-16--adding-an-endpoint)
17. [Known gaps](#part-17--known-gaps)

### The diagrams

| # | Shows | In |
|---|---|---|
| 1 | Where the UI sits among the three processes | Part 1 |
| 2 | Boot, request by request, URL to first paint | Part 4 |
| 3 | A read and a write, side by side | Part 6 |
| 4 | Which write moves which screen | Part 6.3 |
| 5 | Identity → query keys → request headers | Part 7 |
| 6 | The full route and component tree | Part 8 |
| 7 | The whole product as five screens | Part 9 |
| 8 | The rubric's life, draft to approved | Part 9.4 |
| 9 | A run's two axes: status and phase | Part 9.6 |
| 10 | The review screen, as a wireframe | Part 9.7 |
| 11 | How one criterion decides what to render | Part 9.7 |
| 12 | Client checks vs server checks | Part 10 |
| 13 | The four-level error ladder | Part 13 |
| 14 | The polling timeline | Part 14 |

---

## Part 1 — What this thing actually is

It is a **React app in a browser tab that talks to one HTTP API and does nothing
else.**

```
                        the reviewer's browser
        ┌───────────────────────────────────────────────────────┐
        │  web/          React 19 · TypeScript · Vite · Tailwind │
        │                                                       │
        │  owns:     the URL · form state · theme · "acting as"  │
        │  owns NOT: any business logic · any database handle    │
        │            any score, band, rank or escalation         │
        └───────────────────────────┬───────────────────────────┘
                                    │
                   HTTP · same origin · JSON · no CORS
                   X-Actor + X-Actor-Roles on every request
                                    │
        ════════════════════════════╪═══════════ process boundary ═══
                                    ▼
        ┌───────────────────────────────────────────────────────┐
        │  API   FastAPI + uvicorn                              │
        │  ── also serves this very bundle at /ui/ ──            │
        └───────────────────────────┬───────────────────────────┘
                                    ▼
                            ┌───────────────┐         ┌────────────┐
                            │   Postgres    │◀───────▶│   worker   │
                            └───────────────┘         └────────────┘
                                                       does the AI work
                                                       the UI never sees it
```

That sentence is the whole design. It is worth saying what it rules out:

- **It has no database connection.** It cannot have one. It is not a Python
  process. There is no ORM, no SQL, no driver.
- **It has no business logic.** It never decides a band, a score, a rank, or
  whether a candidate is escalated. It renders what the server sends.
- **It has no configuration.** There is no "API URL" setting. It talks to the
  host that served it, and only that host.
- **It has no server-side session.** A reload loses nothing, because there is
  nothing on the server to lose.

What it *does* own:

- **Which screen you are on**, encoded in the URL.
- **What is typed into a form** before it is submitted.
- **Which candidate is selected**, encoded in the URL query string.
- **Cosmetic preferences** — theme, and the name you are reviewing as.

Everything else belongs to the server, and is held in a cache that TanStack Query
manages.

### Why it exists at all

The previous interface was Streamlit — a Python process that re-executed the
entire script on every interaction. Three screens each had a "which run?"
dropdown writing one shared `session_state` key. Each re-run compared its widget
against the shared value and corrected it, which triggered another re-run:

> **1,207 script passes in 150 seconds, 119% CPU, an API call on every pass**,
> with the browser stuck on *Running…*

It was patched twice and the shape that allowed it survived both patches. Here,
the run *is* `/runs/:runId`. There is no shared selection for two widgets to
fight over, the back button works, and a reviewer can send a colleague a link to
the exact screen they are looking at.

That is the entire argument for the migration. Everything else in this document
is consequence.

---

## Part 2 — Running it

### In development

```bash
cd web
npm install                 # once
npm run dev                 # → http://127.0.0.1:5173/ui/
```

Vite serves the app itself and **proxies** the API paths through to the Python
process, so the browser sees one origin. The proxied paths are listed explicitly
in `vite.config.ts`:

```
/positions  /runs  /rubrics  /candidates  /audit  /health  /ready  /dashboard
```

The proxy target is `http://127.0.0.1:8010` unless `SCREENER_API_URL` says
otherwise. The dev server binds to `127.0.0.1` only — auth is stubbed, so
anything that can reach the port can read every candidate in the system.

`scripts/dev.sh start` runs the API, the worker and this dev server together.

### In a deployment

```bash
npm run build               # → web/dist
```

The Python API process serves `web/dist` at `/ui/` (`screener/api/app.py ::
_mount_reviewer_ui`). There is no separate web server, no nginx serving the
bundle, no second port. `scripts/dev.sh build` does the same thing.

**The base path is `/ui/` in both.** `vite.config.ts` sets `base: '/ui/'` and
`App.tsx` passes `import.meta.env.BASE_URL` to the router's `basename`, so dev
and production resolve every asset and every route identically. A base that only
differs in production is a class of bug that only appears after the build.

### Checks

```bash
npm run check   # prettier --check, eslint, tsc --build, vitest run
```

`./scripts/dev.sh check` runs this too, but **warns and skips it** when
`web/node_modules` is absent — a backend change is never blocked by a machine
with no Node on it.

---

## Part 3 — The file map

```
web/
├── index.html                  the HTML shell + the anti-flash theme script
├── vite.config.ts              base path, dev proxy, build output, vitest config
├── package.json                dependencies and the check scripts
└── src/
    ├── main.tsx                mounts React onto #root
    ├── App.tsx                 providers + the entire route table
    ├── index.css               Tailwind import and the `.dark` custom variant
    │
    ├── api/                    ← everything that knows the server exists
    │   ├── types.ts            the wire contract, mirrored from schemas.py
    │   ├── client.ts           THE ONLY MODULE THAT CALLS fetch()
    │   ├── client.test.ts      contract tests for the above
    │   └── queries.ts          every read and write as a hook; keys, polling,
    │                           invalidation
    │
    ├── session/                ← who is acting
    │   ├── context.ts          the React context + useSession()
    │   └── SessionProvider.tsx actor + roles, remembered in localStorage
    │
    ├── pages/                  ← one file per screen
    │   ├── DashboardPage.tsx           /
    │   ├── RequisitionsPage.tsx        /requisitions
    │   ├── NewRequisitionPage.tsx      /requisitions/new
    │   ├── RequisitionPage.tsx         /requisitions/:positionId
    │   ├── RunsPage.tsx                /runs
    │   ├── RunLayout.tsx               /runs/:runId          (tab shell)
    │   ├── RunProgressPage.tsx         /runs/:runId
    │   ├── ReviewPage.tsx              /runs/:runId/review
    │   ├── AuditLayout.tsx             /audit                (tab shell)
    │   └── audit/
    │       ├── RunStoryPage.tsx        /audit
    │       ├── DecisionRecordPage.tsx  /audit/record
    │       └── AuditSearchPage.tsx     /audit/search
    │
    ├── components/             ← parts with domain meaning
    │   ├── AppShell.tsx              header, nav, <Outlet/>
    │   ├── ErrorBoundary.tsx         the last stop before a white screen
    │   ├── QueryState.tsx            loading / error / success, handled once
    │   ├── HealthBadge.tsx           system state in one word
    │   ├── IdentityMenu.tsx          "reviewing as", and roles
    │   ├── ThemeToggle.tsx           light → dark → system
    │   ├── FolderPicker.tsx          browse the server's resume share
    │   ├── JdInput.tsx               upload a JD document, or paste one
    │   ├── RubricEditor.tsx          draft → edit → save → approve
    │   ├── RunList.tsx               runs as a table
    │   ├── RunSelect.tsx             pick a run, labelled meaningfully
    │   ├── RunStatePill.tsx          pending/running/completed/empty/…
    │   ├── EscalationMeter.tsx       the rate, against its budget
    │   ├── StatTile.tsx              one headline count
    │   ├── BandPill.tsx              A/B/C/D — never a score
    │   ├── CandidateDetail.tsx       one candidate, in decision order
    │   ├── CriterionBlock.tsx        one criterion + quote + disagreement
    │   ├── ParsedResumeText.tsx      what the parser extracted
    │   ├── DecisionForm.tsx          one decision, with a required reason
    │   ├── BulkDecisionForm.tsx      one decision across a group
    │   └── ClosePositionButton.tsx   confirmed close
    │
    ├── lib/                    ← pure helpers, no network
    │   ├── labels.ts           THE WORDS ON SCREEN. Each one is a decision
    │   ├── highlight.ts        slice a quote out with context around it
    │   ├── format.ts           dates, percentages, counts, CSV
    │   ├── escalations.ts      count reasons across a group
    │   ├── jd.ts               a job description + where it came from
    │   ├── theme.ts            light/dark/system, persisted
    │   └── useDebounced.ts     settle a value before acting on it
    │
    ├── ui/                     ← thin Tailwind wrappers, no domain knowledge
    │   ├── Alert  Badge  Button  Card  Disclosure  Field  FileInput
    │   ├── Markdown  Popover  Table
    │   └── cn.ts               class-name merging
    │
    └── test/
        ├── setup.ts            jsdom stubs for Radix
        └── utils.tsx           renderWithProviders()
```

**The dependency direction is strictly one way:**

```
pages ──▶ components ──▶ ui
   │           │
   └───────────┴──▶ api/queries ──▶ api/client ──▶ api/types
   │
   └──▶ lib          (pure, imports nothing but types)
   │
   └──▶ session
```

Nothing in `ui/` imports from `api/` or `lib/`. Nothing in `lib/` performs I/O.

---

## Part 4 — Boot: from URL to first paint

A reviewer types `http://host:8000/ui/runs/run-8f21c0/review`.

```
 browser                                            API process
    │
    │  GET /ui/runs/run-8f21c0/review
    │ ─────────────────────────────────────────────────▶
    │                                    "runs/run-8f21c0/review" is not a file
    │                                    on disk → it is a route in the BROWSER
    │  200  index.html                   └──▶ FileResponse(index.html)
    │ ◀─────────────────────────────────────────────────
    │
    ├─ index.html: inline <script>, BEFORE any stylesheet
    │     reads localStorage['screener-theme'] → toggles .dark on <html>
    │     (so the page never flashes the wrong theme first)
    │
    ├─ main.tsx        createRoot(#root).render(<StrictMode><App/></StrictMode>)
    │
    ├─ App.tsx         <ErrorBoundary>                  ← outermost, catches all
    │                   └ <QueryClientProvider>          retry 1 · staleTime 10s
    │                      └ <SessionProvider>           localStorage actor+roles
    │                         └ <Toaster/>               bottom-right
    │                            └ <BrowserRouter basename="/ui/">
    │
    ├─ route match     AppShell ─▶ RunLayout ─▶ ReviewPage
    │
    ├─ hooks fire      useCandidates('run-8f21c0')
    │                    key ['candidates', {actor,roles}, 'run-8f21c0'] → MISS
    │                      └ api.listCandidates() → fetch()
    │  GET /runs/run-8f21c0/candidates + identity headers
    │ ─────────────────────────────────────────────────▶
    │                        route → service → stores → core/rank.py
    │                        → candidate_response(c, actor) for every row
    │  200  RankedCandidates                (recruiter view or auditor view)
    │ ◀─────────────────────────────────────────────────
    │
    └─ <QueryState>    spinner "Loading results…"  →  render prop  →  three groups
```

### Step 1 — The server decides what to send

`screener/api/app.py :: reviewer_ui` receives `asset_path = "runs/run-8f21c0/review"`.

```python
candidate = (dist / asset_path).resolve()
if asset_path and candidate.is_file() and candidate.is_relative_to(dist):
    return FileResponse(candidate)  # a real built asset
return FileResponse(index, media_type="text/html")  # anything else
```

`runs/run-8f21c0/review` is not a file on disk, so the server returns
`index.html`. That is deliberate: it is a route **in the browser**, not a file.

> `StaticFiles(html=True)` is not enough here — it serves `index.html` for a
> *directory* and answers 404 for `/ui/runs/run-1/review`. The symptom is easy to
> miss: the app works until somebody reloads the page or shares a link.

The resolved path is checked against the bundle directory before anything is
opened, even though the bundle is something this process built itself.

### Step 2 — `index.html` runs the anti-flash script

Before any stylesheet loads, a small inline script reads `localStorage` and puts
`.dark` on `<html>`:

```js
var stored = localStorage.getItem('screener-theme');
var dark = stored === 'dark' ||
  ((stored === null || stored === 'system') &&
    window.matchMedia('(prefers-color-scheme: dark)').matches);
document.documentElement.classList.toggle('dark', dark);
document.documentElement.style.colorScheme = dark ? 'dark' : 'light';
```

It cannot import `lib/theme.ts` (that is a module, this is synchronous inline
script), so the storage key and the fallback are duplicated by hand and kept in
lockstep. It exists only so the page never flashes the wrong theme first.

The head also carries `<meta name="robots" content="noindex, nofollow">` —
candidate names and verdicts render here, and nothing should index or archive
them.

### Step 3 — `main.tsx` mounts React

```tsx
const container = document.getElementById('root');
if (!container) throw new Error('index.html is missing #root');
createRoot(container).render(<StrictMode><App /></StrictMode>);
```

### Step 4 — `App.tsx` builds the provider stack

Outermost to innermost:

| Provider | Why it is at this level |
|---|---|
| `<ErrorBoundary>` | Outermost, so a crash anywhere lands somewhere with a message |
| `<QueryClientProvider>` | One cache for the app |
| `<SessionProvider>` | Identity, needed by every query key |
| `<Toaster>` | Transient outcome messages, bottom-right |
| `<BrowserRouter basename="/ui/">` | The routes |

The `QueryClient` is configured once:

```ts
{ retry: 1, staleTime: 10_000, refetchOnWindowFocus: true }
```

- **`retry: 1`, not the default 3.** The API is on loopback, so a failure is
  usually a stopped service or a rejected request. Retrying those twice more only
  delays telling the reviewer.
- **`staleTime: 10s`, not zero.** A run in progress genuinely changes underneath
  the screen — but zero would re-fetch the same candidate list three times as
  someone moves between tabs.

### Step 5 — The router matches, the page mounts, the hooks fire

`/runs/run-8f21c0/review` matches `AppShell` → `RunLayout` → `ReviewPage`.

Each of those calls its hooks. `useCandidates('run-8f21c0')` finds nothing in the
cache, so TanStack Query calls `api.listCandidates(...)`, which calls `fetch`,
which hits `GET /runs/run-8f21c0/candidates` with the identity headers attached.

While that is in flight, `<QueryState>` renders a spinner with the message
`"Loading results…"`. When it resolves, the render prop runs with non-nullable
data.

---

## Part 5 — The four rules this app is held to

These are enforced **from Python**, in `tests/test_layering.py`, because they are
the browser-side half of the "the UI is not a privileged process" decision. They
run in the normal `pytest` suite, so a violation fails CI even if nobody ran
`npm run check`.

### Rule 1 — There is no Python UI

`test_the_reviewer_ui_is_not_a_python_process`. A `ui/` directory reappearing, or
a `streamlit` import anywhere, fails the build.

### Rule 2 — `fetch` appears in exactly one file

`test_the_reviewer_ui_makes_all_network_calls_in_one_module`. The regex is
`\bfetch\(|XMLHttpRequest|\baxios\b`, and only `web/src/api/client.ts` may match.

> The word-boundary form matters: the substring `"fetch("` would also match
> TanStack Query's `refetch()`, which is a cache instruction, not network I/O.

**Why this rule earns its keep:** every request therefore carries the actor
headers the audit log records, and every failure becomes an `ApiError` with a
sentence in it — rather than a `TypeError: Failed to fetch` rendered over
candidate data.

### Rule 3 — Nothing is handed to the DOM as markup

`test_the_reviewer_ui_renders_no_raw_html`. No file may contain
`dangerouslySetInnerHTML`.

Resume text, decision reasons, injection excerpts and audit details are all
attacker-influenced. They go to the DOM as JSX children — React escapes them —
and the one place markdown is rendered (`ui/Markdown.tsx`) uses `react-markdown`,
which builds React elements rather than an HTML string, with raw HTML and GFM
both disabled.

### Rule 4 — No absolute URLs

`test_the_reviewer_ui_only_talks_to_the_host_that_served_it`. No file may match
`["'`]https?://`.

Every request path in `client.ts` is relative (`/positions`, `/runs/…`). The app
talks only to the host that served it. Consequences: the API needs no CORS
middleware, and there is no "API URL" setting for an operator to point at another
machine — an operator who could do that could point candidate data at another
machine.

---

## Part 6 — The data layer (three files)

Everything the server knows enters the app through these three files, in this
order: `types.ts` describes the shapes, `client.ts` fetches them, `queries.ts`
caches them.

---

### 6.1 `api/types.ts` — the wire contract

**Input:** nothing. It is types only, erased at build time.
**Output:** TypeScript interfaces used everywhere else.

Every interface names the Python model it mirrors:

| TypeScript | Python (`screener/schemas.py`) |
|---|---|
| `Position` | `PositionResponse` |
| `Folder`, `FolderPage` | `FolderResponse`, `FolderPageResponse` |
| `Criterion`, `Rubric` | `Criterion`, `RubricResponse` |
| `Run`, `RunStatus`, `FailedFile` | `RunResponse`, `RunStatusResponse`, `FailedFileResponse` |
| `CandidateSummary` | `CandidateResponse` |
| `CriterionView`, `CriterionAuditView` | `CriterionView`, `CriterionAuditView` |
| `VerifierView`, `HighlightSpan` | `VerifierView`, `HighlightSpan` |
| `InjectionFinding` | `InjectionFindingView` |
| `RankedCandidates` | `RankedResponse` |
| `Dashboard`, `ReviewQueue` | `DashboardResponse`, `ReviewQueueResponse` |
| `Health` | `HealthResponse` |
| `AuditEntry`, `AuditPage` | `AuditEntryResponse`, `AuditPageResponse` |
| `RunStory` | `RunStoryResponse` |
| `AdverseActionRecord`, `DecisionRecord` | `AdverseActionResponse`, `DecisionRecordResponse` |
| `BulkDecisionResult` | `BulkDecisionResponse` |

**Hand-written, not generated, deliberately.** The response models are the API's
public boundary. A change to one should be a decision someone makes on both
sides, not a diff that appears in a generated file nobody reads.

**Fields the recruiter view omits are optional here.** That is not laziness about
the shape: `candidate_response` on the server really does return two different
objects depending on the actor's roles, so code reading an auditor-only field has
to prove it handled its absence. `CriterionAuditView extends CriterionView` with
the extra fields; `CandidateSummary` carries the recruiter fields, and an auditor
response simply has more.

The unions worth memorising:

```ts
Verdict            = 'strong' | 'partial' | 'none'
Band               = 'A' | 'B' | 'C' | 'D'
Decision           = 'undecided' | 'advance' | 'hold' | 'reject'
Support            = 'supported' | 'insufficient' | 'contradicted'
EvidenceStatus     = 'verified' | 'partial' | 'unverified' | 'not_applicable'
VerificationStatus = 'pending' | 'done' | 'skipped'
RunState           = 'pending' | 'running' | 'completed' | 'empty' | 'failed' | 'aborted'
Phase              = 'judge' | 'verify' | 'done'

DECISIONS = ['advance', 'hold', 'reject']   // the three a reviewer can record
```

Two field-level notes carried in the file's comments:

- **`Criterion.claim` is passed through the editor untouched.** The server's
  model forbids unknown fields but *defaults* missing ones, so a save that
  dropped `claim` would silently blank the hypothesis the verification phase
  tests against — and nothing about the result would look wrong.
- **`Position.status` is carried even though the list endpoint returns open
  requisitions only.** A closed one disappearing from the list is otherwise
  indistinguishable from a deleted one, and the screen has to be able to say
  which happened.

---

### 6.2 `api/client.ts` — the only network I/O

**Input:** an `Identity` (`{actor, roles}`) plus per-call arguments.
**Output:** a promise of a typed response, or a thrown `ApiError`.

#### `ApiError`

```ts
export class ApiError extends Error {
  readonly status: number;   // 0 when the request never reached the server
}
```

Everything a user is told about a failure comes out of this class.

#### `headers(identity, body)`

```ts
{ 'X-Actor': identity.actor }
+ 'X-Actor-Roles': identity.roles.join(',')   // ONLY when roles.length > 0
+ 'Content-Type': 'application/json'          // ONLY for a body that is NOT FormData
```

> **`FormData` is the one body that must not be typed.** The browser has to set
> `multipart/form-data` *with the boundary it generates*; a header we wrote would
> replace it with one that has no boundary, and the server would be unable to
> split the parts — a 422 for a request that was fine.

**Omitting `X-Actor-Roles` is not the same as sending it empty.** The API reads
an absent header as the stub's default role set (`{admin}`), and an empty one as
"this actor has no roles at all" — which would lock the operator out of every
role-scoped read.

#### `request<T>(path, identity, options)`

The single function every call goes through.

| Step | What it does | Why |
|---|---|---|
| 1 | `AbortSignal.timeout(timeoutMs)`, combined with the caller's signal via `AbortSignal.any` | A query that is cancelled because the reviewer navigated away must stop; a slow API must also stop |
| 2 | `fetch(path, {method, headers, body, signal, cache: 'no-store', credentials: 'same-origin'})` | `no-store` is the request-side half of the API's `Cache-Control: no-store` — nothing caches candidate names and verdicts |
| 3 | On a thrown fetch: if the caller's signal aborted, rethrow (navigation, not a failure). If the timeout fired, `ApiError("The API did not respond within Ns. It may be busy loading the model.")`. Otherwise `ApiError("Cannot reach the screener API. Is the API service running?")` | Three different situations, three different next actions |
| 4 | `if (!response.ok) throw new ApiError(await explain(response), response.status)` | Status → sentence |
| 5 | `204` → `null`; empty body → `null`; otherwise `JSON.parse(text)`, and a parse failure becomes an `ApiError` naming the path | The decision endpoints return 204. A **200 that is not JSON** means something answered that was not the API — in development, a path missing from `API_PATHS` in `vite.config.ts`; in a deployment, a proxy or a login page in front of it. Both look identical here, and neither is a `SyntaxError` anybody can act on |

Timeouts:

```ts
DEFAULT_TIMEOUT_MS = 30_000
EXTRACT_TIMEOUT_MS = 180_000   // rubric extraction is a live LLM call
UPLOAD_TIMEOUT_MS  = 60_000    // reading one document
```

> Rubric extraction is ~5 s typical, but a cold model load takes considerably
> longer, and timing out mid-generation looks like a failure when it was only
> slow.
>
> A document upload is the opposite case: the server bounds it at
> `jd_parse_timeout_s`, which is seconds, so failing fast is the kinder
> behaviour — the paste box is right there.

#### `explain(response)` — status codes become sentences

It first tries to read `{"detail": "..."}` out of the body (a non-JSON error body
is normal from a proxy or a crashed process, so the parse is wrapped).

| Status | What the reviewer is told |
|---|---|
| **403** | the server's `detail`, else *"You do not have the role this needs. Add it under your name, top right."* |
| **404** | *"Not found — it may have been deleted, or the id is wrong."* |
| **409** | the server's `detail`, else *"That rubric has not been approved yet. Approve it before starting a run."* |
| **413** | the server's `detail`, else *"That file is too large. Try a smaller one, or paste the text instead."* |
| **422** | *"The request was rejected as invalid: …"* |
| **503** | the server's `detail` (the parser is at capacity — retry), else *"The screener is not ready — check the model, disk space and migrations."* |
| anything else | the server's `detail`, else *"The API returned N."* |

A 409 in particular has one cause in this system and a clear next step, and
saying so beats echoing a status code at someone trying to fill a vacancy.

#### `createApi(identity)` — every call the app can make

Built **per identity** rather than read from a module-level singleton, so
changing "reviewing as" cannot leave a stale actor on an in-flight request. The
two-person approval flow depends on that name being exact.

| Method | HTTP | Body / query | Returns |
|---|---|---|---|
| `listPositions(includeClosed?)` | `GET /positions[?include_closed=true]` | — | `Position[]` |
| `listFolders({path,q,offset,limit})` | `GET /positions/folders?…` | — | `FolderPage` |
| `createPosition({reference,title,jd_text})` | `POST /positions` | JSON | `Position` |
| `closePosition(positionId)` | `POST /positions/{id}/close` | — | `Position` |
| `extractRubric(positionId)` | `POST /positions/{id}/rubric/extract` | — | `Rubric` (180 s timeout) |
| `saveRubric(positionId, criteria, baseVersion)` | `PUT /positions/{id}/rubric` | `{criteria, base_version}` | `Rubric` |
| `approveRubric(rubricId)` | `POST /rubrics/{id}/approve` | — | `Rubric` |
| `latestRubric(positionId)` | `GET /positions/{id}/rubric` | — | `Rubric \| null` |
| `approvedRubric(positionId)` | `GET /positions/{id}/rubric/approved` | — | `Rubric \| null` |
| `listRuns()` | `GET /runs` | — | `Run[]` |
| `createRun({position_id, rubric_id})` | `POST /runs` | JSON | `Run` |
| `startRun(runId)` | `POST /runs/{id}/start` | — | `{count}` |
| `rescanRun(runId)` | `POST /runs/{id}/rescan` | — | `{count}` |
| `abortRun(runId)` | `POST /runs/{id}/abort` | — | `null` (204) |
| `runStatus(runId)` | `GET /runs/{id}/status` | — | `RunStatus` |
| `listCandidates(runId)` | `GET /runs/{id}/candidates` | — | `RankedCandidates` |
| `signOff(runId)` | `POST /runs/{id}/sign-off` | — | `null` (204) |
| `decide({candidateId,decision,reason})` | `POST /candidates/{id}/decision` | `{decision, reason}` | `null` (204) |
| `decideBulk({candidate_ids,decision,reason})` | `POST /candidates/decisions` | JSON | `BulkDecisionResult` |
| `fileUrl(candidateId)` | — | — | **a URL string**, not a request |
| `searchAudit(filters)` | `GET /audit?…` | — | `AuditPage` |
| `runStory(runId)` | `GET /runs/{id}/story` | — | `RunStory` |
| `adverseActionRecord(candidateId)` | `GET /candidates/{id}/record` | — | `AdverseActionRecord` |
| `health()` | `GET /health` | — | `Health` |
| `dashboard()` | `GET /dashboard` | — | `Dashboard` |

**`fileUrl` is the odd one out on purpose.** It returns a string for the browser
to follow in a new tab. The original document is served as a stream; pulling a
5 MB PDF through `fetch` to hand it to an object URL would put every document a
reviewer opens into this tab's memory for no purpose.

---

### 6.3 `api/queries.ts` — the cache

**Input:** the session identity (via `useScope()`), plus per-hook arguments.
**Output:** `UseQueryResult<T>` for reads, `UseMutationResult<…>` for writes.

The governing idea, from the file's own header:

> **Nothing here is fetched in an effect.** `useEffect` + `useState` around a
> `fetch` is the shape that produces the two failures this screen cannot afford:
> a request that keeps running after the reviewer navigated away, and a stale
> candidate list rendering as if it were current.

```
        A READ                                  A WRITE
   ═══════════════════                    ═══════════════════

   ReviewPage                             DecisionForm
      │ useCandidates(runId)                 │ useDecide(runId).mutate({...})
      ▼                                      ▼
   ┌─────────────────────┐               ┌─────────────────────┐
   │  api/queries.ts     │               │  api/queries.ts     │
   │  key includes the   │               │  mutationFn         │
   │  IDENTITY, always   │               │                     │
   └──────────┬──────────┘               └──────────┬──────────┘
              │ cache hit? ──yes──▶ return          │
              │ no                                  │
              ▼                                     ▼
   ┌─────────────────────────────────────────────────────────────┐
   │  api/client.ts     ← THE ONLY MODULE THAT CALLS fetch()      │
   │  · headers(identity)   X-Actor / X-Actor-Roles              │
   │  · AbortSignal.any([caller's signal, timeout])              │
   │  · cache: 'no-store'   credentials: 'same-origin'           │
   └──────────┬──────────────────────────────────────┬───────────┘
              ▼                                      ▼
            API                                    API
              │                                      │
      ┌───────┴────────┐                     ┌───────┴────────┐
      │ 2xx → JSON     │                     │ 204 → null     │
      │ else → explain()                     │ else → explain()
      │      → ApiError│                     │      → ApiError│
      └───────┬────────┘                     └───────┬────────┘
              ▼                                      ▼ onSuccess
      TanStack cache                        invalidateQueries([...])
              │                                      │
              ▼                                      └──▶ every affected read
      <QueryState> ──▶ UI                                 re-fetches itself
```

#### `useApi()` and `useScope()`

```ts
useApi()   → createApi({actor, roles}), memoised on those two values
useScope() → {actor, roles}, memoised
```

#### Query keys — all in one object

```ts
export const keys = {
  health:        (id)                  => ['health', id],
  config:        ()                    => ['config'],          // NOT identity-scoped
  dashboard:     (id)                  => ['dashboard', id],
  positions:     (id, includeClosed)   => ['positions', id, {includeClosed}],
  folders:       (id, path, q, offset) => ['folders', id, path, q, offset],
  latestRubric:  (id, positionId)      => ['rubric', 'latest', id, positionId],
  approvedRubric:(id, positionId)      => ['rubric', 'approved', id, positionId],
  runs:          (id)                  => ['runs', id],
  runStatus:     (id, runId)           => ['run-status', id, runId],
  candidates:    (id, runId)           => ['candidates', id, runId],
  audit:         (id, filters)         => ['audit', id, filters],
  runStory:      (id, runId)           => ['run-story', id, runId],
  record:        (id, candidateId)     => ['record', id, candidateId],
};
```

**Two things about this object are load-bearing.**

**Every key carries the identity.** Roles change what the API returns —
`candidate_response` serves a recruiter view or an auditor view from the same
URL, and three reads answer 403 without the `auditor` role. A cache keyed without
the actor would hand the second person the first person's view.

**They are in one place.** Invalidation is the part that rots when keys are
written inline: a mutation invalidates `['runs']` while a component subscribed to
`['run-list']`, and the screen silently shows yesterday's data.

#### `useConfig()` — the one key that is not identity-scoped

Every other key in this file contains the identity, because roles change what the
API returns and a shared key would hand the second person the first person's
view. `config` is deployment policy: the answer is the same for everybody, so
scoping it would refetch on every "reviewing as" change for no difference.

```ts
staleTime: Infinity, gcTime: Infinity, refetchOnWindowFocus: false
```

It changes only when the API restarts. A stale answer cannot mislead anyone into
a wrong decision either — the server enforces `jd_intake_mode` independently, so
the worst case is a control that is offered and then refused with a sentence
saying why.

#### `useExtractJdDocument()` — a read that is a mutation

Reading a document has **no cache identity**. The same file uploaded twice is two
separate acts by a person, and the second one is how somebody retries after a
failure — a cache would silently return the first reading, including the failure.
Nothing is invalidated on success, because nothing on the server changed.

#### The reads

| Hook | Endpoint | Refetch behaviour | Notes |
|---|---|---|---|
| `useHealth()` | `/health` | every **30 s**, `retry: false` | Health is a banner, not a screen |
| `useDashboard()` | `/dashboard` | every **5 s while `runs_in_progress > 0`**, else off | That is when the numbers move |
| `usePositions(includeClosed)` | `/positions` | default | The flag is **in the key**, not filtered from one list — the two answers are different server responses |
| `useFolders(path, q, offset)` | `/positions/folders` | default, `placeholderData: previous` | Keeps the previous page on screen so the picker does not flicker to empty |
| `useLatestRubric(positionId)` | `/positions/{id}/rubric` | default | |
| `useApprovedRubric(positionId)` | `/positions/{id}/rubric/approved` | default | |
| `useRuns()` | `/runs` | default | |
| `useRunStatus(runId)` | `/runs/{id}/status` | every **5 s while `isLive(status)`**, else off | `isLive` = `pending` or `running` |
| `useCandidates(runId)` | `/runs/{id}/candidates` | default | |
| `useAudit(filters)` | `/audit` | `placeholderData: previous`, `retry: false` | A 403 for want of the auditor role is not worth three tries |
| `useRunStory(runId)` | `/runs/{id}/story` | `retry: false` | Same |
| `useAdverseActionRecord(id \| null)` | `/candidates/{id}/record` | `enabled: id !== null`, `retry: false` | Does not fire until a candidate is chosen |

```ts
const POLL_MS = 5_000;
const FOLDER_PAGE_SIZE = 15;
export function isLive(status: string) {
  return status === 'pending' || status === 'running';
}
```

#### The writes, and exactly what each invalidates

> **Invalidate rather than patch the cache by hand.** Optimistic edits are the
> wrong trade on this screen: the server rejects decisions on escalated
> candidates, refuses sign-off with an unread queue, and versions a rubric on
> save. A cache written from what the client *hoped* happened would show a
> reviewer an outcome the system did not record.

| Hook | Calls | On success |
|---|---|---|
| `useCreatePosition()` | `createPosition` | invalidate `['positions', scope]` (both flags), `dashboard` |
| `useClosePosition()` | `closePosition` | invalidate `['positions', scope]`, `dashboard` |
| `useExtractRubric(pid)` | `extractRubric` | **`setQueryData(latestRubric)`** with the returned draft |
| `useSaveRubric(pid)` | `saveRubric` | `setQueryData(latestRubric)`; invalidate `approvedRubric` |
| `useApproveRubric(pid)` | `approveRubric` | `setQueryData(latestRubric)`; invalidate `approvedRubric` |
| `useCreateRun()` | `createRun` | invalidate `runs`, `dashboard` |
| `useRunControl(runId)` | `startRun` / `rescanRun` / `abortRun` | invalidate `runStatus`, `runs`, `dashboard` |
| `useDecide(runId)` | `decide` | invalidate `candidates`, `runStatus`, `dashboard` |
| `useDecideBulk(runId)` | `decideBulk` | same three |
| `useSignOff(runId)` | `signOff` | invalidate `runs`, `runStatus`, `runStory` |

The three rubric mutations use `setQueryData` rather than invalidate because the
server hands back the exact object that would be re-fetched — writing it straight
in avoids a round trip with no risk of divergence. Everything else invalidates.

**`useRunControl` is one mutation for three verbs** because they differ only in
the verb and all three invalidate the same queries. Three near-identical hooks
would be three places to forget one of them.

`useDecide` and `useDecideBulk` both invalidate the dashboard, because a decision
empties part of the review queue the dashboard is counting.

### Which write moves which screen

```
                    ┌──────────┐ ┌──────────┐ ┌──────┐ ┌──────────┐ ┌──────────┐
                    │positions │ │dashboard │ │ runs │ │runStatus │ │candidates│
  ──────────────────┼──────────┼─┼──────────┼─┼──────┼─┼──────────┼─┼──────────┤
  useCreatePosition │    ✦     │ │    ✦     │ │      │ │          │ │          │
  useClosePosition  │    ✦     │ │    ✦     │ │      │ │          │ │          │
  useCreateRun      │          │ │    ✦     │ │  ✦   │ │          │ │          │
  useRunControl     │          │ │    ✦     │ │  ✦   │ │    ✦     │ │          │
  useDecide         │          │ │    ✦     │ │      │ │    ✦     │ │    ✦     │
  useDecideBulk     │          │ │    ✦     │ │      │ │    ✦     │ │    ✦     │
  useSignOff        │          │ │          │ │  ✦   │ │    ✦     │ │          │
                    └──────────┘ └──────────┘ └──────┘ └──────────┘ └──────────┘

  the three rubric writes do not invalidate — they WRITE the returned object in:
  useExtractRubric ─▶ setQueryData(latestRubric)
  useSaveRubric    ─▶ setQueryData(latestRubric) + invalidate approvedRubric
  useApproveRubric ─▶ setQueryData(latestRubric) + invalidate approvedRubric
```

#### One exported helper

```ts
/** Everyone in a run, in the order a reviewer should meet them. */
export function everyone(result?: RankedCandidates): CandidateSummary[] {
  return [...result.needs_review, ...result.meets_must_haves, ...result.missing_must_have];
}
```

Note the order: **needs-review first**, always.

---

## Part 7 — Identity and the session

### `session/context.ts`

```ts
export interface Session extends Identity {   // {actor: string, roles: string[]}
  setActor: (actor: string) => void;
  setRoles: (roles: string[]) => void;
}
export function useSession(): Session   // throws outside the provider
```

### `session/SessionProvider.tsx`

**Input:** `localStorage` keys `screener.actor` and `screener.roles`.
**Output:** the context value, and writes back on every change.

```ts
const DEFAULT_ACTOR = 'poc-operator';
const DEFAULT_ROLES = ['admin'];      // NOT 'auditor'
```

**`auditor` is deliberately not granted by default.** The audit reads are the
only role-gated surface in the system, and a default that quietly includes the
role turns the gate into something nobody has ever seen work. An operator adds it
deliberately — which is also how they discover it exists.

**Stored values are validated on read.** `localStorage` outlives deploys, so what
comes back was written by whatever version the reviewer last had open:

```ts
function readStored<T>(key, fallback, valid: (v: unknown) => v is T): T {
  try {
    const raw = window.localStorage.getItem(key);
    if (raw === null) return fallback;
    const parsed: unknown = JSON.parse(raw);
    return valid(parsed) ? parsed : fallback;
  } catch { return fallback; }   // private mode, or an older build's value
}
```

An unvalidated `JSON.parse` here would put an arbitrary value into the actor
header on every request — and the actor header is what the audit log records
against a decision.

Writes are wrapped in `try/catch` too: nothing in here is candidate data, and
losing it costs one retyped name.

```
   IdentityMenu (header)
   ┌──────────────────────┐
   │ Reviewing as         │
   │  [ alice           ] │────┐
   │                      │    │
   │ Roles                │    │
   │  [x] admin           │────┤
   │  [ ] auditor         │    │
   └──────────────────────┘    │
                               ▼
                    ┌──────────────────────┐
                    │  localStorage        │   survives reloads and deploys,
                    │   screener.actor     │   so it is read back through a
                    │   screener.roles     │   type guard — never a bare
                    └──────────┬───────────┘   JSON.parse
                               │
                 ┌─────────────┴─────────────┐
                 ▼                           ▼
   ┌──────────────────────────┐  ┌──────────────────────────┐
   │ EVERY query key          │  │ EVERY request header     │
   │  ['candidates',          │  │  X-Actor: alice          │
   │    {actor, roles},       │  │  X-Actor-Roles: admin    │
   │    runId]                │  │                          │
   └──────────────────────────┘  └──────────────────────────┘

   ! changing either value changes every key → the WHOLE cache re-fetches.
     Required, not incidental: an auditor gets MORE FIELDS back from the
     very same URL.
```

### Why identity is a name typed into a box

Authentication is stubbed. The `X-Actor` / `X-Actor-Roles` headers are simply
trusted by the API, which is only acceptable because it listens on loopback. The
PoC uses a typed name instead of a login screen because **the thing worth
demonstrating is separation of duties in the audit log** — one person approving a
rubric and a different one signing off the run — and a single hardcoded identity
would make that untestable.

`IdentityMenu.tsx` puts it one click away in the header for the same reason: a
reviewer working under somebody else's name should be obvious rather than
discoverable, and nobody demonstrates separation of duties if it takes three
clicks to find the control.

---

## Part 8 — The route tree

From `App.tsx`:

| URL | Component | What it is for |
|---|---|---|
| `/` | `DashboardPage` | Three counts, and the runs holding the review queue |
| `/requisitions` | `RequisitionsPage` | The list of open job postings |
| `/requisitions/new` | `NewRequisitionPage` | Folder + title + JD |
| `/requisitions/:positionId` | `RequisitionPage` | Rubric: draft → edit → approve → run |
| `/runs` | `RunsPage` | Every run, newest first |
| `/runs/:runId` | `RunLayout` → `RunProgressPage` | Live progress, controls, failed files |
| `/runs/:runId/review` | `RunLayout` → `ReviewPage` | The three groups and the decisions |
| `/audit` | `AuditLayout` → `RunStoryPage` | One run, told as a sequence |
| `/audit/record` | `AuditLayout` → `DecisionRecordPage` | One person's outcome, in full |
| `/audit/search` | `AuditLayout` → `AuditSearchPage` | The searchable log |
| anything else | `<Navigate to="/" replace />` | |

Everything sits inside `<Route element={<AppShell />}>`, which supplies the
header and an `<Outlet/>`.

```
  /ui/
   │
   ├── /                                DashboardPage
   │                                      └ StatTile ×3, review-queue table
   │
   ├── /requisitions                     RequisitionsPage
   │    ├── /new                         NewRequisitionPage
   │    │                                  └ FolderPicker
   │    └── /:positionId                 RequisitionPage
   │                                       ├ ClosePositionButton
   │                                       ├ RubricEditor
   │                                       └ RunList
   │
   ├── /runs                             RunsPage  └ RunList
   │    └── /:runId ──── RunLayout ──┬── (index)   RunProgressPage
   │                     [2 tabs]    │               ├ controls
   │                                 │               ├ EscalationMeter
   │                                 │               └ failed files table
   │                                 └── /review    ReviewPage
   │                                                 ├ EscalationMeter
   │                                                 ├ 3× Disclosure + table
   │                                                 ├ BulkDecisionForm
   │                                                 ├ CandidateDetail
   │                                                 │   ├ ParsedResumeText
   │                                                 │   ├ CriterionBlock ×n
   │                                                 │   └ DecisionForm
   │                                                 └ SignOff
   │
   └── /audit ────────── AuditLayout ┬── (index)    RunStoryPage        ⟨lazy⟩
                         [3 tabs]    ├── /record    DecisionRecordPage  ⟨lazy⟩
                         [role hint] └── /search    AuditSearchPage     ⟨lazy⟩

   anything else  ─▶  <Navigate to="/" replace/>
```

### The audit screens are code-split

```tsx
const RunStoryPage = lazy(() => import('./pages/audit/RunStoryPage')…);
const DecisionRecordPage = lazy(…);
const AuditSearchPage = lazy(…);
```

They carry the markdown renderer that turns audit rows into sentences, and a
recruiter working a queue of candidates never opens them. This is the one place
in the app where the boundary is obvious enough to be worth drawing.

### Two shell routes, not four nav links

`AppShell` has four destinations: Overview, Job Postings, Runs, Audit.

**Review is deliberately not a fifth.** It belongs to a run, is reached from one,
and a top-level "Review" that first asks *which* run is a question the navigation
should already have answered. That question is exactly what caused the Streamlit
dropdown war.

`RunLayout` and `AuditLayout` are tab shells: two and three tabs respectively,
on one route rather than several places in the navigation, because only one of
them is ever the answer to "what is happening with this run" — and which one
depends on whether it has finished, not on what the reviewer picked from a menu.

---

## Part 9 — Screen by screen

Each section gives: what triggers it, what it reads, what it renders, what the
reviewer can do, and what is validated where.

---

### The whole product, as five screens

```
  /requisitions/new     /requisitions/:id      /runs/:id     /runs/:id/review
 ┌─────────────────┐  ┌──────────────────┐  ┌────────────┐  ┌───────────────┐
 │ 1. pick folder  │  │ 2. draft rubric  │  │ 4. Start   │  │ 5. 3 groups   │
 │    server-side  │  │ 3. edit criteria │  │    progress│  │    decide each│
 │    browser      │─▶│ ██ APPROVE ██    │─▶│    5s poll │─▶│    sign off   │
 │    title + JD   │  │   ↑ the gate     │  │    ETA     │  │    export CSV │
 └─────────────────┘  └──────────────────┘  └────────────┘  └───────────────┘
   POST /positions     POST …/rubric/extract  POST …/start    POST …/decision
                       PUT  …/rubric          GET  …/status   POST …/decisions
                       POST /rubrics/…/approve      ×N        POST …/sign-off
                       POST /runs

          nothing is screened against a rubric a person has not approved
                                   ▲
                                   └─ the one gate the design exists for
```

### 9.0 `AppShell` — the frame around everything

**Trigger:** any route.
**Reads:** nothing itself; its three header widgets each read their own.
**Renders:** sticky header (title, nav, health badge, theme toggle, identity
menu) and `<main><Outlet/></main>` capped at `max-w-6xl`.

Nav links use `NavLink`, with `end: true` **only on the index route** — without
it "Overview" stays highlighted on every screen, because every path starts with
`/`.

#### `HealthBadge`

- **Trigger:** mount; refetches every 30 s.
- **In:** `useHealth()` → `GET /health` → `Health`.
- **Out:** one badge.

| Condition | Badge |
|---|---|
| `isPending` | `checking…` |
| `isError` | `API unreachable` (error tone, message in the title attribute) |
| all four checks pass | `Healthy · v{app_version}` (ok tone) |
| otherwise | warn badge; a popover lists the specific problems |

The four conditions are named individually rather than rolled into "unhealthy"
**because the operator's next action differs for each**:

| Field false | Sentence shown |
|---|---|
| `model_digest_matches_pin` | *"Model digest differs from the config pin — reproducibility is broken."* |
| `migrations_current` | *"The database schema is stale."* |
| `disk_ok` | *"Disk nearly full — N GB free."* |
| `llm_reachable` | *"The model is not reachable; screening will fail."* |

#### `ThemeToggle`

One button cycling `light → dark → system`. Not a three-way switch: this is
reached once in a while, and a single control that always shows the *current*
state needs no chrome to explain which of three buttons is active.

While the stored choice is `system`, it subscribes to
`matchMedia('(prefers-color-scheme: dark)')` so the page keeps up if the OS
setting changes while the tab is open — not only at the next reload.

`lib/theme.ts` does the work: writes `screener-theme`, toggles `.dark` on
`<html>` (Tailwind's `dark:` utilities key off the class, not the media query, so
a picked theme can override the OS), and sets `document.documentElement.style.
colorScheme` so browser-drawn controls — scrollbars, checkboxes — agree with the
page.

#### `IdentityMenu`

A Radix popover with a text field ("Reviewing as") and two role checkboxes
(`admin`, `auditor`). Changing either calls `setActor` / `setRoles`, which
persists to `localStorage` **and changes every query key**, so the whole cache
re-fetches under the new identity. That is the correct behaviour: the auditor
sees more fields, so the cached recruiter view must not be reused.

---

### 9.1 `/` — `DashboardPage`

**Trigger:** navigating to `/`.
**Reads:** `useDashboard()` → `GET /dashboard` → `Dashboard`.
**Polls:** every 5 s while `runs_in_progress > 0`.
**Role gate:** none.

> **One request, not one per run.** Assembling these numbers here — `/positions`,
> then `/runs`, then a candidate list per run — would download every résumé in
> the database to produce three integers, and would show numbers taken at three
> different instants that visibly fail to add up.

> **No role gate, deliberately, and it is a property to keep.** Nothing in the
> response names a person, a file or a reason: the size of a review queue is not
> a disclosure about the people in it.

Renders three `StatTile`s:

| Tile | Value | Hint |
|---|---|---|
| Active job postings | `open_positions` | link to `/requisitions`, or *"No job postings yet."* |
| Total applications | `applications` | *"N more waiting to be screened"* if `unscreened_files > 0`, else *"CVs screened, counted once per job posting"* |
| Candidates to review | `awaiting_review` | *"Undecided, and either escalated or not yet verified"*; `attention` styling when > 0 |

The second hint is worded carefully because the number is not simply "rows in a
table": a re-run of the same folder screens the same CV again, and counting that
as a new applicant would overstate every job posting.

Below that:

- A line naming `runs_in_progress` and warning that the numbers are still moving.
- **The review queue broken out by run** (`queues[]`), because review happens
  *inside* a run — "31 candidates to review" is a number until it says which runs
  hold them. Each row links straight to `/runs/{run_id}/review`.
- If the listed queue counts sum to less than `awaiting_review`, a line saying
  so: the table is capped server-side, the headline count is not, and saying so
  is the difference between a bounded list and a wrong total.
- An empty state when there are no postings and no applications.

`StatTile` carries a comment worth repeating: **no delta and no sparkline**,
because the system stores no history to compute one from. A trend line drawn from
the only figure available would be a picture of nothing, and a reader cannot tell
that by looking at it.

---

### 9.2 `/requisitions` — `RequisitionsPage`

**Reads:** `usePositions()` → `GET /positions` → `Position[]` (open only).
**Renders:** a table — Reference, Title, Posted by, Posted — each reference
linking to `/requisitions/{id}`. Empty state offers "Post the first one".

---

### 9.3 `/requisitions/new` — `NewRequisitionPage`

**Trigger:** the "New job posting" button, from the dashboard or the list.

Two cards side by side: **1. Resume folder**, then **2. The job**.

> The folder is chosen first because it is the part people get wrong. The title
> is typed from something already written; the folder has to be found on a server
> the reviewer cannot see, and a requisition pointed at the wrong one screens the
> wrong applicants without ever looking broken.

#### `FolderPicker` — browsing the server's filesystem

**Local state:** `path` (where you are), `typed` (search box), `offset` (page).
**Debounced:** `useDebounced(typed, 250ms)` → `search`.
**Reads:** `useFolders(path, search, offset)` → `GET /positions/folders?path=…&q=…&offset=…&limit=15`.

> **The server's filesystem, not the reviewer's.** The share is mounted on the
> screener host, so browsing here is browsing that share. A browser cannot hand a
> server a path from the machine it is running on — which is why this is a
> server-side navigator rather than a native folder dialog, and why the reviewer
> never sees or types an absolute path.

Filtering and paging are **server-side**, because the cost being avoided is
server-side: counting a folder's resumes is a recursive walk, so an unbounded
listing costs thousands of filesystem operations on a network mount. Debouncing
exists for the same reason — a request per keystroke is the expensive mistake
here, not a slightly late list.

Controls:

| Control | Effect |
|---|---|
| **Up one level** | `path` → its parent (or root) |
| **Use this folder** | selects `path` itself — a `RESUMES_DIR` pointing straight at a folder of CVs otherwise offers nothing to pick |
| **Refresh** | `page.refetch()` — use after adding a folder on the server |
| **Open** (per row) | descend, only shown when `has_subfolders` |
| **Use** (per row) | select that folder |
| **Prev / Next** | shown only when `total > 15` |

Three distinct empty states, because they mean different things:

| Situation | Message |
|---|---|
| a search is active | *"No folders matching '…'."* |
| inside a folder with no subfolders | *"No subfolders here — use **Use this folder** to screen this one."* |
| at the share root, nothing at all | a warning that `RESUMES_DIR` may not point where you think |

**The chosen folder name becomes the requisition's `reference`.** Everything
downstream — the run's folder, the containment check — derives from it.

#### `JdInput` — the job description, uploaded or pasted

**Controlled**, like `FolderPicker`: the page holds a `JdValue` (`lib/jd.ts`) and
this component replaces it. The value carries the text *and its provenance*, so
the requisition can record which document its rubric was drafted from.

**Reads:** `useConfig()` → `GET /config` → `jd_intake_mode`, the size and page
caps, and the accepted extensions. This is the only server-supplied configuration
in the app; see 6.3.

| `jd_intake_mode` | What renders |
|---|---|
| `both` (default) | the file control, and the paste box under it |
| `upload` | the file control only |
| `paste` | the paste box only — no upload |

> The server enforces the same rule independently, in `create_position` and in
> the upload endpoint. This only decides what to *render*: a stale tab offering
> the wrong control gets a sentence back explaining why, rather than a form that
> silently does nothing. **A control that only hides a button is not a control.**

**Uploading is two steps, and the second one is the point.**

```
1. FileInput → useExtractJdDocument().mutate(file)
   → POST /jd-documents   (multipart; creates NOTHING on the server)
   → { text, filename, file_sha256, page_count, ocr_used,
       chars_stripped, warnings, injection_signals, parser_version }

2. the text lands in an editable textarea — never read-only

3. the reviewer corrects it and submits it like any pasted description
```

> A two-column PDF interleaves. OCR on a scan drops a "not". The rubric drafted
> from that text is what screens people out, and the human approval gate in front
> of the rubric **cannot** catch it — the reviewer has nothing to compare the
> criteria against. Showing what the machine actually read is that same control,
> one step earlier. This is locked decision #33 (`screener_spec_v8.md`).

Three advisories can appear above the text, none of which block:

| Shown when | Says |
|---|---|
| `ocr_used` | the document had no text layer; the words were recognised from an image and may be wrong |
| `injection_signals` non-empty | something reads like an instruction to the model rather than a job requirement, naming the patterns |
| `warnings` non-empty | whatever the parser itself reported |

> The injection patterns were calibrated against **résumé** prose — "fire on what
> a résumé cannot plausibly say" — and a job description says several of those
> things routinely ("your task is to…"). So the warning points at a paragraph
> rather than making a claim about it, and the reviewer can simply edit the text.
> Blocking on it would be an adverse outcome produced by a heuristic that
> `detect_injection.py` itself calls "one cheap layer, not the defense".

#### The form

`react-hook-form` owns `title` only; the folder and the job description are
controlled values held by the page. Validation:

| Field | Rule | Where |
|---|---|---|
| `folder` | must be chosen | checked in `onSubmit`, **not** by disabling the button |
| `title` | `.trim().length > 0` | react-hook-form `validate` |
| `jd.text` | `.trim().length > 0` | checked in `onSubmit`, same reasoning as the folder |

> The folder is checked on submit rather than by disabling the button, **so the
> reason is stated**. A disabled control with no explanation is a dead end.

#### The submit path

```
onSubmit
 → create.mutate({reference: folder, title: trimmed, jd_text: jd.text.trim(),
                  jd_source, jd_filename, jd_file_sha256, jd_ocr_used})
 → POST /positions
      server: CreatePositionRequest  (lengths, sha256 pattern, extra="forbid") → 422
      server: is_safe_reference(reference)                                     → 400
      server: jd_intake_mode forbids this source                               → 400
      server: an open requisition already uses this reference                  → 409
 → onSuccess: toast.success(`Created ${reference}`)
              navigate(`/requisitions/${position.id}`)
 → onError:   toast.error(error.message)     ← already a sentence
```

> **Upload and paste converge here.** There is one path into `positions.jd_text`,
> and the text on it has always been read and accepted by a person. The
> provenance fields are a record of *origin* — `jd_file_sha256` identifies the
> uploaded document and asserts nothing about `jd_text`, because the reviewer
> edits it in between, deliberately.

---

### 9.4 `/requisitions/:positionId` — `RequisitionPage`

Before anything else on this screen: **where the job description came from.** The
header line carries `· from senior-backend.pdf` when there was a document, and an
OCR warning sits above the rubric when `jd_ocr_used`.

> A rubric drafted from OCR'd text was drafted from an approximation. The person
> approving it is the last one who can catch a requirement the recognition
> mangled, so the warning belongs *beside the criteria they are approving* and
> not only on the form where the file was uploaded — which they may never have
> seen, since a different person can raise the requisition.

**Reads four things:**

| Hook | Why |
|---|---|
| `usePositions(true)` | **includes closed** — a run outlives the post it screened for |
| `useLatestRubric(positionId)` | the editable draft |
| `useApprovedRubric(positionId)` | whether a run can be started at all |
| `useRuns()` | the runs already screened against this post |

> The whole flow lives on one screen in the order it happens — draft a rubric,
> edit it, approve it, start a run — because it is a sequence with a gate in the
> middle, and a reviewer who has to navigate between steps loses track of which
> one they are waiting on.

Header shows the title, a `closed` badge when applicable, the reference, who
posted it, and `ClosePositionButton`.

#### `ClosePositionButton`

A Radix popover confirmation. Hidden entirely once `status === 'closed'`.

> **Confirmed, because it is not visibly reversible from here.** Closing removes
> the requisition from every list in the app, and nothing in the interface
> reopens it.
>
> **The reassurance is the important half.** "Close" next to a list of candidates
> reads like a delete, and a reviewer who believes it might destroy an
> adverse-action record will simply never press it — leaving filled posts
> cluttering the queue forever. Nothing is deleted.

#### No rubric yet

A card with one button, **"Draft from the job description"** → `useExtractRubric`
→ `POST /positions/{id}/rubric/extract`. While pending it says *"This is a live
model call and can take a minute on a cold start."* (180 s client timeout.)

#### A rubric exists → `RubricEditor`

Mounted with **`key={rubric.id}`**, so a saved version *replaces* the editor's
contents rather than merging into them.

**Local state:** `criteria` (a copy), `problem` (a validation message).
**Derived:** `approved = rubric.approved_at !== null`;
`dirty = JSON.stringify(criteria) !== JSON.stringify(rubric.criteria)`.

The table has one row per criterion: id (read-only), text (`input`), must-have
(`checkbox`), weight (`select`, 1–5), and a remove button. Every control has an
`aria-label` naming the criterion.

**The id generator is subtler than it looks:**

```ts
function nextCriterionId(criteria) {
  const highest = criteria.reduce((max, c) =>
    Math.max(max, Number(/^C(\d+)$/.exec(c.id)?.[1] ?? 0)), 0);
  return `C${highest + 1}`;
}
```

> Counting collides the moment a criterion is removed from the middle: delete C2
> from four and the next added row is also called C4. That is two rows sharing a
> React key — which React resolves by reusing the wrong DOM node, so the text a
> reviewer typed appears against the other criterion — and two criteria sharing
> an id in what gets saved.

New rows are created with `claim: ''` and `claim_stale: false`, and existing rows
carry their `claim` through untouched.

**Client-side validation on save:**

| Rule | Message |
|---|---|
| `4 ≤ criteria.length ≤ 12` | *"A rubric holds between 4 and 12 criteria."* |
| every criterion has non-empty text | *"Every criterion needs text, or remove the empty row."* |

**Buttons, and when each is disabled:**

| Button | Disabled when | Does |
|---|---|---|
| Add criterion | `length >= 12` | appends a blank row locally |
| Save as new version | `!dirty` | `PUT /positions/{id}/rubric` with `base_version: rubric.version` |
| Approve version N | `approved \|\| dirty` | `POST /rubrics/{id}/approve` |
| Redraft from the job description | — | `POST …/rubric/extract` again |

Approve carries a `title` when dirty: *"Save your edits first — approval applies
to a stored version"*, so the disabled state explains itself.

**`base_version` is the concurrency control.** It is the version the editor was
looking at. Two recruiters tuning the same rubric in adjacent tabs is the
ordinary case, not the exotic one — and without it the second save silently
supersedes the first: no conflict, no error, just one person's edits gone and a
higher version number to suggest everything worked. A stale value comes back
**409**, which `explain()` renders as the server's own sentence.

An approved rubric shows an ok-toned alert: *"Approved by X. Editing below
creates a new, unapproved version — this one stays exactly as it was screened
against."*

Below the table, the must-have explainer: *"A must-have is a hard requirement:
failing one moves the candidate to the unqualified group, ranked separately and
never against the rest. Mark them sparingly."*

### The rubric's life

```
        (no rubric yet)
              │
              │  "Draft from the job description"   POST …/rubric/extract
              │  ~5 s live model call · 180 s client timeout
              ▼
       ┌─────────────┐   edit a cell    ┌─────────────┐
       │  v1  draft  │─────────────────▶│   dirty     │
       │             │◀─── (key=id) ────│  (local)    │
       └──────┬──────┘   remount clears └──────┬──────┘
              │                                │  "Save as new version"
              │  "Approve version 1"           │   PUT  base_version = 1
              │  POST /rubrics/{id}/approve    │
              │  (disabled while dirty)        ├── someone else saved first
              ▼                                │        └──▶ 409 "rubric was
       ┌─────────────┐                         │              edited by someone
       │  APPROVED   │                         ▼              else…"
       │  frozen     │                  ┌─────────────┐
       └──────┬──────┘                  │  v2  draft  │
              │                         └─────────────┘
              │  runs may now be created against THIS version
              ▼
       ┌─────────────┐
       │ POST /runs  │   editing later makes v3 — v1 stays exactly as it was
       └─────────────┘   screened against, because candidates carry its hash
```

#### Screening runs

Gated on `useApprovedRubric`:

- **null** → info alert: *"No approved rubric for this job posting yet. Approve
  one above before starting a run."*
- **present** → a paragraph naming the version, criteria count and approver, then
  **"New screening run"** → `POST /runs {position_id, rubric_id}` → on success,
  `toast.success("Snapshotted N file(s)")` and navigate to `/runs/{run.id}`.

Then `RunList` filtered to this position.

---

### 9.5 `/runs` — `RunsPage`

**Reads:** `useRuns()`, `usePositions(true)`.
**Renders:** `RunList` with a Job posting column.

`RunList` columns: Run (link), Job posting (optional), State (`RunStatePill`),
Files, **Needing review**, Started.

> The escalation rate is a column rather than something found by opening each
> run: across a page of runs it is the number that says whether the screening is
> working, and a rate climbing run over run is only visible when they are side by
> side.

`RunStatePill` tones: `pending` neutral, `running` info, `completed` ok, `empty`
neutral, `failed` error, `aborted` warn.

> **`empty` is not a failure and not a success**: the folder held nothing to
> screen, which is a fact about the share rather than about the applicants.

---

### 9.6 `/runs/:runId` — `RunLayout` + `RunProgressPage`

#### `RunLayout` (the shell)

**Reads:** `useRuns()`, `usePositions(true)`, `useRunStatus(runId)`.
**Renders:** breadcrumb (Job Postings / reference / runs / runId), the title with
a live `RunStatePill`, a line with the folder, file count and who started it, and
two tabs — **Progress** and **Review** — then `<Outlet/>`.

`state = status.data?.status ?? run?.status` — the polled status wins, the list's
copy is the fallback while it loads.

### A run's two axes: status and phase

```
  status ──────────────────────────────────────────────────────────────
                    ┌──▶ empty        the folder held nothing to screen
                    │                 (NOT a failure and NOT a success)
     create_run ────┤
                    └──▶ pending ──▶ running ──┬──▶ completed
                          ▲                    ├──▶ aborted    (resumable)
                          │                    └──▶ failed
                          └── abort, then start again


  phase  ──────────────────────────────────────────────────────────────
     judge ─────────────────────▶ verify ─────────────▶ done
     every file screened once     second-model pass     nothing pending
       ▲                                                     │
       └───────── rescan adds files to a finished run ────────┘


  what RunProgressPage shows for each phase
     judge   →  heading "Screening"    bar = (done+failed)/total
     verify  →  heading "Verifying"    bar + "second-model checks X of Y"
     done    →  heading "Finished"     polling stops
```

#### `RunProgressPage`

**Reads:** `useRunStatus(runId)` — polls every 5 s while live, stops when not.

> Polled every five seconds, not pushed: a job that updates every few seconds
> over more than an hour does not justify a persistent connection. The poll stops
> when the run does, so a finished run left open on a second monitor costs
> nothing.

**Controls card** — three buttons, all through `useRunControl`:

| Button | Endpoint | Success message |
|---|---|---|
| Start screening | `POST /runs/{id}/start` | *"N file(s) queued — the worker will pick them up."* |
| Rescan folder | `POST /runs/{id}/rescan` | *"N new file(s) added."* |
| Abort | `POST /runs/{id}/abort` | *"Run aborted. It can be started again later."* |

Each outcome is **said as a number rather than as "done"**. The card hint states
the rule: *"Nothing is ever silently added to a run in progress — a folder that
has grown since the snapshot needs an explicit rescan."*

Abort is the `danger` variant with a title: *"Stops the run. Already-screened
results are kept."*

**Progress card** — heading depends on `phase`:

| `phase` | Heading |
|---|---|
| `judge` | Screening |
| `verify` | Verifying |
| `done` | Finished |

A real `role="progressbar"` with `aria-valuemin/max/now`, filled to
`(done + failed) / total`. Under it: *"N of M screened"*, plus *"· second-model
checks X of Y"* during the verify phase.

Five metrics: Screened (`done`), Failed (`failed`), Remaining
(`pending + claimed`), ETA (`duration(eta_seconds)`), Undecided
(`undecided_count`). And, when non-zero, *"N file(s) from earlier runs are ahead
in the queue."* — the queue is strict FIFO across runs.

While live, an aside spins: *"live, refreshing every 5s"*.

**Needing review card** — `EscalationMeter` with the live rate and breakdown, and
a link to the review tab.

**Failed files** — rendered only when non-empty, as an error alert plus a table
(File, Phase, Attempts, Last error):

> These produced no candidate, so they appear in none of the review groups — a
> count alone leaves a reviewer no way to find out who is missing from the
> results, or to tell "nobody applied" from "three CVs were never read".

The alert says explicitly: *"This is an infrastructure failure, not a judgement
about the applicant. Sign-off is blocked until they are resolved."*

#### `EscalationMeter`

**In:** `rate`, optional `count`/`total`, optional `breakdown`.
**Constant:** `ESCALATION_BUDGET = 0.03` from `lib/labels.ts`.

Renders the percentage, a `±N.N pts vs 3% budget` badge, and — when over budget —
a warning:

> *"X% of candidates need a human decision, against a 3% design budget. A queue
> larger than a person will genuinely read is the point at which review stops
> being meaningful."*

`EscalationBreakdown` renders counts **grouped by reason**, sorted descending:

> "23 need review" prompts a shrug. "8 unverified evidence, 7 judge disagreement"
> tells a reviewer that similar cases can be worked in a batch, which is the
> difference between a queue that gets cleared and one that does not.

⚠️ **The 3% here is a display constant.** The server's `escalation_budget`
setting is deliberately unset (see `CODEBASE_GUIDE.md` Part 9). This number is
the on-screen reference, not a measurement.

---

### 9.7 `/runs/:runId/review` — `ReviewPage`

The screen the product exists for.

**Reads:** `useCandidates(runId)` → `GET /runs/{id}/candidates` → `RankedCandidates`.
**URL state:** `?c=<file_sha256>` selects a candidate, via `useSearchParams`.

```ts
setParams(next, { replace: true });
```

> `replace` so browsing candidates does not bury the run page under fifty history
> entries a reviewer has to click back through.

### What the screen looks like

```
┌────────────────────────────────────────────────────────────────────────────┐
│ Needing review   4.2%  (12 of 284)   [ +1.2 pts vs 3% budget ]             │
│ [8 unverified evidence] [3 judge disagreement] [1 negation]                │
└────────────────────────────────────────────────────────────────────────────┘
┌──────────────────────────────┐ ┌───────────────────────────────────────────┐
│ ▼ Could not be scored   [12] │ │ [B]  alice-chen.pdf                       │
│   not ranked, not scored —   │ │       verified · score 7.8/10, exported   │
│   a statement about the      │ │       for audit       [Original document] │
│   system, not the candidate  │ │                                           │
│  ┌──┬───────────────┬──────┐ │ │ ▶ Parsed resume text                      │
│  │ —│ bad-scan.pdf  │undec.│ │ │                                           │
│  │ —│ broken.docx   │undec.│ │ │ ! Needs a human decision because          │
│  └──┴───────────────┴──────┘ │ │     · unverified evidence                 │
│  [Export CSV]                │ │       the quote could not be matched back │
│                              │ │                                           │
│ ▶ Meets all must-haves [201] │ │ Criteria     (met, then partial, then not)│
│   [Export CSV]               │ │ ┌───────────────────────────────────────┐ │
│   ▸ Decide on all 201 at once│ │ │ met · Production Python · w4 · MUST   │ │
│                              │ │ │ verified                              │ │
│ ▶ Missing a must-have   [71] │ │ │ "…led a team of six building the      │ │
│   ranked among themselves,   │ │ │  ▓▓payments platform in Python▓▓      │ │
│   never against the group    │ │ │  across three regions…"               │ │
│   above                      │ │ ├───────────────────────────────────────┤ │
│                              │ │ │ not met · Kubernetes · w2             │ │
│  ▲ needs-review is FIRST     │ │ │ ! A second model disagrees—you decide │ │
│    and OPEN by default       │ │ └───────────────────────────────────────┘ │
│                              │ │                                           │
│                              │ │ Your decision                             │
│                              │ │  ( ) advance   ( ) hold   ( ) reject      │
│                              │ │  Reason [ required — it is the record  ]  │
│                              │ │  [ Record decision ]                      │
│                              │ │                                           │
│                              │ │                                           │
└──────────────────────────────┘ └───────────────────────────────────────────┘
┌────────────────────────────────────────────────────────────────────────────┐
│ Sign off   ! 12 candidate(s) still need a decision.  [Sign off]            │
└────────────────────────────────────────────────────────────────────────────┘
```

#### The three groups

| Order | Key | Title on screen | Ranked? | Open by default |
|---|---|---|---|---|
| 1 | `needs-review` | **Could not be scored** | no | **always** |
| 2 | `qualified` | Meets all must-haves | yes | only if group 1 is empty |
| 3 | `unqualified` | Missing a must-have | among themselves | no |

> **`needs_review` is its own group, first, and open.** At 1,000 applicants a
> reviewer reads the top of Band A and stops. An unscoreable candidate parked at
> the bottom of one long list is invisible in practice — which is the adverse
> outcome escalation was redesigned to prevent.
>
> **Missing a must-have is a separate group, not a low score.** Those candidates
> are ranked among themselves and never against the ones who met the hard
> requirements; the two are not comparable, and a single list implies they are.

The first group's title is **"Could not be scored"**, not "needs review":

> That language is reserved for the row-level flag a ranked candidate can also
> carry (a partial must-have, say). This group is a different, more severe thing:
> no score and no rank exist at all.

Its note: *"The system could not produce a reliable result for these. They are
not ranked and not scored — that is a statement about the system, not about the
candidate."*

#### The candidate table

Three columns: Band (`BandPill`), Candidate (name button + a sub-line), Decision.

The sub-line is `VERIFICATION_BADGE[verification_status]` plus any flags:

| `verification_status` | Shown as |
|---|---|
| `done` | verified |
| `pending` | provisional |
| `skipped` | not verified |

Row styling, and the comment explaining the order:

```tsx
candidate.file_sha256 === selectedHash
  ? 'bg-blue-50 dark:bg-blue-950/40'
  : candidate.review_required && 'bg-amber-50 dark:bg-amber-950/30'
```

> Selection wins when both apply — two same-specificity background utilities
> would otherwise depend on stylesheet order, not this list. A candidate can
> carry `review_required` inside the qualified or unqualified groups too, so this
> cannot rely on group placement alone to surface it.

Selection is also announced with `aria-current="true"` on the row: a highlight
alone is invisible to anyone using a screen reader.

`BandPill` shows `A`/`B`/`C`/`D`, or `—` when `band` is null, with `BAND_HELP` in
the title attribute.

> **Reviewers see a band, not a score.** Three verdict levels across at most
> twelve criteria cannot support a rendered precision of `7.8`. The decimal
> implies resolution that does not exist and invites over-reliance on a number
> the system cannot justify to that precision.

#### CSV export (per group)

`downloadCsv(`${runId}-${group.key}.csv`, exportCsv(group.candidates))`.

Columns: filename, file_sha256, band, **score**, must_haves_met, scoreable,
review_required, decision, decided_by, decided_at, verification_status,
escalation_reasons (`|`-joined), flags (`|`-joined), scored_at.

The numeric score **is** in the export — the button's title says so: *"Includes
the numeric score for audit. On screen, reviewers see bands."*

`format.toCsv` uses `papaparse`, because quoting is the part of CSV that is easy
to get wrong and a resume filename with a comma in it is not exotic.
`downloadCsv` builds a blob locally:

> The rows are already in this tab — asking the API to render a CSV it does not
> otherwise produce would add an egress path for candidate data that does not
> need to exist.

#### `BulkDecisionForm` (groups 2 and 3 only)

Rendered for the ranked groups, **never** for needs-review.

**Eligibility filter, client-side:**

```ts
const eligible = candidates.filter(c => !c.review_required && c.id !== null);
```

Three render paths:

| Situation | What is shown |
|---|---|
| group empty | nothing |
| group non-empty but nothing eligible | an info alert: *"Every candidate in this group needs review — open each one to decide."* |
| some eligible | a `<Disclosure>` with the form |

> Rendering nothing in the middle case looks identical to the control being
> broken. Say why, and point at the way to actually decide them.

The form: three radios (`advance` / `hold` / `reject`, defaulting to `reject`)
and a **required shared reason**. When some are excluded it says so: *"N of M in
this group — the other X need review and are decided individually, not in bulk."*

On success it reports both halves:

```
toast.success(`Recorded for ${result.decided.length}.`)
if (result.skipped.length) toast.warning(`${n} skipped — they need review and
                                          have to be opened individually.`)
```

**The server skips escalated candidates regardless of what this sends.** The
client filter is a courtesy; the server's `record_bulk_decision` excludes
`review_required` *and* `verification_status === 'pending'` and returns the
skipped ids.

> Bulk actions exist because a reviewer working 400 clear rejections one modal at
> a time will stop reading them. The exclusion exists because the escalated ones
> are precisely those where the system said *a human has to look at this*.

#### `CandidateDetail` — the right-hand panel

**In:** one `CandidateSummary` and the `runId`.

Rendered in the order the decision is actually made:

**1. Header** — `BandPill`, filename, and a sub-line:

```
{verified|provisional|not verified} · internal score 7.8/10, exported for audit
                                    ·  or "not scored"
```

Plus an **Original document** link (`api.fileUrl(candidate.id)`, new tab), hidden
when `candidate.id === null`.

**Focus management:** choosing a candidate replaces the whole panel while the
reviewer's focus is still back in the list, so an effect keyed on
`candidate.file_sha256` moves focus to the heading (`tabIndex={-1}`). The next
Tab then lands in the assessment they just opened rather than on the following
name in the list.

**2. `ParsedResumeText`** — a collapsed `<details>` with the text the parser
extracted, in a `<pre className="whitespace-pre-wrap">`.

> Distinct from both the per-criterion evidence snippets and the "Original
> document" link. Rendered with the original line breaks intact rather than
> reflowed or split into invented sections: the backend has no notion of resume
> sections, so anything beyond preserving whitespace would be structure this
> component made up.

**3. Banners, in order:**

| Condition | Banner |
|---|---|
| `verification_status === 'pending'` | warn, **"Provisional"** — *"The second model has not checked this candidate yet. Escalations it would raise are not shown below."* |
| `score === null` | info — *"Not scored, and not ranked. That is a statement about the system, not about the candidate."* |
| `escalation_reasons.length > 0` | warn, **"Needs a human decision because"** — a list, each with `escalationLabel` and `escalationHelp` |
| each `flag` | warn, titled with the flag, body from `flagHelp(flag)` |

A half-verified result that renders like a finished one is how someone signs off
on work that has not happened yet — hence the first banner.

**`SUSPECTED_INJECTION` gets extra treatment.** When `injection_findings` is
non-empty, the flag's alert also lists each finding as `signal` + `<q>excerpt</q>`:

> The grounds, not just the verdict. Deciding this flag means deciding whether
> the matched text is an attack or is the candidate describing their job, and
> that is unanswerable without seeing the text. The excerpt is attacker-controlled
> resume content, so it goes to the DOM as a JSX child and never as markup.

**4. Summary, notable strengths, red flags** — plain paragraphs, when present.

**5. Criteria** — sorted by verdict, not by rubric order:

```ts
const VERDICT_ORDER = { strong: 0, partial: 1, none: 2 };
```

> Met, then partial, then not found — the order a reviewer actually wants to scan
> in. The sort is stable, so criteria sharing a verdict keep the rubric's order
> among themselves.

Preceded by a caveat that matters: *"Evidence is quoted from the candidate's own
document. It confirms the model read the resume faithfully — it cannot tell a
true claim from a false one, and it is not fraud detection."*

**6. `DecisionForm`.**

#### `CriterionBlock` — one criterion

**In:** a `CriterionView` and the candidate's `resume_text`.

Header row: verdict badge (`VERDICT_LABEL`: strong → *met*, partial → *partial*,
none → *not met*), the criterion text, `weight N`, a `must-have` badge, and an
evidence badge.

**The evidence badge has a special case:**

```ts
const context = quoteInContext(resumeText, criterion.highlights);
const badge = criterion.evidence_status === 'unverified' && context
  ? EVIDENCE_BADGE_WEAK_MATCH          // "found, but too weak to confirm"
  : EVIDENCE_BADGE[criterion.evidence_status];
```

> `unverified` covers two different situations the single badge cannot tell
> apart: nothing in the document resembles the quote, versus a real fragment was
> found but was too short or weak to clear the verification bar. This component
> already knows which — it has to, to decide whether there is a highlight to
> render.

**Then the evidence itself, in three mutually exclusive shapes:**

| Situation | Rendered as |
|---|---|
| there is a highlight | `<blockquote>` with `before`, `<mark>matched</mark>`, `after`, and `…` where truncated |
| `evidence_status === 'not_applicable'` (verdict is `none`) | a plain `<blockquote>` — an honest "not found" answer |
| evidence exists but nothing matched | a **warn `Alert`**, titled *"Not found in the resume"*, never a blockquote |

> No highlight means nothing in the document backs this up — never a
> `<blockquote>`, which would tell the reviewer it came from the resume when it
> did not. Seen in practice: the model echoed the criterion's own wording back as
> its "quote".

Then, when set:

- `negation_suspected` → *"A negation appears just before this quote — read the
  full sentence."*
- `verifier` → an info alert titled **"A second model disagrees — you decide"**,
  with the suggested verdict, the rationale, and any evidence it found.

> "A second model disagrees — you decide", never "the correct answer is".
> Presented as an answer, reviewers defer to it, and automated decision-making
> returns through the interface.

### How one criterion decides what to render

```
                      criterion.evidence is set?
                                 │
                  ┌──── no ──────┴────── yes ─────┐
                  ▼                               ▼
            render nothing        quoteInContext(resume_text, highlights)
                                    · min(starts) .. max(ends)
                                    · ±240 chars either side
                                    · null if out of range / no highlights
                                                  │
                              ┌──── found ────────┴───── null ────┐
                              ▼                                   ▼
                   ┌──────────────────────┐         evidence_status is
                   │ <blockquote>         │           'not_applicable'?
                   │  …before             │                  │
                   │  <mark>matched</mark>│        ┌── yes ───┴─── no ───┐
                   │  after…              │        ▼                     ▼
                   └──────────────────────┘  <blockquote>        ⚠ warn Alert
                    the quote INSIDE its      an honest           "Not found in
                    paragraph — a bare        "not found"          the resume"
                    quote can be cherry-      answer               + the text in
                    picked from a sentence                          italics
                    that said the opposite                          NEVER a quote

  and the badge:
      evidence_status === 'unverified'  AND  a context was found
             └──▶ "found, but too weak to confirm"
      otherwise
             └──▶ EVIDENCE_BADGE[evidence_status]
```

#### `lib/highlight.ts :: quoteInContext`

**In:** `resumeText`, `HighlightSpan[]`.
**Out:** `{before, matched, after, truncatedStart, truncatedEnd}` or `null`.

```ts
const CONTEXT_CHARS = 240;
const start = Math.min(...highlights.map(h => h.start));
const end   = Math.max(...highlights.map(h => h.end));
if (start < 0 || end > resumeText.length || start >= end) return null;
```

Returns `null` for empty text, no highlights, or out-of-range offsets — which is
what drives the "Not found in the resume" branch above.

> **Offsets arrive already translated** out of the redacted `sent_text` by the
> server's read layer, so this only has to slice. Doing the translation here
> would put it downstream of the boundary that owns it, in the one process with
> no server tests.

That translation is `screener/core/offsets.py`, applied in
`schemas.criterion_view`. The UI never sees `sent_text` unless the actor is an
auditor.

Showing the quote inside its paragraph is not decoration: **a bare quote can be
cherry-picked from a sentence that said the opposite.**

#### `DecisionForm`

**In:** one candidate, the `runId`.
**Guard:** if `candidate.id === null`, renders a warn alert instead of a form —
*"This file produced no candidate record, so no decision can be recorded against
it. It is listed under the run's failed files."*

Fields: three radios (default `advance`) and a **required** reason.

| Rule | Message |
|---|---|
| `reason.trim().length > 0` | *"A reason is required. It is the record."* |

> The reason is required and is not a formality: it is the adverse-action record,
> the answer to "why was I rejected", and it is stored against the reviewer's
> name.

When the candidate already has a decision, an ok-toned alert says who recorded it
and that *"Recording another decision appends to the history; it does not erase
this one."*

On success: toast, and `reset({decision: kept, reason: ''})` — the verdict choice
is kept, the reason is cleared, because the next candidate needs its own reason.

The mutation invalidates `candidates`, `runStatus` and `dashboard`, so the row's
Decision column, the undecided count and the dashboard tile all move together.

#### Sign-off

```ts
const outstanding = everyone.filter(c =>
  c.decision === 'undecided' &&
  (c.review_required || c.verification_status === 'pending'));
```

The button is disabled while `outstanding.length > 0`, with a warn alert naming
the count.

> **Gated on the same condition the server enforces.** Mirroring the precondition
> rather than counting the visible rows means the warning and the refusal agree.
> A screen that says "ready" and a button that comes back 400 teaches reviewers
> to distrust the screen.

---

### 9.8 `/audit` — `AuditLayout` and its three tabs

#### `AuditLayout`

**Reads:** `useSession()` only.

Renders the heading, a standing note — *"An append-only record of every decision
and mutation, enforced by database triggers rather than by convention. That is
the property that makes it evidence."* — and, **when `roles` lacks `auditor`**, a
warning telling the reviewer exactly how to fix it:

> *"The audit log names who did what and carries decision reasons, so it is
> gated. Add `auditor` to your roles under your name, top right."*

This is a **hint, not the gate.** The three endpoints behind these tabs return
403 regardless of what the client believes. The client-side check exists so the
reviewer is told what to do instead of watching three panels fail.

Three tabs: Run story · Decision record · Search the log.

> **Scoped to one run by default, searchable second.** The question people bring
> to an audit log is "show me how this hiring decision was made and prove a human
> was involved", and that question is always about one thing. A chronological
> dump of every row answers a different and rarer question.

#### `/audit` — `RunStoryPage`

**URL state:** `?run=<runId>`.
**Reads:** `useRunStory(runId)` → `GET /runs/{id}/story` → `RunStory`. `retry: false`.

Header names the position reference, the run id, the rubric version, who approved
it and who signed it off.

**`SeparationOfDuties`** — three outcomes:

| Condition | Alert |
|---|---|
| `!signed_off_by` | warn — *"Not signed off yet — no one has accepted these results."* |
| `separation_of_duties` true | ok — *"X approved the rubric and Y signed off the run."* |
| both the same person | warn — *"X both approved the rubric and signed off the run. Permitted, but a second reviewer is the stronger record."* |

> Permitted by the system — the flow is demonstrable with one operator — but it
> is the first thing an auditor checks, so the record says it outright instead of
> leaving it to be reconstructed from two lines of the log.

When `candidate_events_truncated`, an info alert says per-candidate decisions are
truncated while the run-level record is complete.

**The timeline** — an ordered list, each row: timestamp, an optional marker
badge, the actor, and the event as a sentence.

```ts
const marker = HUMAN_GATES.has(action) ? 'human gate'
             : action === 'decision'   ? 'decision'
             : '';
// HUMAN_GATES = {'approve_rubric', 'sign_off_run'}
```

> The human gates and the decisions are what an auditor scans for, so they keep a
> label; everything else is unmarked so the labels stay meaningful.

`actor_id === null` renders as *System* in italics, not as a blank:

> `actor_id` is null for work no person did. Rendering that as a blank loses a
> fact the record is making deliberately.

(On the server side, `advance_phase`, `complete_run`, `reclaim_orphaned` and
`reclaim_expired` are all written with no actor, precisely so a crash recovery is
never indistinguishable from a person's decision.)

#### `/audit/record` — `DecisionRecordPage`

**URL state:** `?run=<runId>&candidate=<id>`.
**Reads:** `useCandidates(runId)` for the picker, `useAdverseActionRecord(id)`
for the record (`enabled` only once an id exists).

The picker lists only candidates with `id !== null` and `decision !== 'undecided'`.
When there are none: *"No decisions recorded for this run yet. A record exists
once a reviewer has advanced, held or rejected someone."*

> The artefact handed to a regulator or to the applicant. Deliberately available
> for **every** decision rather than only rejections: a record that exists solely
> for adverse outcomes cannot be checked against a favourable one, and that
> comparison is the first test of whether it is honest.

The record renders:

1. **Header** — filename, the outcome in caps, the job posting, run id, and when
   it was screened. A `REJECT` gets a red alert: *"This is an adverse outcome.
   The grounds below are the record of why."*
2. **Grounds given** — every step of `history`, each showing who changed what
   from what to what and when, with the reason in an alert. **Never truncated:
   this is the answer to "why was I rejected".** No history at all gets a warn
   alert saying so.
3. **Assessment against the approved rubric** — band, score, must-haves; or
   *"Not scored"* when `!scoreable`.
4. **The criteria table**, with three verdict columns:

   | Column | Meaning |
   |---|---|
   | **Judge said** (`model_verdict`) | the first model's verdict, before the consistency gate |
   | **Final verdict** (`verdict`) | what stood |
   | **Quote found** (`verified`) | whether the evidence matched the document |
   | **Verifier** | what the second model made of it |

   > Three columns, three passes. Collapsing them loses the disagreement.

**`model_verdict` appears here and nowhere else in the app.** It is auditor-only
data. `schemas.py` keeps it off the reviewer's screen deliberately: showing both
the forced verdict and the original invites exactly the second-guessing the
consistency gate exists to remove.

#### `/audit/search` — `AuditSearchPage`

**URL state:** every filter. `actor_id`, `action`, `entity`, `entity_id`,
`since`, `until`, `offset`.

> The filters live in the URL rather than in component state: an auditor who has
> narrowed to one actor over one week needs to be able to send that view to
> somebody, and to still have it after a reload.

Six filter controls; `action` is a `<select>` populated from `KNOWN_ACTIONS` in
`lib/labels.ts`. Changing any filter other than `offset` deletes `offset` — a new
filter starts at page one.

**One date subtlety:**

```ts
function nextDay(isoDate: string): string   // 'until' is advanced by one day
```

> The end date advances a day so the day itself is included rather than cut off
> at midnight, which would silently drop everything done that day.

Results are a table (When / Who / What / Entity), 50 per page, with Prev/Next and
an `N–M of T` counter. `actor_id === null` renders as *system*.

---

## Part 10 — Every validation, in one table

The client validates so the reviewer gets an immediate, specific message. **The
server validates because it is the only place that can be trusted.** Nothing in
this table is client-only in effect.

| # | What | Client check | Where | Server check | Failure the user sees |
|---|---|---|---|---|---|
| 1 | Resume folder chosen | `if (!folder)` on submit | `NewRequisitionPage` | — | inline error alert |
| 2 | Job title non-empty | `value.trim().length > 0` | react-hook-form | `CreatePositionRequest` min_length | inline error alert |
| 3 | JD non-empty | `if (!jd.text.trim())` on submit | `NewRequisitionPage` | `CreatePositionRequest` min_length | inline error alert |
| 3a | JD file type | `accept` on the input — **a convenience, not a check** | `FileInput` | `validate_file` sniffs magic bytes; the extension is not consulted for the type | **400** → alert with the server's sentence |
| 3b | JD file size | — | — | ASGI middleware, **before** the body is buffered | **413** → alert |
| 3c | JD page count | — | — | pre-parse hint **and** post-parse count (§8.2 sees nothing on a modern PDF) | **400** → alert |
| 3d | Intake method allowed | renders only the permitted controls | `JdInput` + `useConfig` | `jd_intake_mode` in both `create_position` and the upload endpoint | **400** → alert / toast |
| 3e | Parser capacity | — | — | non-blocking semaphore, `jd_max_concurrent_parses` | **503** + `Retry-After` → alert |
| 4 | Reference is a safe folder name | — | — | `core/resume_paths.is_safe_reference` | **400** → toast |
| 5 | Reference not already open | — | — | `positions_store.get_open_by_reference` | **409** → toast |
| 6 | Rubric has 4–12 criteria | `MIN/MAX_CRITERIA` | `RubricEditor.onSave` | pydantic | inline `problem` alert |
| 7 | Every criterion has text | `.some(c => !c.text.trim())` | `RubricEditor.onSave` | pydantic | inline `problem` alert |
| 8 | Criterion ids unique | `nextCriterionId` from the max | `RubricEditor` | — | (prevented, not reported) |
| 9 | Not editing someone else's version | `baseVersion: rubric.version` sent | `RubricEditor` | `service.save_rubric` compares to latest | **409** → toast with the server's sentence |
| 10 | Approve only a saved version | `disabled={approved \|\| dirty}` + title | `RubricEditor` | — | disabled button that explains itself |
| 11 | Run needs an approved rubric | button only rendered when `approvedRubric !== null` | `RequisitionPage` | `rubrics_store.is_approved` | **409** → toast |
| 12 | Run has files to start | — | — | `service.start_run` (`progress.total == 0`) | **400** → toast |
| 13 | Decision reason non-empty | `value.trim().length > 0` | `DecisionForm` | `service.record_decision` | inline error alert |
| 14 | Bulk reason non-empty | `value.trim().length > 0` | `BulkDecisionForm` | `service.record_decision` | inline error alert |
| 15 | Decision is one of three | radios from `DECISIONS` | `types.ts` | `service.record_decision` | (prevented) |
| 16 | No bulk decision on escalated candidates | `!c.review_required` filter | `BulkDecisionForm` | `record_bulk_decision` skips and reports | warning toast naming the count |
| 17 | Candidate has a record to decide against | `candidate.id !== null` | `DecisionForm` | — | warn alert instead of the form |
| 18 | Sign-off blocked with an unread queue | `outstanding` mirrors the server rule | `ReviewPage.SignOff` | `service.sign_off_run` | disabled button + warn alert |
| 19 | Auditor role for audit reads | `roles.includes('auditor')` | `AuditLayout` | `AUDITOR_ROLE not in actor.roles` | warn alert; **403** → *"You do not have the role this needs…"* |
| 20 | Stored identity is the right shape | `isName` / `isRoleList` type guards | `SessionProvider` | — | falls back to defaults |
| 21 | Highlight offsets in range | `start < 0 \|\| end > len \|\| start >= end` | `lib/highlight.ts` | `core/offsets.py` translates | "Not found in the resume" alert |
| 22 | Folder search does not flood the API | `useDebounced(250ms)` | `FolderPicker` | server pages at ≤100 | (prevented) |

```
   the client                                   the server
   ══════════                                   ══════════
   PREDICTS the refusal                         MAKES the refusal
   so the reviewer is not surprised             the only one that counts

   folder chosen ·································· (nothing — it is a UI concept)
   title / JD non-empty ··························· CreatePositionRequest    → 422
        ·········································· is_safe_reference        → 400
        ·········································· reference already open   → 409
   4–12 criteria, none empty ······················ pydantic
   base_version sent with the save ················ compared to latest       → 409
   Approve disabled while dirty ··················· (nothing)
   run button hidden without an approved rubric ··· rubrics_store.is_approved→ 409
        ·········································· run has no queued files  → 400
   reason non-empty ······························· record_decision          → 400
   bulk form filters review_required ·············· record_bulk_decision SKIPS
                                                       and returns the ids
   sign-off mirrors the precondition ·············· sign_off_run             → 400
   auditor hint in AuditLayout ···················· AUDITOR_ROLE check       → 403

   ─────────────────────────────────────────────────────────────────────────
   Where the two ever disagree, the server wins and the client is the bug.
```

**Where the two disagree, the server wins and the client is the one at fault.**
Items 9, 16, 18 and 19 are the ones where that is most visible, and in all four
the client's job is only to *predict* the refusal so the reviewer is not
surprised by it.

---

## Part 11 — The shared vocabulary (`src/lib/`)

### `labels.ts` — the words on the screen

> These are not UI strings in the decorative sense. Each one is a decision about
> what a reviewer is told when the system is uncertain, and several exist to stop
> a specific way that human oversight quietly stops working — a flag that reads
> as a judgement about the candidate, a second model presented as the right
> answer, a count with no reason attached.

**Read this file early.** It is where the judgement about what a reviewer is told
actually lives.

Contents:

| Export | What it is |
|---|---|
| `ESCALATION_BUDGET` | `0.03` — the display constant |
| `BAND_HELP` | A/B/C/D → one line each, used as pill tooltips |
| `FLAG_HELP` / `flagHelp()` | 19 flags → what each means, in a sentence |
| `ESCALATION_LABELS` / `escalationLabel()` | wire value → short label |
| `ESCALATION_HELP` / `escalationHelp()` | wire value → why it applies |
| `EVIDENCE_BADGE` | evidence status → badge text |
| `EVIDENCE_BADGE_WEAK_MATCH` | the special case described in §9.7 |
| `VERIFICATION_BADGE` | done/pending/skipped → verified/provisional/not verified |
| `VERDICT_LABEL` | strong/partial/none → met/partial/not met |
| `KNOWN_ACTIONS` | the audit filter dropdown |
| `HUMAN_GATES` | `{approve_rubric, sign_off_run}` |
| `describeEvent()` | one audit row → one markdown sentence |

**Two of the entries are worth reading in full.**

`FLAG_HELP.SUSPECTED_INJECTION`:

> *"The document contains instruction-like text. This is a prompt for a human
> look, not a judgement about the candidate — a security engineer's CV
> legitimately trips it."*

`ESCALATION_HELP.PARTIAL_MUST_HAVE`, which exists because the short label reads
as a contradiction next to "Meets all must-haves":

> *"A must-have was scored partial rather than strong. It still counts as met —
> an unmet must-have would place the candidate in a different group entirely —
> but a human should confirm the evidence is strong enough on its own."*

**A bug the file's own comment records:** the escalation keys are UPPER_CASE
because `EscalationReason` is a Python `StrEnum` whose `.value` *is* the member
name (`"UNVERIFIED_EVIDENCE"`), not a lower-cased form of it. Lower-case keys
never matched, and every reason silently rendered as its raw upper-case name via
the fallback.

#### `describeEvent(action, detail)`

**In:** an action string and the untyped `detail` JSON.
**Out:** a markdown sentence, rendered by `ui/Markdown`.

```ts
create_position: d => `Posted job **${text(d.reference, '?')}**.`
approve_rubric:  d => `**Approved the rubric** (\`${short(d.rubric_hash)}\`).`
complete_run:    d => `Screening complete — ${d.done} judged, ${d.failed} failed,
                       **${pct(d.escalation_rate)} escalated**.`
```

Three defences are built in:

1. **`text(value)` never does `String(object)`.** `detail` is untyped JSON by
   design — a new audited action writes whatever it has. `String({})` renders
   `[object Object]` in a compliance record, which reads as a bug in the audit
   log rather than as an unexpected shape.
2. **An unknown action degrades to showing the raw row**, never to a blank line
   on a compliance screen.
3. **A throwing renderer is caught** and falls back to the raw row, so a
   malformed detail cannot blank the screen.

`decision` gets its own renderer, because the reason is the point — that string
is the adverse-action justification, so it is never truncated and never hidden
behind a toggle.

### `format.ts`

| Function | Does | Why not by hand |
|---|---|---|
| `dateTime(v)` | `2026-05-04 11:22` | `date-fns` parses what pydantic emits (sometimes zoned, sometimes not) and formats in the reviewer's offset. Hand-slicing showed everyone UTC and called it local time |
| `date(v)` | `2026-05-04` | tables where the time is noise |
| `compactCount(n)` | `1,284` / `12.9K` above 100k | exact while a reviewer plans around it; compact when eight digits stop being one glance |
| `percent(v, digits)` | `3%` | |
| `duration(s)` | *"about 20 minutes"* | `formatDistanceStrict` |
| `shortHash(v, n=12)` | first 12 chars, or `—` | |
| `toCsv(rows)` | papaparse `unparse` | quoting is the easy part of CSV to get wrong |
| `downloadCsv(name, csv)` | blob + click + revoke | keeps candidate data off a new server route |

### `escalations.ts`

```ts
countEscalations(reasons: string[][]): Record<string, number>
```

The run-status endpoint returns this breakdown ready-made while a run is in
flight; the review screen has only the candidate list, so it derives the same
shape rather than showing one undifferentiated count.

### `theme.ts` and `useDebounced.ts`

Covered in §9.0 and §9.3 respectively.

---

## Part 12 — The UI primitives (`src/ui/`)

Thin wrappers, no domain knowledge, no data fetching. Several exist because the
naive version has a specific failure.

| Module | What it is | The reason it is not inline |
|---|---|---|
| `cn.ts` | `twMerge(clsx(...))` | Without `tailwind-merge`, `<Button className="px-2">` produces `px-4 px-2` and which one applies depends on the order Tailwind happened to emit them |
| `Button` | `cva` variants: primary / default / ghost / danger, md / sm, `busy` spinner | |
| `Alert` | tones info / ok / warn / error, each with its own icon | Colour alone is not a signal |
| `Badge` | five tones, pill-shaped | |
| `Card` / `CardBody` / `CardHeader` | the one container in the app | |
| `Field` / `Choice` | a `<label>` **wrapping** its input | No `id` to collide when the same form renders twice on a screen — which the review page does for every candidate group |
| `FileInput` | one file, by click or drop; the native control is `sr-only` and driven by a `<label>` | `index.css` styles every `input` that is not a checkbox or radio, which catches `type="file"` and wraps a browser-drawn button in a text-field border. Hiding it visually while leaving it in the DOM keeps the keyboard and screen-reader behaviour the browser already implements. **`accept` filters the picker and is not a check** — the server sniffs magic bytes and never consults the extension |
| `Table` / `Th` / `Td` | plain semantic tables with an `sr-only` caption | **No data-grid library**: the ordering that matters is the server's. Sorting a ranked list by filename is how the top of Band A stops being the top of the screen |
| `Disclosure` | a styled native `<details>` | Not a JS accordion — the browser already implements the state, the keyboard behaviour and the semantics, and content inside stays findable by the browser's own in-page search **while collapsed**, which matters on a compliance screen |
| `Popover` | Radix | Focus trapping, Escape, outside-click, `aria-expanded`, collision-aware positioning — every one a known-difficult detail |
| `Markdown` | `react-markdown`, inline, no GFM, no raw HTML | Builds React elements rather than an HTML string, so an audit detail containing `<script>` renders as the text it is |

---

## Part 13 — What happens when things fail

Four layers, innermost first.

```
 ┌─ 4 ─ ErrorBoundary ─────────────────────────────────────────────────────┐
 │  a component threw while rendering                                      │
 │  "This screen could not be displayed. Nothing was submitted, and no      │
 │   screening result was changed."          [ Try again ]                 │
 │                                                                         │
 │  ┌─ 3 ─ mutation onError ──────────────────────────────────────────┐    │
 │  │  toast.error(error.message)                    bottom-right     │    │
 │  │  transient, beside the click that caused it                     │    │
 │  │                                                                 │    │
 │  │  ┌─ 2 ─ <QueryState> ────────────────────────────────────────┐  │    │
 │  │  │  isPending → spinner + "Loading results…"                 │  │    │
 │  │  │  isError   → Alert "That did not work" + [ Try again ]    │  │    │
 │  │  │  success   → children(data)   ← data is NON-NULLABLE      │  │    │
 │  │  │                                                           │  │    │
 │  │  │  ┌─ 1 ─ api/client.ts ──────────────────────────────┐     │  │    │
 │  │  │  │  fetch threw     → "Cannot reach the screener    │     │  │    │
 │  │  │  │                     API. Is the API running?"    │     │  │    │
 │  │  │  │  timeout fired   → "…did not respond within 30s. │     │  │    │
 │  │  │  │                     It may be busy loading the   │     │  │    │
 │  │  │  │                     model."                      │     │  │    │
 │  │  │  │  !response.ok    → explain(status) → ApiError    │     │  │    │
 │  │  │  └──────────────────────────────────────────────────┘     │  │    │
 │  │  └───────────────────────────────────────────────────────────┘  │    │
 │  └─────────────────────────────────────────────────────────────────┘    │
 └─────────────────────────────────────────────────────────────────────────┘

        at every one of the four levels: a sentence, never a stack trace
```

### 1. The request fails → `ApiError` with a sentence

`client.ts` turns every status and every network condition into something a
recruiter can act on. See §6.2.

### 2. A query fails → `<QueryState>`

Every read goes through it, so every screen has the same three states:

```tsx
if (query.isPending) return <spinner + "Loading…">;
if (query.isError)   return <Alert tone="error" title="That did not work">
                              {query.error.message}
                              <Button onClick={refetch}>Try again</Button>
                            </Alert>;
return children(query.data);          // ← non-nullable
```

> Written inline they drift: one page renders a spinner, another renders nothing,
> a third renders a half-built table from `undefined`. The render-prop form also
> makes the success case take non-nullable data, so TypeScript refuses the "it
> will be there by then" assumption that produces a blank panel in front of a
> reviewer.

### 3. A mutation fails → a toast

Every `mutate` call passes `onError: (error) => toast.error(error.message)`.
Sonner renders it bottom-right.

> Outcomes of an action the reviewer just took — "saved version 3", "12 files
> queued" — are transient and belong beside the click, not as a banner that
> pushes the page down. Anything that has to be read before continuing stays an
> inline `Alert`.

### 4. A render throws → `ErrorBoundary`

Still a class component, because error boundaries are the one part of React with
no hook equivalent. It logs to the console and renders:

> **This screen could not be displayed** — *"The reviewer interface hit a
> problem rendering. Nothing was submitted, and no screening result was
> changed."* + the message + a **Try again** button that clears the error state.

> A stack trace mid-page is useless to a recruiter and indistinguishable from a
> bug in the screening itself. A render failure here means *this app* has a bug,
> so it says that.

**Failures are sentences, never stack traces** — at every one of the four levels.

---

## Part 14 — Polling and caching, summarised

| What | Interval | Stops when |
|---|---|---|
| `/health` | 30 s | never (it is a banner) |
| `/dashboard` | 5 s | `runs_in_progress === 0` |
| `/runs/{id}/status` | 5 s | status is not `pending` or `running` |
| everything else | on mount, on window focus, after `staleTime` (10 s) | — |

```
   t=0    5s    10s   15s   20s   25s   30s   35s   40s
    │     │     │     │     │     │     │     │     │
    │     │     │     │     │     │     │     │     │
 health ──────────────────────────────────●───────────────  every 30s, always
                                                            (it is a banner)

 dashboard ●─────●─────●─────●─────●─────●─────●─────●────  every 5s, but ONLY
                                                            while runs_in_progress>0

 runStatus ●─────●─────●─────●─────✗                        every 5s, but ONLY
                                   └─ status left           while pending|running
                                      pending/running
                                      → polling STOPS

 everything ●                                               on mount · on window
 else                                                       focus · after 10s stale

  ⇒ a finished run left open on a second monitor costs nothing:
    both live polls are conditional on SERVER state, not on the screen being open.
```

Two properties fall out of this:

- **A finished run left open on a second monitor costs nothing.** Both live polls
  are conditional on server state, not on the screen being mounted.
- **Changing identity re-fetches everything**, because every key contains it.
  That is required, not incidental: the auditor sees more fields.

The read-side cost of the 5 s poll is deliberately bounded on the server too:
`service.run_status` runs in a **read-only transaction** (`SET TRANSACTION READ
ONLY`), so a status poll cannot contend with the worker's writes.

---

## Part 15 — Tests

Run with `npm run test` (vitest + jsdom + Testing Library), and as part of
`npm run check` / `./scripts/dev.sh check`.

| File | Covers |
|---|---|
| `api/client.test.ts` | headers, status→sentence mapping, timeouts, 204 handling, **a 200 that is not JSON** |
| `lib/highlight.test.ts` | slicing, truncation markers, out-of-range offsets |
| `lib/labels.test.ts` | that every wire value maps to a label, and the fallbacks |
| `pages/DashboardPage.test.tsx` | the tiles and the queue table |
| `pages/ReviewPage.test.tsx` | the three groups, selection, sign-off gating |
| `components/CriterionBlock.test.tsx` | the three evidence render paths |
| `components/ClosePositionButton.test.tsx` | the confirmation flow |
| `components/JdInput.test.tsx` | which controls each `jd_intake_mode` renders, the extracted text staying editable, provenance carried, `FormData` sent with no hand-written `Content-Type`, the OCR and injection advisories, and errors **not** blaming the document |
| `components/ParsedResumeText.test.tsx` | collapsed rendering, whitespace preserved |
| `components/ThemeToggle.test.tsx` | the cycle and the media-query subscription |

`test/utils.tsx` supplies `renderWithProviders`, wrapping in
`QueryClientProvider` + `SessionProvider` + `MemoryRouter`:

> Retries off and no caching between tests: a retried failure turns a one-line
> assertion into a five-second timeout, and a cache shared across tests makes the
> order they run in significant.

`test/setup.ts` stubs `ResizeObserver` and the pointer-capture methods, because
jsdom implements no layout and Radix measures its trigger to position a panel.
Without them the failure names `ResizeObserver` rather than anything to do with
the component. **They are stubs, not polyfills** — nothing asserts on them.

### What these tests cannot see

There is no MSW and no test server: **every test here replaces `fetch` with a
function returning a literal somebody wrote.** On the other side of the boundary,
the Python suite reaches the API through `TestClient`, which never opens a
socket. So both suites are complete on their own side and **neither one exercises
the network path the browser actually uses** — `browser → Vite proxy → uvicorn`.

That gap has shipped a bug. `/jd-documents` and `/config` were added to
`client.ts` and not to `API_PATHS` in `vite.config.ts`; the dev server answered
them with `index.html` and a **200**, the app got a page where it expected JSON,
and nothing reached the API at all — so its access log stayed empty while the
interface reported a broken endpoint. Every test on both sides passed.

Two things came out of it, and they are the mitigation rather than a fix:

- `tests/test_layering.py::test_every_path_the_client_calls_is_reachable_in_development`
  parses the paths out of `client.ts` and `API_PATHS` out of `vite.config.ts` and
  fails if the first is not a subset of the second.
- `client.ts` turns a non-JSON 200 into a named `ApiError` pointing at
  `vite.config.ts`, instead of letting a bare `SyntaxError` escape to a
  component carrying `Unexpected token '<'` as its whole explanation.

**Neither covers a running server serving stale code**, which is not a property
of the source and cannot be. `RELOAD=1 scripts/dev.sh start api`, or
`scripts/dev.sh restart api` after a backend change. And a claim that something
works end to end has to be checked through the port the browser uses, not the
one that is convenient.

**And four rules are enforced from Python**, in `tests/test_layering.py` — see
§5. Those run in the normal pytest suite, so they fail CI even on a machine that
never ran `npm`.

`tests/test_ui.py` covers the **seam**: that the API serves the bundle, that a
deep-linked client-side route returns `index.html` rather than a 404, and that it
never shadows an API path sharing its prefix. Both are invisible until someone
opens a browser.

---

## Part 16 — Adding an endpoint

Five steps, in order:

1. **`src/api/types.ts`** — add the response shape, mirroring the pydantic model,
   and name that model in a comment.
2. **`src/api/client.ts`** — add the call inside `createApi`. Relative path only.
3. **`vite.config.ts`** — add the path to `API_PATHS`. **This is the step that
   gets forgotten**, and it fails silently rather than loudly: the dev server
   answers an unlisted path with `index.html` and a 200, so the app gets a page
   where it expected JSON, the API's access log stays empty, and it reads as a
   broken endpoint. It was missed when `/jd-documents` was added, and the
   symptom — an upload that failed with nothing on the server side to show for
   it — cost an afternoon.
4. **`src/api/queries.ts`** — add a query key to `keys` and a hook. For a write,
   **name the keys it invalidates** in `onSuccess`.
5. **Use the hook in a page**, wrapping the read in `<QueryState>` so loading and
   failure look the same as everywhere else.

If the endpoint is role-gated, also add the client-side hint (as `AuditLayout`
does) so the reviewer is told what to do rather than watching a panel fail.

---

## Part 17 — Known gaps

Read this before reporting any of it as a bug.

- **Authentication is stubbed.** `X-Actor` and `X-Actor-Roles` are typed into a
  popover and trusted by the API. This is only acceptable while the API listens
  on loopback. Both the dev server and the API bind to `127.0.0.1` for that
  reason.
- **There is no authorization model, which is a separate gap.** Roles gate what a
  response *contains*; **no write endpoint checks a role**. One person can draft
  a rubric, approve it, decide every candidate and sign off the run. The audit
  trail records that faithfully — `RunStoryPage` even says so outright — it just
  does not prevent it.
- **`ESCALATION_BUDGET = 0.03` in `labels.ts` is a display value.** The server's
  `escalation_budget` setting is deliberately unset, because a guessed budget is
  worse than none: it reads like a measurement in every report that quotes it.
  These two numbers are not connected.
- **`types.ts` is hand-mirrored from `schemas.py`.** Deliberate, for a surface
  this size — but it means a server-side field rename is a change someone has to
  make in both places. `tsc` catches a *removed* field being read; it cannot
  catch a field the server stopped sending.
- **No virtualisation on the candidate tables.** A run of 1,000 renders 1,000
  rows. It has not been a problem, and the groups are collapsed by default, but
  it is the obvious next thing if a run gets much larger.

---

## The one-page summary

The reviewer interface is a browser tab that calls one API through one module,
caches every server value in TanStack Query under a key that includes who is
asking, keeps its own state in the URL so no two screens can disagree, validates
forms only to predict a refusal the server will make anyway, and renders every
failure as a sentence. It shows a band rather than a score, puts the candidates
it could not score first and open, quotes evidence inside its surrounding
paragraph, and presents the second model as a disagreement rather than an answer
— because each of those is a way that human oversight otherwise stops working
while still appearing to work.
