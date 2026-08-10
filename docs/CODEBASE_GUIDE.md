# Reading this codebase

A route through ~8,900 lines of source, in the order that makes each file
comprehensible when you reach it. The layering is enforced by tests, so reading
bottom-up genuinely works: nothing early references anything you have not seen.

Budget: **20 minutes** for orientation, **half a day** for the five layers, and
you will be able to place any file in the repository.

The authority on *why* anything is the way it is remains `screener_spec_v4.md`.
This document is a route through it and the code, not a replacement for either.

---

## Step 0 — twenty minutes, no code

Read three things, in this order:

| Where | What it gives you |
|---|---|
| `screener_spec_v4.md:1913` (section 23) | The whole system as one ASCII flow, JD to CSV |
| `screener_spec_v4.md:1389` (section 13) | `screen_one` as ten lines of pseudocode — this *is* the product |
| `screener_spec_v4.md:17` (section 1) | The locked decisions. Most "why is it like this?" questions end here |

Then stop. The spec is 2,000 lines and it is a reference, not a tutorial. Come
back to it by section number when a file raises a question.

The one sentence worth carrying into the code: **this system ranks people, and
every rule that can cost a candidate a place is a pure function in `core/`.**
That constraint explains most of the architecture.

---

## The five layers

### 1. The vocabulary — ~550 lines

    screener/models.py    (379)
    screener/ports.py     (164)

Every other file is a function over these types. Read `models.py` for the shapes
and `ports.py` for the seams — and note which seams are real: `LLMClient` and
`ResumeParser` are genuine swap points (callers are typed against the Protocol),
while the storage Protocols are declarations that `tests/test_layering.py` keeps
honest. Section 6 of the spec is explicit that storage portability is *not* the
one-file swap the inference ports are.

### 2. The spine — 308 lines

    screener/pipeline.py    ::screen_one

One function, top to bottom, about 40 lines of actual sequence: validate, parse,
sanitize, detect injection, redact, budget, judge, validate verdicts, screen free
text, verify evidence, score. Once you have read it you can place every other
file in the repository.

Note the ordering rule it encodes: `verify_evidence` matches against the exact
string sent to the model — post-sanitize, post-redaction. Matching anything else
fails on every quote near a redaction.

### 3. The decisions — `core/`, ~750 lines, all pure

Read in pipeline order:

    detect_injection.py   (154)   review, never exclude
    redact_pii.py         (131)
    budget.py             (136)   silent truncation is the failure it prevents
    validate_verdicts.py  (102)   set equality against the rubric
    screen_freetext.py    (139)
    verify_evidence.py    (309)   the anti-hallucination checks
    compute_score.py       (68)   arithmetic only — the model never scores
    rank.py                (60)   three partitions, never one list

**This is the part to read slowly.** Every consequence for a person is decided
here, and there is no I/O anywhere in it, so each function can be run in a REPL
against a literal.

Read each one with its test file open beside it. `tests/test_verify_evidence.py`
next to `screener/core/verify_evidence.py` is the fastest way into the hardest
module in the codebase.

### 4. The seams

    screener/service.py         (702)   transaction boundary
    screener/worker_loop.py     (248)   claim, heartbeat, reclaim
    screener/storage/uow.py      (89)
    screener/storage/jobs_store.py (389)

`service.py` is the biggest file and mostly rhythm: open a unit of work, call
stores, append an audit row, commit. The rule that shapes it is that **stores
never open a transaction** — they receive one. And `service.py` may not import
`pipeline.py`: it enqueues, the worker executes.

All of the concurrency lives in `jobs_store.py` and nowhere else.

### 5. The edges

    screener/clients/ollama_client.py  (330)   structured output, token counting
    screener/intake/validate_file.py   (277)
    screener/intake/sandbox.py         (438)   rlimits, subprocess, fork safety
    screener/api/                              routes parse, authorize, delegate
    ui/                                        HTTP client only

Read `sandbox.py` last, and only if hostile files interest you. It is the most
specialised code here and teaches you nothing about the domain.

---

## Three shortcuts that beat reading linearly

### `tests/test_layering.py` is the architecture, executable

Nineteen rules read from the AST import graph, each with a docstring saying what
goes wrong if you break it. It is the fastest 350 lines in the repository, and it
answers "what is allowed to import what" definitively rather than by convention.

Three rules carry most of the weight:

- `service.py` must not import `pipeline.py` — otherwise a 1,000-CV batch runs
  inside an HTTP request.
- `ui/` must not import `sqlite3` or `screener.*` — the real enforcement of the
  UI-is-an-HTTP-client decision, since `sqlite3` ships with Python and cannot be
  uninstalled.
- `core/` must do no I/O — which is why the scoring and evidence rules are
  testable without a GPU or a database.

### Read a real trace

One judged candidate, complete, as it actually happened:

```bash
.venv/bin/python -m json.tool \
  data/traces/run-62975f4a43e3/a68a9588f7cac18ddba8318c52eddd5937806d986217a5278c7c35eadccd3e91.jsonl
```

You get the exact system prompt, the exact user message (criteria plus the
delimited resume), the model, `num_ctx`, `seed`, `temperature`, the prompt token
count, the attempt count, and the raw output. That single file makes the prompt
contract and the LLM client concrete in a way that reading them will not. There
are 445 of them under `data/traces/`.

### Run it once yourself

```bash
.venv/bin/screener work --limit 1
```

Then read `worker_loop.py` immediately afterwards. Watching it claim, screen and
complete a single job is worth more than tracing it statically. The CLI needs
neither the API nor the daemon — that is the break-glass path, and it exercises
the same `Worker` class the daemon runs.

---

## How to read the comments

The docstrings carry reasoning, not mechanics, and many of them record things
that actually failed on this system:

- `screener/core/verify_evidence.py:269` — why a check that works was
  deliberately downgraded to flag-only, after it removed the two strongest
  candidates from a live run.
- `screener/intake/sandbox.py` — why the process limit is relative to current
  usage rather than absolute (the absolute one killed every parse).
- `config/settings.py` — why `parse_mem_limit_mb` is 2048 and
  `evidence_match_min_chars` is 16. Both numbers were measured, not chosen.

When a constant looks arbitrary, the comment above it usually says what was
measured to pick it. `git grep -n "Measured"` finds ten of them.

---

## Repository map

| Path | What lives there |
|---|---|
| `screener/models.py`, `ports.py` | Domain contracts and seams |
| `screener/core/` | Every decision that affects a candidate. Pure |
| `screener/intake/` | File validation, sandboxed parsing, Unicode sanitizing |
| `screener/llm/`, `screener/prompts/` | Prompt contracts. Prompts are hashed files, not literals |
| `screener/clients/` | Ollama. The one place inference is spoken to |
| `screener/storage/` | SQLite, migrations, unit of work, stores |
| `screener/pipeline.py` | The screening sequence |
| `screener/service.py` | Transactions, audit, authorization |
| `screener/worker_loop.py`, `worker.py` | The daemon that screens the queue |
| `screener/api/` | FastAPI. Parses, authorizes, delegates, serializes |
| `ui/` | Streamlit. HTTP client, no database, no domain imports |
| `eval/` | The harness that decides whether a change improved anything |
| `tests/` | 448 tests. `test_layering.py` is the architecture |
| `deploy/` | systemd units |
| `scripts/dev.sh` | Local runner: start, stop, restart, status, logs, check |

---

## What to skip

`screener/cli.py` (424) and `ui/screener_app.py` (577) are the largest files
after `service.py` and the least informative — presentation over an API you will
already understand. `eval/` matters only when you need to judge whether a change
improved anything.

---

## If you only have an hour

1. Spec section 23 — the flow diagram.
2. `screener/pipeline.py` — the spine.
3. `screener/core/verify_evidence.py` and `screener/core/compute_score.py`.

That is the product's actual judgment in about 400 lines.

---

## Verifying you have understood it

Run the gates, which take about 30 seconds and need neither a GPU nor a database:

```bash
./scripts/dev.sh check          # ruff format, ruff check, mypy, pytest
```

Then try to answer these from the code. Each has a definite answer:

1. Where is the decision made that an unmet must-have does *not* set
   `review_required`? Why not?
2. What happens to a candidate whose evidence quote cannot be verified — and how
   does that differ from evidence that contradicts its own verdict?
3. Which eight fields go into the results cache key, and which one is deliberately
   excluded so that a run can resume?
4. Why does the API use `--factory` rather than a module-level `app`?
5. Where would you add a second LLM backend, and how many files would change?

If those are answerable, you can change this codebase safely.
