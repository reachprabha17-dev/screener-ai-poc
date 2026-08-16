# Reviewer interface

React 19 + TypeScript, built by Vite into static files that the API process
serves at `/ui/`. It replaces the Streamlit UI that lived in `ui/`.

React publishes no LTS line — 19 is the current stable major, and that is what
this pins. Node 22 LTS is the runtime the build is developed and tested against.

```bash
npm install                 # once
npm run dev                 # http://127.0.0.1:5173/ui/ , proxying the API
npm run check               # eslint + tsc + vitest, the gates CI runs
npm run build               # → web/dist, which the API serves at /ui/
```

`scripts/dev.sh start` runs this alongside the API and the worker;
`scripts/dev.sh build` produces the bundle a deployment serves.

---

## Why this exists

Streamlit re-executes the entire script on every interaction, and everything
below followed from working around that.

The concrete failure: three screens each carried a "which run?" dropdown, all
writing one shared `session_state` key. Each re-run compared its widget against
the shared value and corrected it, which triggered another re-run — **1,207
script passes in 150 seconds, 119% CPU, an API call on every pass**, with the
browser stuck on _Running…_. It was fixed with `on_change` callbacks and then
again by splitting into pages so only the active one executes, but the shape that
allowed it survived both fixes: any two widgets sharing state across screens
could do it again.

Here the run is `/runs/:runId`. There is no shared selection to fight over, the
back button works, and a reviewer can send a colleague the exact screen they are
looking at. That is the whole argument for the migration; the rest is
consequence.

## What did not change

The decisions the Streamlit implementation encoded are load-bearing and were
carried over verbatim, most of them with the paragraph explaining why:

- **Reviewers see a band, not a score.** Three verdict levels across at most
  twelve criteria cannot support a rendered `7.8`. The float is in the CSV export
  and in the detail view, next to its provenance.
- **`needs_review` is its own group, first and open.** At 1,000 applicants a
  reviewer reads the top of Band A and stops.
- **The escalation rate is on screen during the run**, against its budget.
  Oversight collapses into rubber-stamping the moment the queue exceeds what a
  person will read, and that failure is silent.
- **Evidence is quoted inside its surrounding paragraph**, with the match
  highlighted — a bare quote can be cherry-picked from a sentence that said the
  opposite.
- **The second model disagrees; it is never the answer.** Presented as an answer,
  reviewers defer to it and automated decision-making returns through the
  interface.
- **Failures are sentences, never stack traces.** `api/client.ts` turns every
  HTTP status into something a recruiter can act on.

## Shape of the code

| Path                 | What is in it                                                                                       |
| -------------------- | --------------------------------------------------------------------------------------------------- |
| `src/api/client.ts`  | The only module that performs network I/O. Attaches identity headers; turns failures into sentences |
| `src/api/queries.ts` | Every read and write as a TanStack Query hook. Query keys, polling, invalidation                    |
| `src/api/types.ts`   | The wire contract, hand-mirrored from `screener/schemas.py`                                         |
| `src/lib/labels.ts`  | The words on screen — flag help, escalation reasons, audit sentences. Each is a decision            |
| `src/ui/`            | Button, Alert, Card, Table, Popover… thin `cva` wrappers over Tailwind                              |
| `src/components/`    | The parts with domain meaning: rubric editor, candidate detail, escalation meter, folder picker     |
| `src/pages/`         | One file per screen                                                                                 |

## Rules this app is held to

Enforced from Python, in `tests/test_layering.py`, because they are the
browser-side half of decision #11:

- `fetch` appears in exactly one module. Every request therefore carries the
  actor headers the audit log records, and every failure becomes an `ApiError`
  with a sentence in it.
- Nothing hands a string to the DOM as markup. Resume text, decision reasons and
  audit details are all attacker-influenced.
- No absolute URLs. The app talks only to the host that served it, which is why
  the API needs no CORS and there is no "API URL" setting for an operator to
  point somewhere else.

## Adding an endpoint

1. Add the response shape to `src/api/types.ts`, mirroring the pydantic model.
2. Add the call to `src/api/client.ts`.
3. Add a hook and a query key to `src/api/queries.ts` — and, for a write, name
   the keys it invalidates.
4. Use the hook in a page. Wrap the read in `<QueryState>` so loading and failure
   look the same as everywhere else.
