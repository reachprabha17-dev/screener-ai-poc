# Screener spec v7 — the reviewer interface

**Status:** amendment to `screener_spec_v6.md`. v6 remains the specification for
everything not listed here: intake, sandboxing, prompts, scoring, verification,
storage, the worker, and the API's behaviour are unchanged and untouched by this
document.

**Scope of v7:** the reviewer interface moves from Streamlit to React, and the
three-process deployment becomes two processes plus a static bundle. Nothing
about *what* a reviewer is shown changes — §15's contract is carried over
clause by clause. What changes is the thing rendering it, and two locked
decisions that named Streamlit by name.

---

## 1. Amended locked decisions

| # | v6 | v7 | Why |
|---|---|---|---|
| 13 | **Three processes**: FastAPI, `worker.py`, Streamlit. Never collapsed | **Two processes**: FastAPI, `worker.py`. The reviewer interface is a static bundle the API serves. Never collapsed | The third process existed to run Python for the UI. There is no Python in the UI. The boundary the decision protected — screening never sharing a process with serving — is untouched |
| 16 | Streamlit has **no database driver installed** | The reviewer interface **cannot reach the database at all**, and every request it makes passes through one client module | v6's own §16 note conceded the weakness: `sqlite3` ships with Python and cannot be uninstalled, so the rule was enforced by `tests/test_layering.py` rather than by packaging. A browser tab has no database driver to install |
| 31 *(new)* | — | **The interface is served same-origin by the API at `/ui/`.** No CORS, no configurable API host | An operator who can point the interface at another host can point candidate data at another host. Removing the setting is stronger than documenting it |
| 32 *(new)* | — | **No string reaches the DOM as markup.** No raw-HTML injection anywhere in the interface | Resume text, decision reasons and audit details are all attacker-influenced (§8, §10.2). Streamlit's evidence highlighting needed `unsafe_allow_html`; React elements need nothing |

Decisions 1–12, 14, 15, 17–30 stand unchanged.

---

## 2. Amended architecture

```
                    ┌──────────────────────────────────────────┐
  Browser ──HTTP──► │  FastAPI  (control plane, sync handlers) │
  (React bundle,    │     authz (stubbed) · audit actor        │
   served from      │     transactions · enqueue only          │
   /ui/ by this     │     GET /ui/*  → web/dist (static)       │
   same process)    └───────────────────┬──────────────────────┘
   CLI (typer) ─────────────────────────┤
                                        ▼
                              ┌──────────────────┐
                              │ SQLite → MS SQL  │
                              └────────┬─────────┘
                    ┌──────────────────┴──────────────────────┐
                    │  worker.py (daemon)                     │
                    │   phase 1: judge   (granite4.1:8b)      │
                    │   phase 2: verify  (gemma4:12b)         │
                    └─────────────────────────────────────────┘
```

| Caller | Path |
|---|---|
| Browser | HTTP → FastAPI → `service.py` → stores |
| CLI | `service.py` → stores (break-glass when the API is down) |
| Worker | `service.py` for reads/writes, `pipeline.py` for execution |

**Serving the bundle is not a layering violation.** What the API gained is a
directory of files to hand out. It gained no UI code, no template engine and no
session state; the interface reaches data through exactly the endpoints the CLI
and any future client use.

**`/ui/`, not `/`.** The interface does client-side routing, so a deep link must
return `index.html` rather than 404 — and `/audit` and `/runs` are both API paths
*and* screens. A catch-all at the root would make which one answers depend on
route registration order, which works until somebody adds an endpoint whose path
a reviewer had bookmarked. Enforced by `tests/test_ui.py`.

---

## 3. Amended tooling

```toml
[project.optional-dependencies]
# The `ui` extra is gone. The interface has no Python dependencies because it is
# not a Python process.
mssql = ["pyodbc"]
dev   = ["pytest", "pytest-cov", "hypothesis", "ruff", "mypy", "httpx"]
```

New settings key: `web_dist_dir` (default `web/dist`). Absent or unbuilt, the API
serves its endpoints and nothing at `/ui/` — which is the normal state of a
source checkout, because the Vite dev server holds the bundle during development
and proxies the API.

The interface's own toolchain, pinned in `web/package.json`:

| Choice | Version | Why this one |
|---|---|---|
| React | 19.x | React publishes no LTS line; 19 is the current stable major |
| Node | 22 LTS | The runtime the build is developed and tested against |
| TypeScript | 5.9, `strict` + `noUncheckedIndexedAccess` + `exactOptionalPropertyTypes` | The API is another process that can be redeployed underneath this one. Every response is unknown-shaped until parsed |
| Vite | 8 | Build and dev server. The dev server proxies the API paths so the browser sees one origin in development too |
| TanStack Query | 5 | Server state, caching, polling, invalidation |
| React Router | 7 | One screen per URL |
| Tailwind CSS | 4 | Styling without a bespoke design system to maintain |
| Radix, lucide, sonner, react-hook-form | current | Popover/focus behaviour, icons, toasts, form state — the accessible details that are wrong when hand-rolled |
| Vitest + Testing Library | current | The interface's tests |

**No component framework was vendored.** `web/src/ui/` holds thin `cva` wrappers
over Tailwind utilities — the shape shadcn/ui generates, without a generator or
files to keep in sync.

---

## 4. Amended folder structure

```
web/
  package.json  vite.config.ts  tsconfig.*.json  eslint.config.js  .prettierrc.json
  index.html
  src/
    main.tsx  App.tsx  index.css
    api/
      client.ts        the only module that performs network I/O
      queries.ts       every read and write as a hook; keys, polling, invalidation
      types.ts         the wire contract, mirrored from screener/schemas.py
      client.test.ts   the HTTP contract and the error sentences
    session/           who the reviewer says they are (stubbed auth, §17 of v6)
    lib/               labels, formatting, evidence highlighting — pure, tested
    ui/                Button, Alert, Card, Table, Disclosure, Popover, Markdown
    components/        the parts with domain meaning
    pages/             one file per screen
    test/              setup and provider harness
```

`ui/` (the Streamlit package) is deleted, along with `.streamlit/config.toml` and
`deploy/screener-ui.service`.

---

## 5. §15 carried over — the parts that are the specification

Every clause of v6 §15 stands. Restated here only where the implementation is now
somewhere else:

- **§15.1 response models** — unchanged on the server. Mirrored in
  `web/src/api/types.ts`, hand-written rather than generated: the response models
  are the API's public boundary, and a change to one should be a decision made on
  both sides rather than a diff nobody reads.
- **§15.2 field exposure by role** — unchanged, and now visible in the cache
  contract: **every query key carries the actor and their roles**, because the
  same URL returns a recruiter view or an auditor view depending on who asked. A
  key without the actor would serve the second person the first person's view.
- **§15.3 highlight rendering** — unchanged. Offsets are still translated
  server-side out of `sent_text`; the client slices `resume_text` and marks the
  span. `lib/highlight.ts`, unit-tested, including the case where offsets do not
  fit the text and the plain quote is shown instead.
- **§15.4 list screen** — unchanged: three groups in fixed order, needs-review
  first and expanded, escalations grouped by reason, run header with phase, ETA,
  escalation rate against budget, and undecided count. `verification_status =
  'pending'` still renders as provisional.
- **§15.5 detail screen** — unchanged, including the presentation rule: the
  verifier reads as *"a second model disagrees — you decide"*, never as a
  correction.
- **§15.6 export** — unchanged. The CSV carries the float score for audit while
  the screen shows bands; it is generated in the browser from rows already
  fetched rather than added as a server endpoint, so no new egress path exists.

### 5.1 New: the URL is the state

| Route | Screen |
|---|---|
| `/ui/requisitions` | Open requisitions |
| `/ui/requisitions/new` | Folder picker, title, job description |
| `/ui/requisitions/:positionId` | Rubric: draft, edit, approve; runs for this requisition |
| `/ui/runs` | Every run, newest first |
| `/ui/runs/:runId` | Live progress, controls, failed files |
| `/ui/runs/:runId/review?c=<sha256>` | The three groups, the candidate, the decision |
| `/ui/audit`, `/ui/audit/record`, `/ui/audit/search` | Run story, one candidate's record, the searchable log |

This replaces the shared sidebar state of the Streamlit implementation, and with
it a specific defect: three screens each carried a "which run?" selector writing
one shared key, and the correction each made on re-run triggered another re-run —
**1,207 script passes in 150 seconds, 119% CPU, an API call on every pass**.
There is now nothing to share. A reviewer can also link a colleague to the exact
screen they are looking at, which the previous interface could not do at all.

### 5.2 New: client-side rules

1. **No component fetches.** Server state belongs to the query cache; components
   hold view state only.
2. **Writes invalidate, they do not patch.** The server refuses decisions on
   escalated candidates, refuses sign-off with an unread queue, and versions a
   rubric on save. A cache written from what the client hoped happened would show
   a reviewer an outcome the system did not record.
3. **Progress is polled, never pushed** (v6 §22.2 unchanged): 5 s while a run is
   live, stopped when it is not.
4. **Failures are sentences.** Every HTTP status becomes something a recruiter
   can act on, in one place. A stack trace on screen is useless to a recruiter and
   indistinguishable from a bug in the screening itself.
5. **The rubric round-trips fields it does not edit.** `claim` and `claim_stale`
   are carried through the editor untouched: the server's model *defaults*
   missing fields rather than rejecting them, so a save that dropped `claim`
   would silently blank the hypothesis phase 2 verifies against, and nothing
   about the result would look wrong.

---

## 6. Amended §16 — the API surface

One addition, no changes to any existing endpoint:

```
GET /ui                    → index.html
GET /ui/{path:path}        → the built asset if it names one, index.html otherwise
```

Registered after the routers, resolved under the `no-store` middleware, and
skipped entirely when `web_dist_dir` holds no `index.html`. The path is resolved
and checked for containment before anything is opened.

`MUTATING_ROUTES` in `tests/test_api.py` is unchanged: the mount adds no mutating
route, which is a property worth stating because the tripwire would otherwise be
the thing that noticed.

---

## 7. Amended deployment

`deploy/screener-ui.service` is deleted. `deploy/screener-api.service` gains
`Environment=WEB_DIST_DIR=/opt/screener/web/dist` and serves the interface at
`http://127.0.0.1:8000/ui/`, still loopback-only for the reason v6 gives: auth is
stubbed, so anything that can reach the port can read every candidate.

Deployment adds one build step, which is the honest cost of this change:

```bash
npm --prefix web ci
npm --prefix web run build     # → web/dist
```

An air-gapped host needs `node_modules` or a prebuilt `web/dist` shipped with the
release. Nothing is fetched at runtime: the bundle inlines every dependency, and
there is no CDN, font host or telemetry endpoint to reach. This replaces
Streamlit's `gatherUsageStats`, which had to be switched off in two places
because it phoned home by default.

---

## 8. Gates

Added to `scripts/dev.sh check`, alongside the four v6 gates:

| Gate | Command |
|---|---|
| Formatting | `prettier --check .` |
| Lint | `eslint .` (type-checked rules, react-hooks) |
| Types | `tsc --build` |
| Tests | `vitest run` |

Skipped with a warning when `web/node_modules` is absent: a backend change should
not be blocked by a machine with no Node on it.

`tests/test_layering.py` replaces its three Streamlit rules with four read from
the TypeScript source. The method is weaker than the import-graph analysis used
elsewhere in that module and the module says so — the raw-HTML rule caught its
own explanatory comment on the first run, which is step 15's `BackgroundTasks`
lesson repeating itself.

1. `fetch` appears in exactly one module. Every request therefore carries the
   actor headers the audit log records, and every failure becomes a sentence.
2. Nothing hands a string to the DOM as markup.
3. No absolute URLs anywhere in the interface.
4. No Python under `web/`, and no `streamlit` import anywhere in the repository.

`tests/test_ui.py` no longer tests a Python HTTP client — that contract moved to
`web/src/api/client.test.ts`. It tests the seam this process owns: the bundle is
served, a deep-linked client-side route returns `index.html`, the mount never
shadows an API path, the bundle is not cached, and an API with no built interface
still serves its endpoints.

---

## 9. What was deliberately not carried over

- **The "API URL" box.** Same-origin removes the setting. See decision 31.
- **The sidebar.** Identity and roles are in the header; the working context is
  in the URL.
- **A top-level "Review" destination.** Review belongs to a run and is reached
  from one. A top-level Review that first asks *which run* is a question the
  navigation should already have answered.
- **`pandas`.** It was a dependency for rendering tables.
- **An audit-log CSV export.** v6's plan deferred it as a data-egress decision to
  take deliberately rather than as a UI convenience. Still deferred.

---

## 10. Open questions

- **Candidate list at 1,000.** Every group renders every row. This is untested
  above a few hundred; virtualisation is the obvious answer if it is needed, and
  guessing at it now would be premature. Measure before adding it.
- **A name filter on the review list.** At 1,000 applicants, finding one person
  by name is currently a browser find. Deliberately not added in this pass:
  filtering interacts with the fixed group ordering that §15.4 exists to
  guarantee, and it deserves its own decision.
- **Bundle size.** 366 kB (114 kB gzipped) for the main chunk, with the audit
  screens split out. Acceptable on a LAN; worth revisiting only if it is not.
- **Two languages, two type systems.** `web/src/api/types.ts` mirrors
  `screener/schemas.py` by hand. Generating it from the OpenAPI schema is
  possible and was rejected for a surface this size — but it is the first thing
  to reconsider if the two ever drift in a way a test does not catch.
