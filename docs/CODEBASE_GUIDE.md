# Reading this codebase

A route through ~11,400 lines of source, in the order that makes each file
comprehensible when you reach it. The layering is enforced by tests, so reading
bottom-up genuinely works: nothing early references anything you have not seen.

Budget: **20 minutes** for orientation, **half a day** for the five layers, and
you will be able to place any file in the repository.

The authority on *why* anything is the way it is remains `screener_spec_v6.md`.
This document is a route through it and the code, not a replacement for either.
`screener_spec_v4.md` is retained as history — where the two disagree, v6 wins,
and `v4_to_v6_migration.md` records what moved and why.

---

## Step 0 — twenty minutes, no code

Read three things, in this order:

| Where | What it gives you |
|---|---|
| `screener_spec_v6.md:1917` (section 24) | The whole system as one ASCII flow, JD to CSV, with both phases drawn |
| `screener_spec_v6.md:1286` (section 13) | `judge_one` and `verify_one` as pseudocode — this *is* the product |
| `screener_spec_v6.md:16` (section 1) | The locked decisions. Most "why is it like this?" questions end here |

Then stop. The spec is a reference, not a tutorial. Come back to it by section
number when a file raises a question.

Two sentences worth carrying into the code:

**This system ranks people, and every rule that can cost a candidate a place is a
pure function in `core/`.** That constraint explains most of the architecture.

**The verifier escalates and proposes; it never overrules.** Phase 2 is a second
model reading the same attacker-controlled text as the first. Letting it move an
outcome would mean a candidate's result changed because two models disagreed,
with no human involved. `core/reconcile_judge.py` is where that is enforced, and
it is pure so the guarantee is testable without a GPU.

---

## The five layers

### 1. The vocabulary — ~780 lines

    screener/models.py    (585)
    screener/ports.py     (192)

Every other file is a function over these types. Read `models.py` for the shapes
and `ports.py` for the seams — and note which seams are real: `LLMClient` and
`ResumeParser` are genuine swap points (callers are typed against the Protocol),
while the storage Protocols are declarations that `tests/test_layering.py` keeps
honest. Section 6 of the spec is explicit that storage portability is *not* the
one-file swap the inference ports are — and see "What is not built" below for
where that currently stands.

The three fields to notice in `models.py`, because everything downstream turns on
them: `Candidate.resume_text` (what HR reads), `Candidate.sent_text` (what the
model actually saw, post-sanitize and post-redaction), and
`ScoredCriterion.match_blocks` (which characters of `sent_text` a quote matched).
Three text versions exist and two are persisted; §12.6 says why the third is not.

### 2. The spine — 357 lines

    screener/pipeline.py    ::judge_one, ::verify_one

Two functions. `judge_one` is the phase-1 sequence, about 40 lines of actual
order: validate, parse, sanitize, detect injection, redact, budget, judge,
validate verdicts, screen free text, verify evidence, detect negation, score.
`verify_one` is phase 2 and is much shorter — two batched LLM calls and a pure
reconciliation.

Note the ordering rule `judge_one` encodes: `verify_evidence` matches against the
exact string sent to the model, post-sanitize and post-redaction. Matching
anything else fails on every quote near a redaction. Translation back into
`resume_text` coordinates happens at read time, in `core/offsets.py`.

Once you have read these two functions you can place every other file here.

### 3. The decisions — `core/`, ~1,600 lines, all pure

Read in pipeline order:

    detect_injection.py   (154)   review, never exclude
    redact_pii.py         (245)   produces the span map offsets depend on
    offsets.py             (73)   sent_text coordinates → resume_text coordinates
    budget.py             (136)   silent truncation is the failure it prevents
    validate_verdicts.py  (102)   set equality against the rubric
    screen_freetext.py    (139)
    verify_evidence.py    (388)   stage B — the anti-hallucination checks
    detect_negation.py    (117)   stage C — the context that inverts a quote
    reconcile_judge.py    (134)   phase 2 folded back in, and never onto a verdict
    compute_score.py       (68)   arithmetic only — the model never scores
    rank.py                (60)   three partitions, never one list

**This is the part to read slowly.** Every consequence for a person is decided
here, and there is no I/O anywhere in it, so each function can be run in a REPL
against a literal.

Read each one with its test file open beside it. `tests/test_verify_evidence.py`
next to `screener/core/verify_evidence.py` is the fastest way into the hardest
module in the codebase. `tests/test_reconcile_judge.py` is the shortest way to
see the "escalates, never overrules" invariant actually asserted.

### 4. The seams

    screener/service.py         (989)   transaction boundary
    screener/worker_loop.py     (290)   claim, heartbeat, reclaim, phase switch
    screener/storage/uow.py      (89)
    screener/storage/jobs_store.py (437)

`service.py` is the biggest file and mostly rhythm: open a unit of work, call
stores, append an audit row, commit. The rule that shapes it is that **stores
never open a transaction** — they receive one. And `service.py` may not import
`pipeline.py`: it enqueues, the worker executes.

The parts of `service.py` that are not rhythm are worth finding by name:
`record_decision` (three writes, one transaction), `save_rubric` (optimistic
locking on `base_version`, 409 on conflict), `approve_rubric` (blocked while any
criterion's `claim_stale` is set), and `sign_off_run` (refuses while any
review-required candidate is undecided).

All of the concurrency lives in `jobs_store.py` and nowhere else. The claim is
one statement — `UPDATE ... WHERE id = (SELECT ... LIMIT 1) RETURNING` — so there
is no window between choosing a job and owning it.

### 5. The edges

    screener/clients/ollama_client.py  (394)   structured output, token counting
    screener/intake/validate_file.py   (277)
    screener/intake/sandbox.py         (438)   rlimits, subprocess, fork safety
    screener/schemas.py                (457)   wire contracts + role-scoped views
    screener/api/                              routes parse, authorize, delegate
    ui/                                        HTTP client only

`schemas.py` is worth more attention than its position suggests: §15.2's
role-scoped field exposure is a disclosure boundary, and it is implemented by
returning a *different type* to an auditor rather than by filtering a dict. That
is also why `get_candidate` sets `response_model=None` — FastAPI would otherwise
re-validate the subclass back down to the base model and silently drop the
auditor's extra fields.

Read `sandbox.py` last, and only if hostile files interest you. It is the most
specialised code here and teaches you nothing about the domain.

---

## Three shortcuts that beat reading linearly

### `tests/test_layering.py` is the architecture, executable

Nineteen rules read from the AST import graph, each with a docstring saying what
goes wrong if you break it. It is the fastest 391 lines in the repository, and it
answers "what is allowed to import what" definitively rather than by convention.

Three rules carry most of the weight:

- `service.py` must not import `pipeline.py` — otherwise a 1,000-CV batch runs
  inside an HTTP request.
- `ui/` must not import `sqlite3` or `screener.*` — the real enforcement of the
  UI-is-an-HTTP-client decision, since `sqlite3` ships with Python and cannot be
  uninstalled.
- `core/` must do no I/O — which is why the scoring and evidence rules are
  testable without a GPU or a database.

### Read one candidate as the auditor sees it

Traces are gone (v6 removed them: a second store of full résumé text to index,
retain, permission and purge, holding what `sent_text` now holds). The v6
equivalent is the auditor view, which is *more* informative because it shows the
model's input and the matched offsets side by side:

```bash
curl -s -H 'X-Actor-Roles: auditor' \
  localhost:8000/candidates/1 | .venv/bin/python -m json.tool
```

You get `sent_text` — the exact string the model was given — alongside each
criterion's `evidence`, `evidence_status`, `match_ratio`, `longest_span`,
`model_verdict`, and the `verifier` panel (`disagrees`, `suggested_verdict`,
`rationale`). Diff `sent_text` against `resume_text` in the same payload and the
redaction span map becomes concrete. That single response makes the prompt
contract, the evidence check and the verifier's bounded role concrete in a way
that reading them will not.

Note what the `highlights` on each criterion are: character offsets into
`resume_text`, not `sent_text`. The match blocks are computed against `sent_text`
and translated at read time by `core/offsets.py`. Highlighting the untranslated
offsets renders correctly right up until a résumé contains a redaction above the
quote, and then it is silently wrong — `tests/test_read_layer.py` has a test
named for exactly that.

For a schema failure specifically, `data/failures/` holds the raw model output —
captured only on failure, so it is empty on a healthy run.

### Run it once yourself

```bash
.venv/bin/screener work --limit 1
```

Then read `worker_loop.py` immediately afterwards. Watching it claim, screen and
complete a single job is worth more than tracing it statically. The CLI needs
neither the API nor the daemon — that is the break-glass path, and it exercises
the same `Worker` class the daemon runs.

The phase switch is the part to watch for: the worker drains every `judge` job
for a run, then unloads granite, loads gemma, and drains the `verify` jobs. The
model is loaded twice per run, not twice per résumé — that is the whole reason
the run is two phases rather than one pass, and it is forced by 12 GB of VRAM.

---

## How to read the comments

The docstrings carry reasoning, not mechanics, and many of them record things
that actually failed on this system:

- `screener/core/verify_evidence.py` — why a check that works was deliberately
  downgraded to flag-only, after it removed the two strongest candidates from a
  live run.
- `screener/core/reconcile_judge.py` — why `escalation_reasons` is *derived* from
  the flags rather than accumulated alongside them.
- `screener/intake/sandbox.py` — why the process limit is relative to current
  usage rather than absolute (the absolute one killed every parse).
- `config/settings.py` — why `parse_mem_limit_mb` is 2048, `seconds_per_resume`
  is 5.4, and `evidence_match_min_chars` is 16. All three were measured.

When a constant looks arbitrary, the comment above it usually says what was
measured to pick it. `git grep -n "Measured"` finds them.

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
| `screener/pipeline.py` | `judge_one` (phase 1) and `verify_one` (phase 2) |
| `screener/service.py` | Transactions, audit, authorization, preconditions |
| `screener/schemas.py` | Wire contracts and the §15.2 role-scoped views |
| `screener/worker_loop.py`, `worker.py` | The daemon that drains both phases |
| `screener/api/` | FastAPI. Parses, authorizes, delegates, serializes |
| `ui/` | Streamlit. HTTP client, no database, no domain imports |
| `eval/` | The harness that decides whether a change improved anything |
| `tests/` | 547 tests run without a GPU; 37 more are `live`-marked. `test_layering.py` is the architecture |
| `deploy/` | systemd units |
| `scripts/dev.sh` | Local runner: start, stop, restart, status, logs, check |

---

## The two models

Phase 1 judges with `granite4.1:8b`; phase 2 verifies with `gemma4:12b`. They do
not fit in 12 GB together, which is why the run is phased rather than
interleaved, and why `OLLAMA_NUM_PARALLEL=1` is locked (spec §1, row 5).

That last setting is load-bearing in a way that is easy to undo by accident: one
slot means one in-flight request, which is what makes Ollama's prompt-cache reuse
deterministic. `count_prompt_tokens` runs a pre-flight count with the *same
bytes* the judging call will send, so the second call inherits the first's
KV cache. Measured on this repo: 246.489 s to pre-fill, then 0.244 s for the
judging call on the identical prompt — a factor of a thousand. Change the prompt
between those two calls and you pay the pre-fill twice.

---

## What to skip

`ui/screener_app.py` (723) and `screener/cli.py` (451) are the largest files after
`service.py` and the least informative — presentation over an API you will
already understand. `eval/` matters only when you need to judge whether a change
improved anything.

---

## What is not built

Read this before concluding a gap is a bug.

- **MS SQL.** Spec §12.2 describes a `storage/dialect/` seam and a second
  migration directory. Neither exists: the PoC runs on SQLite by decision, and
  the port is deferred to production. Several store queries are SQLite-only today
  — `INSERT OR IGNORE`, the two-argument `MAX`, `LIMIT`, and the `RETURNING`
  claim — so the swap is a real piece of work, not a config change. It is also
  gated on the MS ODBC Driver 18 dependency closure being resolved for an
  air-gapped host.
- **The §19.3 verifier comparison has not been run.** `eval/compare_verifiers.py`
  exists and is tested against fakes; the table it produces needs a GPU and the
  labelled set. Until it runs, `verify_scope="all"` is a default rather than a
  finding.
- **`escalation_budget` is `None`,** deliberately (§19.2), so "measure it later"
  cannot quietly become "never". Setting it requires recording
  `escalation_budget_source_run`.
- **The labelled set has one labeller, not the two §18.1 requires.** The corpus
  says so in its own provenance header, in terms nobody can quote past. Any
  accuracy figure from it is provisional.
- **Auth is stubbed.** `X-Actor` and `X-Actor-Roles` are trusted headers, which
  is acceptable only while the port is on loopback and `auth_mode` is `stub`.

---

## If you only have an hour

1. Spec section 24 — the flow diagram.
2. `screener/pipeline.py` — both functions.
3. `screener/core/verify_evidence.py`, `core/reconcile_judge.py`, and
   `core/compute_score.py`.

That is the product's actual judgment in about 600 lines.

---

## Verifying you have understood it

Run the gates, which take about 45 seconds and need neither a GPU nor a database:

```bash
./scripts/dev.sh check          # ruff format, ruff check, mypy, pytest
```

Then try to answer these from the code. Each has a definite answer:

1. Where is the decision made that an unmet must-have does *not* set
   `review_required`? Why not?
2. What happens to a candidate whose evidence quote cannot be verified — and how
   does that differ from evidence that contradicts its own verdict?
3. Which nine fields go into the results cache key, and which one is deliberately
   excluded so that a run can resume?
4. The verifier returns `support="insufficient"` and `suggested_verdict="none"`
   for a criterion the judge scored `strong`. What changes in the database, and
   what does not?
5. A reviewer highlights evidence in the UI. Which text is being highlighted,
   which text were the offsets computed against, and what closes the gap?
6. Why does the API use `--factory` rather than a module-level `app`?
7. Where would you add a second LLM backend, and how many files would change?

If those are answerable, you can change this codebase safely.
