# Guide to this codebase

This is a guide for someone new to the project. It explains what the app does,
what technologies it uses, what you should learn first, and in what order to read
the code.

Source code: about 13,600 lines. Tests: about 12,700 lines.

How long it takes:

* 20 minutes to understand what the app does
* Half a day to read the code, layer by layer
* 2 to 3 days to learn the technologies, if they are new to you

The full design document is `screener_spec_v6.md`. It explains *why* every
decision was made. This guide is a shortcut into it, not a replacement.
(`screener_spec_v4.md` is the old version, kept for history. If the two ever
disagree, v6 is correct.)

`screener_spec_v7.md` amends v6 for one thing only: the reviewer interface, which
is now React rather than Streamlit. Where they overlap — the process model, the
tooling list, the folder structure, and what §15 says a reviewer must be shown —
v7 is correct. Everything else in v6 stands untouched.

---

## Part 1 — What this app does

It screens job applicants using a local AI model.

You give it a job description. It turns that into a list of criteria (a
"rubric"). A human approves the rubric. Then it reads a folder full of CVs,
scores each one against the rubric, and gives you a ranked list.

Two rules shape the entire design. Everything else follows from them.

**Rule 1: every decision that can affect a person is a plain function with no
side effects.** All of that logic lives in `screener/core/`. Those functions
don't touch the database, the network, or the AI model. They take data in and
return data out. This means every rule that could cost someone a job interview
can be tested on a laptop in milliseconds.

**Rule 2: the AI checks its own work, but is never allowed to change a
decision.** The app runs the CVs past a *second* AI model that reviews the
first model's answers. When the two disagree, the app flags the candidate for a
human to look at. It never lets the second model silently overturn the first.
Both models read the same untrusted text (a CV can contain anything, including
text designed to trick an AI), so letting them argue their way to a final answer
with no human involved would be unsafe. This rule is enforced in
`screener/core/reconcile_judge.py`.

### Before reading any code (20 minutes)

Read these three sections of the spec, in this order:

| Section | What you get from it |
|---|---|
| §24 (`screener_spec_v6.md:1917`) | One diagram of the whole system, job description to final CSV |
| §13 (`screener_spec_v6.md:1286`) | The two main functions written as plain pseudocode |
| §1 (`screener_spec_v6.md:16`) | The decisions that are locked and won't change |
| §1 (`screener_spec_v7.md:16`) | The two of those decisions the React interface changed, and why |

Then stop reading the spec. It's a reference book, not a tutorial. Go back to it
by section number when the code raises a question.

---

## Part 2 — The technologies

Everything runs on **one computer, inside the company network, with no internet
access**. That one constraint chose most of the stack for us:

* No internet means the AI model runs locally (Ollama), not through a cloud API.
* One machine means one Postgres instance on that machine, not a cluster. It
  started as SQLite — one file, no server, which suits a proof of concept — and
  that was reversed. Read "Why Postgres, and why not SQLite" below before
  assuming either choice was arbitrary.
* A small internal audience originally meant a Python-based UI (Streamlit). That
  was reversed: the interface is now React, built once into static files that the
  API process serves. The build system is the cost; what it bought is a real
  event model instead of "re-run the whole script on every click", one screen per
  URL, and a UI that cannot open the database because it isn't a Python process
  at all.
* One machine again means plain Linux services (systemd), not Docker or
  Kubernetes.

### The full list

| Technology | What it is | Where it's used here |
|---|---|---|
| **Python 3.12** | The language everything is written in | Everywhere |
| **Pydantic v2** | Defines data shapes and validates them automatically | `models.py`, `schemas.py`, `ports.py` |
| **pydantic-settings** | Reads configuration from environment variables into a typed object | `config/settings.py` |
| **FastAPI** | Web framework that serves the HTTP API | `screener/api/` |
| **uvicorn** | The web server that runs FastAPI | `scripts/dev.sh`, `deploy/` |
| **PostgreSQL 18** | The database. A server, not a file | `screener/storage/` |
| **SQLAlchemy Core** | Engine and connection pool. Not the ORM — the SQL is still written by hand | `storage/connection.py`, `storage/uow.py` |
| **psycopg 3** | The Postgres driver, shared by SQLAlchemy and yoyo | declared in `pyproject.toml` |
| **yoyo-migrations** | Applies database schema changes in order | `storage/migrations/postgres/` |
| **Ollama** | Runs AI models locally on this machine | `clients/ollama_client.py` |
| **React 19 + TypeScript** | The reviewer interface | `web/src/` |
| **Vite** | Builds the interface into static files | `web/vite.config.ts` |
| **TanStack Query** | Owns everything the server knows: caching, polling, refetching | `web/src/api/queries.ts` |
| **React Router** | One screen per URL, so a run can be linked to | `web/src/App.tsx` |
| **Tailwind CSS** | Styling as utility classes; no bespoke design system | `web/src/index.css` |
| **Radix, lucide, sonner** | Popovers, icons, toasts — the accessible details nobody should hand-roll | `web/src/ui/` |
| **react-hook-form** | Form state and validation | `web/src/components/DecisionForm.tsx` |
| **Vitest + Testing Library** | The interface's tests | `web/src/**/*.test.ts*` |
| **httpx** | HTTP client library | `tests/` |
| **Typer** | Builds command-line tools | `screener/cli.py` |
| **structlog** | Writes logs as JSON instead of sentences | `screener/logging.py` |
| **xberg** | Extracts text from PDFs and Word files, with OCR for scans | `intake/parse_worker.py` |
| **python-magic** | Detects a file's real type by looking at its bytes | `intake/validate_file.py` |
| **defusedxml** | XML parser that blocks known XML attacks | `intake/` |
| **pytest** | The testing framework | `tests/` |
| **hypothesis** | Generates random test inputs to find edge cases | `tests/` |
| **mypy** | Checks type annotations are correct before you run the code | Build check |
| **ruff** | Formats code and catches common mistakes | Build check |
| **uv** | Installs dependencies and manages the virtual environment | `uv.lock` |
| **systemd** | Linux's service manager; keeps the app running | `deploy/*.service` |

### What to learn, in order

You do not need all of this before you start. Here is what to learn and when.

#### Learn these first — you can't read the code without them

**1. Modern Python typing (about half a day)**

Learn these specific things:

* `Protocol` — lets you say "anything with these methods will do" without
  inheritance. This is how `ports.py` defines the swappable parts of the system.
* `StrEnum` — an enum whose members are also strings.
* `Literal["a", "b"]` — a type meaning "only these exact values are allowed".
* `X | None` — the modern way to write "optional".

The whole codebase passes strict type checking, so the type annotations are
reliable. Reading them often saves you from reading the function body at all.

**2. Pydantic v2 (about half a day) — the single most useful thing to learn here**

Pydantic is how every piece of data in this app is defined. A Pydantic model is
a class where you declare fields with types, and Pydantic checks the data
matches when you create one.

There are two sets of models, and the difference matters:

* `screener/models.py` (743 lines) — the *internal* shapes. A `Candidate`, a
  `Rubric`, a `Run`.
* `screener/schemas.py` (616 lines) — the *external* shapes. What the API sends
  and receives over HTTP.

They are separate on purpose. It means you can change something internal without
accidentally changing what the API promises to its callers.

Learn: `BaseModel`, `Field`, `ConfigDict`, `field_validator`, `model_dump`, and
how to subclass a model to make a variant.

Skip: generics, custom types, `RootModel`. None of them appear here.

One warning if you learn from older tutorials: Pydantic v1 and v2 are quite
different. In v2, `@validator` became `@field_validator`, and the inner `Config`
class became `model_config`.

**3. pytest (about 2 hours)**

Learn fixtures, `parametrize`, markers, and `monkeypatch`. You will read far
more test code than you write at first, because in this project the tests are
where the rules are actually written down.

#### Learn these before touching the matching part of the code

**4. FastAPI (about half a day) — before working in `screener/api/`**

FastAPI turns Python functions into HTTP endpoints. Learn `APIRouter`,
`Depends` (its dependency injection), `response_model`, and `HTTPException`.

Two things specific to this codebase are worth understanding rather than
memorising:

* One endpoint sets `response_model=None` on purpose. Auditors get a *bigger*
  version of the candidate object than recruiters do. If we declared the normal
  response type, FastAPI would helpfully strip the extra fields back off and the
  auditor would silently see less than they should.
* The API is created by a *function* (`create_app`), not built at import time.
  That's what `--factory` means in the startup command. It lets the app run its
  database checks at startup and refuse to start if the schema is out of date.

Skip: async endpoints (there are none here), WebSockets, background tasks.

**5. How Postgres handles concurrent access (about half a day) — before working
in `screener/storage/`**

This is the least "framework-like" item on the list and the one people most
often get wrong. Two separate programs write to this database: the web API and
the background worker.

Learn:

* **Row-level locking, and `FOR UPDATE SKIP LOCKED`.** This is the single most
  important item here. Postgres locks *rows*, not the database, so two writers
  proceed in parallel unless they want the same row — which is what makes a
  second worker possible at all, and also what makes claiming a job harder than
  it looks. Read `jobs_store.claim_next` and its module docstring: the
  history there is worth your time, because the obvious-looking version of that
  statement handed the same job to two workers.
* **`SELECT ... FOR UPDATE` for read-then-write.** Any sequence that reads state,
  decides something, and writes it back needs the row held across both halves.
  `runs_store.get_for_update` exists for exactly this, and the docstring says
  which three callers need it and why.
* **Read-committed isolation**, which is the default and is what makes the two
  points above necessary. Two transactions can read the same row and both act on
  what they read.
* **The connection pool.** Handlers are synchronous and FastAPI runs them on a
  threadpool, so connections move between threads. `pool_pre_ping` and
  `pool_recycle` are there because a server-side idle timeout can close a
  connection underneath you — a class of problem that does not exist with a
  local file.
* **Why a database transaction must never wrap an AI call.** An AI call takes
  about 5 seconds. Holding locks for 5 seconds blocks the other process. The
  worker's pattern is: claim job (transaction), run the AI (no transaction),
  save result (transaction).

Then read `storage/connection.py` and `storage/uow.py`. That's 300 lines
containing all of the above.

**Why Postgres, and why not SQLite.** SQLite was right for a proof of concept and
the migration away from it is recent, so most of the sharp edges in this codebase
are in that seam. The short version: SQLite's `BEGIN IMMEDIATE` takes a write
reservation over the *whole database*, which made every read-then-write in the
service layer serialisable for free. Nobody had to think about it, so nobody did
— and when the backend changed, that guarantee vanished without a single line of
the dependent code changing. Three bugs shipped through that gap, and the tests
did not catch them because the tests were still running on SQLite.

If you take one thing from this section: **the tests run against a real Postgres
schema** (`tests/conftest.py` explains how), and that is not a convenience. It is
the only check that actually works.

yoyo-migrations takes another 20 minutes. A migration is a `.sql` file with a
matching `.rollback.sql` file. One gotcha: yoyo reads one folder and does not
look inside subfolders. Point it at the wrong level and it silently applies
nothing.

**6. How the reviewer interface works (about 3 hours) — before working in `web/`**

There is one big idea, and most of the code follows from it:

> **Anything the server knows belongs to TanStack Query. Components hold only
> what is on the screen right now.**

No component fetches in a `useEffect`. `web/src/api/queries.ts` declares every
read and every write as a hook, each keyed by what it is *and by who is asking* —
roles change what the API returns, so a cache keyed without the actor would hand
the second reviewer the first one's view. Writes invalidate keys rather than
patching the cache, because the server rejects decisions on escalated candidates
and versions a rubric on save: a cache written from what the client hoped
happened would show an outcome the system did not record.

Learn, in order: TanStack Query (`useQuery`, `useMutation`, query keys,
`invalidateQueries`, `refetchInterval`), then React Router (nested routes,
`useParams`, `useSearchParams`), then enough Tailwind to read the markup.

The other idea is that **the URL is the state**. The Streamlit version kept the
selected run in a sidebar that every page read and three pages wrote, and the
dropdowns fought each other: 1,207 script re-runs in 150 seconds, each one making
an API call. That class of bug cannot occur here, because there is nothing to
share — a run *is* `/runs/:runId`, so the back button works and a reviewer can
send a colleague the exact screen they are looking at.

**7. Ollama (about 2 hours) — before working in `clients/` or `llm/`**

Ollama runs the AI models locally. The key idea:

When you pass a JSON schema as `format=`, Ollama doesn't *ask* the model to
produce that shape. It physically prevents the model from generating any text
that would break the schema. That's why there is no code anywhere in this repo
that tries to repair broken JSON from the model.

But it only constrains the *shape*, not the *meaning*. The model can still
return the right shape with wrong contents, which is what
`core/validate_verdicts.py` exists to catch.

Then learn the settings that matter, all of which are in `config/settings.py`
with comments explaining what goes wrong without them:

* `num_ctx` — how much text the model can read. Go over it and Ollama **silently
  cuts off** the input. The model then judges half a CV and reports no error.
* `num_predict` — how much the model can write. Too low and the JSON gets cut off
  mid-object.
* `temperature`, `top_k`, `seed` — set for repeatability. Same input, same
  output.
* `keep_alive` — how long to keep the model in memory between calls.
* `OLLAMA_NUM_PARALLEL=1` — only one request at a time. See "The two AI models"
  below for why this is important.

Honestly, `config/settings.py` is the best Ollama tutorial in this repository.

#### Learn these when you happen to land in them (an hour each, at most)

**8. Typer** — one decorated function per command. `cli.py` is straightforward.

**9. structlog** — the shape of the log events matters more than the library.
Note that this app has *two separate log streams* and they are not
interchangeable:

* The **audit log** is a table in Postgres. It records *who did what*: approvals,
  overrides, purges, sign-offs. It can only be appended to, enforced by a
  database trigger.
* **`screener.jsonl`** is a rotating log file. It records *what the system did*:
  timings, retries, crashes, budget overflows.

The reason for JSON rather than sentences: you need to ask questions like "how
many parses crashed last night?" You can't ask that of prose.

**10. mypy and ruff** — you'll meet these as build failures rather than as
reading. `./scripts/dev.sh check` runs both.

**11. hypothesis** — used only in the pure logic layer. Learn `@given` and the
handful of strategies actually used. Ignore stateful testing.

**12. `resource`, `subprocess`, fork safety** — only needed for
`intake/sandbox.py`, and only if you're interested in safely parsing hostile
files.

### What you do NOT need to learn

People often expect these and they aren't here:

* No Docker or Kubernetes.
* **No ORM.** SQLAlchemy is here, but only its Core layer — an engine, a
  connection pool, and `text()`. No models, no session, no relationships, no
  lazy loading. Open `storage/results_store.py` and you are looking at the
  actual queries.
* No Celery, Redis, or message queue. The job queue is a table (`jobs`), and
  `FOR UPDATE SKIP LOCKED` is what makes it one.
* No LangChain or agent framework. Prompts are markdown files, and the model is
  called directly.
* No cloud SDKs.
* **Almost no async.** The only `async` code is a small wrapper in
  `parse_worker.py`, because the PDF library only offers an async API.

---

## Part 3 — Reading the code, layer by layer

The layers are enforced by an automated test, so reading bottom-up genuinely
works. Nothing in an early layer refers to anything you haven't seen yet.

### Layer 1: the vocabulary (about 935 lines)

    screener/models.py    (743)
    screener/ports.py     (192)

Every other file in the project is a function over these types. Read
`models.py` for the data shapes, and `ports.py` for the "swappable parts".

Not all the swappable parts are equally real. `LLMClient` and `ResumeParser` are
genuine — you could replace Ollama or the PDF parser by writing one new class.
The storage ones are more of a statement of intent (see "What isn't built yet").

Three fields in `models.py` matter more than the rest, because everything
downstream depends on them:

* `Candidate.resume_text` — the CV as a human reads it.
* `Candidate.sent_text` — the CV as the AI actually saw it, after cleaning and
  after personal details were blanked out.
* `ScoredCriterion.match_blocks` — which exact characters of `sent_text` the AI's
  quoted evidence matched.

So there are three versions of the CV text and two are saved. §12.6 of the spec
explains why the third isn't.

### Layer 2: the spine (357 lines)

    screener/pipeline.py

Two functions, and they are the whole product.

`judge_one` is phase 1. About 40 lines of ordering: validate the file, extract
text, clean it, check for prompt-injection attempts, blank out personal details,
check it fits in the model's context, ask the AI, check the answer covers every
criterion, strip irrelevant commentary, verify the quoted evidence actually
appears in the CV, check for negation ("no experience with Python"), then score.

`verify_one` is phase 2 and much shorter: two AI calls and one pure function
that folds the results back in.

One ordering rule is worth noticing. Evidence is matched against `sent_text` —
the exact string the model was given. If you matched against the original text
instead, every quote near a blanked-out phone number would fail. The conversion
back to original-text positions happens later, when the data is read, in
`core/offsets.py`.

Once you've read these two functions you can place every other file in the
project.

### Layer 3: the decisions (`core/`, about 1,700 lines, all pure functions)

Read them in the order the pipeline calls them:

    detect_injection.py   (171)   flag suspicious text for review, never auto-reject
    redact_pii.py         (245)   blank out personal details; produces a position map
    offsets.py             (73)   convert positions between the two text versions
    budget.py             (136)   make sure nothing gets silently cut off
    validate_verdicts.py  (102)   the AI answered for exactly the right criteria
    screen_freetext.py    (139)   strip commentary that isn't about the job
    verify_evidence.py    (388)   does the AI's quote actually appear in the CV?
    detect_negation.py    (117)   the quote is there, but does it say the opposite?
    reconcile_judge.py    (134)   fold in phase 2, without changing any verdict
    compute_score.py       (68)   arithmetic only; the AI never produces a score
    rank.py                (60)   split into three groups, never one long list
    resume_paths.py        (74)   check a folder path is real and inside bounds

**Read this part slowly.** Every consequence for a real person is decided here.
There is no database access and no network access in any of it, so you can paste
any of these functions into a Python shell and try them on made-up data.

Read each file with its test file open next to it. `tests/test_verify_evidence.py`
is the fastest way into `verify_evidence.py`, which is the hardest file here.
`tests/test_reconcile_judge.py` is the shortest way to see the "never overrules"
rule actually being checked.

### Layer 4: the plumbing

    screener/service.py                (1206)  where transactions begin and end
    screener/worker_loop.py             (290)  the background job runner
    screener/storage/uow.py              (89)  the transaction object itself
    screener/storage/connection.py      (163)  WAL, threading, migration check
    screener/storage/jobs_store.py      (492)  all the concurrency lives here
    screener/storage/results_store.py   (505)
    screener/storage/runs_store.py      (163)
    screener/storage/audit_store.py     (139)
    screener/storage/resumes_store.py   (115)  finds CV folders on the share
    screener/storage/rubrics_store.py   (111)
    screener/storage/positions_store.py  (88)

`service.py` is the largest file and most of it is the same rhythm repeating:
open a transaction, call some stores, write an audit record, commit.

Two rules shape it:

1. **Stores never open a transaction.** They are handed one. This is what makes
   multi-table operations all-or-nothing.
2. **`service.py` is not allowed to import `pipeline.py`.** It only adds jobs to
   the queue; the background worker runs them. Otherwise screening 1,000 CVs
   would happen inside a single web request.

CVs are read from whatever `RESUMES_DIR` points at — a local folder, or a
network share mounted by IT. `resumes_store.list_folders()` lists what's
available for the UI to show, and `core/resume_paths.py` makes sure a chosen
folder is genuinely inside the allowed area.

The interesting parts of `service.py`, worth finding by name:

* `record_decision` — three writes in one transaction.
* `save_rubric` — detects two people editing the same rubric and returns a 409.
* `approve_rubric` — refuses while any criterion is marked as needing re-checking.
* `sign_off_run` — refuses while any flagged candidate hasn't been decided.
* `advance_phase_if_complete` — the switch from phase 1 to phase 2.

All the concurrency is in `jobs_store.py` and nowhere else. Claiming a job is a
single SQL statement (`UPDATE ... WHERE id = (SELECT ... LIMIT 1) RETURNING`),
so there is no gap between picking a job and owning it during which another
worker could grab the same one.

### Layer 5: the edges

    screener/clients/ollama_client.py  (394)   talking to the AI model
    screener/intake/validate_file.py   (277)   is this file safe to open?
    screener/intake/sandbox.py         (438)   parsing untrusted files safely
    screener/intake/parse_worker.py    (102)   the actual PDF/DOCX extraction
    screener/schemas.py                (616)   the HTTP contract
    screener/api/                    (~450)    routes: parse, check permission, delegate
    web/src/                        (~3400)    the reviewer interface (TypeScript)

`schemas.py` deserves more attention than its position suggests. The rule that
auditors see more fields than recruiters is a data-disclosure boundary, and it's
implemented by returning a *different class* to an auditor rather than by
filtering a dictionary. Filtering is easy to forget in one place; returning the
wrong type is caught by the type checker.

The interface is a route tree over four concepts:

    web/src/api/client.ts        the only module that performs network I/O
    web/src/api/queries.ts       every read and write as a hook; caching and polling
    web/src/api/types.ts         the wire contract, mirrored from schemas.py
    web/src/lib/labels.ts        the words on screen: flag help, escalation reasons,
                                 audit sentences. Each one is a decision, not a string
    web/src/ui/                  Button, Alert, Card, Table… thin Tailwind wrappers
    web/src/components/          the parts with domain meaning: rubric editor,
                                 candidate detail, escalation meter, folder picker
    web/src/pages/               one file per screen, matching the routes below

    /                            overview: three counts, and the runs holding
                                 the review queue
    /requisitions                the list, and the way into a new one
    /requisitions/:positionId    rubric: draft, edit, approve, then start a run;
                                 close the requisition when the post is filled
    /runs                        every run, newest first
    /runs/:runId                 live progress, controls, failed files
    /runs/:runId/review          the three candidate groups and the decisions
    /audit, /audit/record,       run story, one candidate's record, the searchable log
    /audit/search

Read `lib/labels.ts` early. It is where the judgement about what a reviewer is
told when the system is uncertain actually lives.

Read `sandbox.py` last, and only if hostile files interest you. It is the most
specialised code here and teaches you nothing about the domain.

---

## Part 4 — Three shortcuts that beat reading everything

### 1. `tests/test_layering.py` is the architecture, written as code

Nineteen rules, each checked against the real import graph, each with a comment
saying what breaks if you violate it. It's 391 lines and it answers "what is
allowed to import what?" definitively.

Three rules carry most of the weight:

* `service.py` must not import `pipeline.py` — or a 1,000-CV batch runs inside
  one HTTP request.
* the reviewer interface reaches data only over HTTP — `fetch` appears in exactly
  one file, nothing renders raw HTML, and no absolute URL exists anywhere in it.
  Under Streamlit this rule was about a Python process that could `import
  sqlite3`; a browser tab cannot, so what is left to enforce is the app's own
  discipline.
* `core/` must not do any I/O — which is why all the scoring rules can be tested
  without a database or a GPU.

### 2. Look at one candidate the way an auditor sees them

```bash
curl -s -H 'X-Actor-Roles: auditor' \
  localhost:8000/candidates/1 | .venv/bin/python -m json.tool
```

You get `sent_text` (the exact text the AI was given) next to each criterion's
quoted evidence, whether that evidence was verified, how well it matched, what
the first model said, and what the second model thought about it.

Compare `sent_text` against `resume_text` in the same response and the redaction
becomes concrete. That one response teaches you more about how the app works
than reading three files.

One detail: the highlight positions in the response point into `resume_text`
(what the human reads), but they were originally calculated against `sent_text`
(what the AI read). `core/offsets.py` converts between them at read time. If you
skip that conversion, highlights look perfect until a CV has a redaction above
the quote, and then they're silently wrong. There's a test named after exactly
that in `tests/test_read_layer.py`.

If the AI ever returns malformed output, the raw response is saved in
`data/failures/`. That folder is empty on a healthy run, by design.

### 3. Run it once yourself

```bash
.venv/bin/screener work --limit 1
```

Then read `worker_loop.py` immediately afterwards. Watching it claim, screen and
finish one job teaches more than tracing it on paper. The command needs neither
the API nor the background service running.

Watch for the phase switch: the worker finishes *every* phase-1 job for a run,
then unloads the first model, loads the second, and does all the phase-2 jobs.
The models are loaded twice per run, not twice per CV. That's the whole reason
the run has two phases, and it's forced by having only 12 GB of video memory.

---

## Part 5 — How to read the comments

The docstrings in this codebase explain *reasoning*, not mechanics. Many of them
record something that actually went wrong on this system:

* `core/verify_evidence.py` — why a check that worked correctly was deliberately
  downgraded to a warning, after it removed the two best candidates from a real
  run.
* `core/reconcile_judge.py` — why the escalation reasons are calculated from the
  flags rather than collected alongside them.
* `intake/sandbox.py` — why the memory limit is relative to current usage rather
  than a fixed number (the fixed number killed every parse).
* `storage/connection.py` — why each thread makes its own database connection,
  and why pointing yoyo at the wrong folder silently disables all migrations.
* `web/src/api/queries.ts` — why the query keys carry the actor, and why writes
  invalidate rather than patch. The header of `web/README.md` carries the
  1,207-re-runs-in-150-seconds story that the migration away from Streamlit
  ended.
* `config/settings.py` — why the parse memory limit is 2048 MB, why the timing
  estimate is 5.4 seconds per CV, and why the minimum evidence length is 16
  characters. All three were measured, not guessed. The 16 replaced a 25 that had
  been copied over from a rejected idea and never re-checked; at 25, three
  perfectly good short quotes were being flagged.

When a number looks arbitrary, the comment above it usually says what was
measured to pick it. `git grep -n "Measured"` finds them all.

---

## Part 6 — Map of the repository

| Path | What's in it |
|---|---|
| `screener/models.py`, `ports.py` | Data shapes and swappable interfaces |
| `screener/core/` | Every decision that affects a candidate. Pure functions |
| `screener/intake/` | File checking, safe parsing, text cleaning |
| `screener/llm/`, `screener/prompts/` | Prompts. Stored as files and hashed, not as strings in code |
| `screener/clients/` | Ollama. The only place the AI is spoken to |
| `screener/storage/` | Postgres, migrations, transactions, seven stores |
| `screener/pipeline.py` | The two main functions: `judge_one` and `verify_one` |
| `screener/service.py` | Transactions, audit records, permissions, preconditions |
| `screener/schemas.py` | The HTTP contract, including role-based views |
| `screener/worker_loop.py`, `worker.py` | The background service |
| `screener/api/` | FastAPI. Parses requests, checks permission, delegates |
| `screener/cli.py` | Typer. Do anything without the API or the UI |
| `web/` | React + TypeScript. Built into static files the API serves at `/ui/` |
| `config/settings.py` | All configuration in one typed object. Read the comments |
| `eval/` | Tools for measuring whether a change actually improved anything |
| `tests/` | 636 tests. 599 run anywhere; 37 need a real GPU and model |
| `deploy/` | systemd service files for the API (which also serves the UI) and the worker |
| `scripts/dev.sh` | Local runner: `start`, `stop`, `restart`, `status`, `logs`, `build`, `check` |

---

## Part 7 — The AI model

Both phases run on `gemma4:12b` — judge and verifier were consolidated to one
model once `verify_scope` defaulted to `"none"` (see `config/settings.py`),
since a verifier trained on the same data as the judge shares its blind spots
anyway, and the two-model design's value came from a check that's now off by
default. The two-phase structure and the `ensure_loaded`/`unload` swap
mechanism remain in place — a run still has a judge phase and a verify phase —
in case the models diverge again, at which point the original constraint
returns: two distinct ~8 GB+ models don't both fit in 12 GB of video memory at
once, which is why a run has two phases instead of interleaving models per CV.

`OLLAMA_NUM_PARALLEL=1` is locked to 1 and it matters more than it looks. One
slot means one request at a time, which makes Ollama's prompt caching
predictable. The app counts the tokens of a prompt first using the *exact same
bytes* it's about to send, so the real call reuses the cache from the counting
call.

Measured on this repo: 246.489 seconds to process the prompt the first time,
then 0.244 seconds for the actual judging call on the identical prompt. That's a
thousand times faster. Change even one character of the prompt between those two
calls and you pay the full cost twice.

---

## Part 8 — What you can skip

`web/src/pages/audit/DecisionRecordPage.tsx`, `storage/results_store.py` (505)
and `cli.py` (451) are the biggest files after `service.py`, and the least
informative. They're display code and SQL over a domain you'll already
understand.

`eval/` only matters when you need to prove a change made things better.

---

## Part 9 — What isn't built yet

Read this before reporting any of it as a bug.

* **Microsoft SQL Server support.** The spec describes it. It doesn't exist, and
  the SQLite-to-Postgres move is the reason to be sceptical about how cheap it
  would be: that was one backend to one other backend, with a Protocol seam
  already in place, and it still shipped three silent dialect bugs. `ports.py`
  names a storage seam, but nothing has ever gone through it — the move went
  through `connection.py`. Treat a third backend as real work, not a config
  value. It is also still blocked on getting the Microsoft ODBC driver onto a
  machine with no internet.
* **The verifier comparison hasn't been run.** `eval/compare_verifiers.py` exists
  and is tested, but producing the actual comparison needs a GPU and the
  labelled data set. Until then, verifying *every* criterion is a default rather
  than a measured choice.
* **`escalation_budget` is deliberately unset.** 3% was a guess, and a guessed
  budget is worse than none because it reads like a measurement in every report
  that quotes it. The worker warns on every run while it's unset, so "we'll
  measure it later" can't quietly become "never". Note that `web/src/lib/labels.ts` has
  its own 3% constant for the on-screen meter — that's a display value, not this
  setting.
* **The labelled data set has one labeller, not the two required.** It says so in
  its own header. Any accuracy figure from it is provisional.
* **Authentication is a stub.** The `X-Actor` and `X-Actor-Roles` HTTP headers
  are simply trusted. This is only acceptable because the API listens on
  localhost only. It must be replaced before anything external can reach it.
  Replacing it *is* one function (`get_actor`) — the `actor` parameter is already
  threaded through every signature, which is the expensive part.
* **There is no authorization model, which is a separate gap from the one above.**
  Roles gate what a response *contains* — an auditor sees more fields than a
  recruiter — and nothing else. No write endpoint checks a role, so
  `approve_rubric`, `record_decision`, `sign_off_run` and `purge_candidate` are
  open to anyone who authenticates. One person can draft a rubric, approve it,
  decide every candidate and sign off the run; the audit trail records that
  faithfully, it just doesn't prevent it. Authenticating the caller gives you
  identity, not permission — both are needed before an external listener exists.
* **Backups are outside the erasure path.** `purge_candidate` clears the database
  columns and the failure captures under `data/`, which was the whole story when
  the database was a file in `data/`. It isn't any more: `pg_dump` output and WAL
  archives hold full resume text, and nothing here reaches them. A retention and
  encryption policy for those is an operational decision nobody has made yet.

---

## Part 10 — If you only have an hour

1. Spec §24, the diagram.
2. `screener/pipeline.py`, both functions.
3. `core/verify_evidence.py`, `core/reconcile_judge.py`, `core/compute_score.py`.

That's the actual judgement the product makes, in about 600 lines.

---

## Part 11 — Check that you've understood it

First, set up. You need a Postgres you can reach and a `DB_URL` pointing at it —
the suite creates a schema of its own inside that database, so it will not touch
your working data, but it does need somewhere to put it:

```bash
uv sync --extra dev --extra postgres
cp .env.example .env            # then set DB_URL
.venv/bin/screener migrate      # apply the schema
```

Then run the build checks. They take about a minute and need no GPU:

```bash
./scripts/dev.sh check          # formatting, linting, type checking, tests
```

It refuses to start if the dev tools are missing or the database is unreachable,
and names which — a gate that skips silently is not a gate. That covers the
reviewer interface too — prettier, eslint, tsc and vitest — when
`web/node_modules` is present. It warns and skips *those* when it isn't, so a
backend change is never blocked by a machine with no Node on it.

To see the interface itself:

```bash
(cd web && npm install)         # once
./scripts/dev.sh start          # API, worker, and the Vite dev server
                                # → http://127.0.0.1:5173/ui/

./scripts/dev.sh build          # what a deployment does instead:
                                # builds web/dist, which the API serves at /ui/
```

Then try to answer these from the code. Every one has a definite answer.

1. A candidate doesn't meet a must-have requirement. Where is it decided that
   this does *not* flag them for human review, and why not?
2. What happens to a candidate whose quoted evidence can't be found in their CV?
   How is that different from evidence that contradicts the verdict it was given?
3. Nine things go into the results cache key. Which one is deliberately left out,
   so that an interrupted run can be resumed?
4. The verifier says there's insufficient support for a criterion the judge
   scored as strong. What changes in the database, and what doesn't?
5. A reviewer highlights evidence in the UI. Which text is being highlighted,
   which text were the positions calculated against, and what bridges the two?
6. Why is the API started with `--factory` instead of pointing at an `app`
   variable?
7. Where would you add support for a second AI backend, and how many files would
   you have to change?
8. Three pages each have a "which run?" dropdown. What stops them fighting over
   the shared value, and what did the previous approach cost?
9. Someone submits a folder path of `../../etc`. Name every layer it passes
   through, and the one that rejects it.
10. Two workers ask for a job at the same moment. What stops them getting the
    same one, and what did the previous answer to that question get wrong?
11. A purge runs against a `file_sha256` that does not exist. What does the
    caller see, and why is that different from a purge that erased a row?

If you can answer those, you can change this codebase safely.
