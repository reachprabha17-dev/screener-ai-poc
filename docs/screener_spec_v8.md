# Screener spec v8 — job descriptions as documents

**Status:** amendment to `screener_spec_v6.md`, alongside `screener_spec_v7.md`.
v6 remains the specification for everything not listed here; v7 remains the
specification for the reviewer interface. Where this document and v6 disagree on
the clauses named below, v8 is correct.

**Scope of v8:** a requisition's job description may now be **uploaded as a PDF
or DOCX** and read by the same parser résumés go through, instead of only being
pasted as text. Nothing about rubric extraction, approval, screening, scoring,
verification or ranking changes — this document ends where `jd_text` is
produced, and `jd_text` is the same field it always was.

Three things follow from that and are specified here: a locked decision about
who checks the extraction, an amendment to §4's layering rule, and an amendment
to §8.4's sandbox mechanism.

---

## 1. Amended and new locked decisions

| # | v6/v7 | v8 | Why |
|---|---|---|---|
| 15 | Screening never runs in a request lifecycle. **No `BackgroundTasks`** | **Unchanged, and deliberately re-stated.** Screening never runs in a request. *Extracting text from one uploaded document* is not screening, and is permitted — one document, synchronously, while a human waits | The rule protects against a 1,000-CV batch inside an HTTP request: work that must survive a restart, be resumable, and reclaim orphans. A single JD parse has none of those properties. It is the same shape as `extract_rubric` (§9.1), which has always been a synchronous LLM call in the service layer |
| 18 | Extraction via **xberg**, **sandboxed in a subprocess** (§8.4) | Unchanged in substance. The subprocess is now launched via an exec'd shim rather than `preexec_fn`, so the sandbox is safe to call from a multi-threaded parent | `preexec_fn` runs Python between `fork` and `exec`, where only async-signal-safe work is legitimate. It was survivable while the only caller was the single-threaded worker; the API serves `def` handlers from a threadpool. See §4 below |
| 33 *(new)* | — | **A job description extracted from a document is shown to a human, in an editable field, before it becomes a requisition.** The text they submit is the text that is stored | Extraction is lossy in ways that are invisible downstream: a two-column layout interleaves, OCR drops a "not". The rubric drafted from that text is what screens people out, and §9.1's approval gate **cannot** catch it — the reviewer has nothing to compare the criteria against. This is that same control applied one step earlier, and it is the reason the flow is two requests rather than one |
| 34 *(new)* | — | **`service.py` reaches the document parser through `ports.DocumentExtractor`.** It still imports no `screener.intake` module | §6's ports are the portability mechanism, and this is one more use of it. The service layer runs inside the API process; keeping the concrete parser out of its import graph is worth more than the convenience of a direct call |
| 35 *(new)* | — | **Which intake methods are offered is deployment configuration (`jd_intake_mode`), enforced by the server and published to the interface at `GET /config`** | A control that only hides a button is not a control. The setting is enforced in `create_position` and in the upload endpoint independently of what any client renders |

Decisions 1–14, 16–17, 19–32 stand unchanged.

---

## 2. The ingestion path

```
  Browser
    │  1. POST /jd-documents   (multipart, one file)
    ▼
  FastAPI ──► service.extract_jd_document()
                 │   ports.DocumentExtractor  (wired at the composition root)
                 │      ├── validate_file()    §8.2  magic bytes, size, pages, zip bomb
                 │      ├── sandboxed parse    §8.4  xberg, separate rlimited process
                 │      └── sanitize()         §8.6  NFKC, bidi, zero-width
                 │   detect_injection()        §10.2 advisory only — never blocks
                 ▼
              { text, filename, file_sha256, page_count, ocr_used,
                chars_stripped, warnings, injection_signals, parser_version }
    │
    │  2. the reviewer reads the text and corrects it        ← decision #33
    │
    │  3. POST /positions   (unchanged JSON contract)
    ▼
    { reference, title, jd_text, jd_source, jd_filename, jd_file_sha256, jd_ocr_used }
```

**`POST /jd-documents` creates nothing** — no row, no stored file, no id. It is a
pure document→text transform. An abandoned upload therefore leaves nothing to
clean up and the endpoint needs no ownership model, which is what makes it safe
to expose without one.

**Upload and paste converge on `create_position`.** There is one path into
`positions.jd_text`, and the text on it has always been read and accepted by a
person.

### Caps (§7)

Deliberately tighter than the résumé path, because unlike a résumé this parse
holds an API threadpool thread for its duration and that is the resource being
protected:

| | résumé | job description |
|---|---|---|
| bytes | `max_file_bytes` 5 MB | `jd_max_file_bytes` 2 MB |
| pages | `max_pages` 50 | `jd_max_pages` 10 |
| parse timeout | `parse_timeout_s` 60 s | `jd_parse_timeout_s` 15 s |
| concurrency | one worker, one at a time | `jd_max_concurrent_parses` 2, acquired **non-blocking** → 503 |

The semaphore is non-blocking on purpose: waiting on it would hold the very
threadpool thread the limit exists to protect, converting a memory ceiling into
an availability problem of the same size.

**The body cap is ASGI middleware, ahead of the app.** Starlette parses the
multipart body during dependency resolution and spools each file part to disk
past 1 MB with no ceiling of its own, so by the time any handler runs an
unbounded upload is already on the filesystem. `Content-Length` alone is not
enough — it is absent under `Transfer-Encoding: chunked` — so the byte count is
also enforced as the body is read.

**The page cap is enforced twice, and must be.** `validate_file` screens what it
can see without parsing, and §8.2 is explicit that on a modern PDF whose page
tree lives in a compressed object stream that is nothing at all. The count is
only knowable after the parse, so `extract_jd_document` re-checks it. A cap
enforced only in the pre-filter looks like it works until somebody uploads one
of those.

---

## 3. Amended §4 — layering

v6's block read:

```
api/         → service.py, schemas.py, models.py
service.py   → storage/*, models.py          (NOT pipeline.py, NOT core/)
```

and `tests/test_layering.py` additionally forbade `screener.intake` in both.
That single ban was doing two jobs which are now separated:

```
api/          → service.py, schemas.py, models.py
                NEVER screener.pipeline — no exceptions, including deps.py
                screener.intake ONLY in api/deps.py, the composition root
service.py    → storage/*, ports.py, core/*, models.py
                NEVER screener.pipeline, NEVER screener.intake
                the document parser is reached through ports.DocumentExtractor
```

**Why the intake ban narrowed.** It existed to keep hostile bytes out of the
process holding the database credentials. That concern is real and it is not
this ban that addresses it — §8.4's sandbox does, by running every parse in a
separate process. Banning the import as well did the sandbox's job a second time,
and the collateral was a legitimate use: reading one job description while a
human waits. `api/deps.py` already constructs `OllamaClient`; naming one more
concrete class there is what a composition root is for.

**Why the pipeline ban did not.** It is about *work* — orphan reclaim,
resumption, surviving a restart — and none of that is affected by anything here.
It stays absolute.

Both halves are enforced: one test asserts nothing under `api/` imports the
pipeline and only `deps.py` imports intake; a second asserts `deps.py` really is
where the extractor is wired, so a narrowed rule cannot quietly become a deleted
one.

---

## 4. Amended §8.4 — how the sandbox applies its limits

The controls are unchanged: `RLIMIT_DATA`, `RLIMIT_AS`, `RLIMIT_CPU`,
`RLIMIT_NPROC`, `RLIMIT_FSIZE`, `RLIMIT_NOFILE`, `RLIMIT_CORE`, a scrubbed
environment, a per-job scratch directory, and the parent's wall clock. **What
changed is where they are applied.**

- **v6:** a `preexec_fn` set them between `fork` and `exec`.
- **v8:** the parent computes them as data and execs `screener/intake/rlimit_shim.py`,
  which applies them to itself and then `execv`s the parse worker. `setsid` moves
  to `subprocess`'s `start_new_session=True`, performed in C.

rlimits and the session id survive `execve`, so the parser is still bounded
before it reads a byte. Nothing is weakened; the only code running unlimited is
the shim's own interpreter startup, which touches no input.

**Why:** `preexec_fn` in a multi-threaded parent is a latent deadlock — the child
inherits a snapshot of locks other threads may have been holding at the instant
of the fork. The symptom is a parse that hangs under load, not one that errors.
This was survivable while the only caller was the single-threaded worker (§16.1);
it is not, now that the API parses.

**Also fixed here:** the timeout path killed one pid, so the parser's own
children outlived it holding the stdout pipe. It now kills the process group,
which `start_new_session` makes safe as well as necessary.

### What this sandbox does and does not contain — measured

v6 labelled this the *minimum acceptable* tier and warned it bounds the blast
radius of a crash but not of a takeover. That warning was measured against a
probe run as the parser, and it is accurate:

**Contained:** memory exhaustion, CPU spin, fork bombs, descriptor exhaustion,
oversized writes, core dumps of candidate text, segfaults, hangs, environment
inheritance, and anything written relative to the working directory.

**Not contained:** the parser runs as the service account with an unrestricted
filesystem view and unrestricted network. Measured, it could read the project
`.env` (which contains `DB_URL` and its password), open TCP connections to
`127.0.0.1:5432` and to the public internet, resolve DNS, spawn `/bin/sh`, and
enumerate every process on the host. **Scrubbing the environment does not protect
the database credentials, because the child can read the file that holds them.**

**v8 raises the priority of closing this**, because it widens the entry point:
before, only files an operator placed in the résumé share were parsed; now anyone
who can reach the interface can feed the parser a document over HTTP. §8.4's
tier-1 and tier-2 remedies are unchanged and still correct — a container
(`--network none --read-only --cap-drop ALL --pids-limit`) or a dedicated
unprivileged uid. Blocking network is the single highest-value control: it turns
parser RCE from "compromise and exfiltrate" into "crash a subprocess we already
expect to crash."

---

## 5. Contracts

**`ports.py`** — `DocumentExtractor` is new; `ResumeParser.parse` gains
`timeout_s`, because the worker and the API bound very different things with it:

```python
class DocumentExtractor(Protocol):
    def extract(
        self,
        data: bytes,
        *,
        filename: str,
        timeout_s: int | None = None,
        max_bytes: int | None = None,
        max_pages: int | None = None,
    ) -> ParseResult: ...
```

It takes **bytes, not a path**, so that no caller above it has to invent a safe
filename for hostile content. The implementation generates the stem and takes
only the *suffix* from the upload — deliberately, because §8.2 rejects an
extension that disagrees with the sniffed container type, and naming the file
after what was sniffed would make that check validate its own sniff.

**New files**, amending §4's tree:

```
screener/
├── intake/
│   ├── document_text.py     8.2 → 8.4 → 8.6 composed; satisfies DocumentExtractor
│   └── rlimit_shim.py       applies the rlimits, then execs the parse worker (§4)
└── api/routes/config.py     GET /config
storage/migrations/postgres/0003.jd-provenance.{sql,rollback.sql}
web/src/
├── components/JdInput.tsx   upload or paste, and the correction step (#33)
├── ui/FileInput.tsx         one file, by click or drop
└── lib/jd.ts                a job description plus where it came from
```

**`models.py`** — `JdExtraction` (not persisted; the answer to one request), and
`Position` gains `jd_source`, `jd_filename`, `jd_file_sha256`, `jd_ocr_used`.

**Provenance is a record of origin, not a claim about `jd_text`.** The reviewer
edits the extracted text before submitting it, by design (decision #33), so
`jd_file_sha256` identifies the *uploaded document* and asserts nothing about the
stored description. Anything stronger would be a guarantee the workflow does not
support.

**Migration `0003.jd-provenance`** — four columns on `positions`. `jd_source` is
`NOT NULL DEFAULT 'paste'`: every row existing when it runs *was* pasted, so the
default is a true statement about history rather than a backfilled guess. The
other three are nullable and read as "not applicable" for a pasted description,
never as "unknown".

**Audit** — `create_position` records `jd_source`, `jd_filename` and `jd_ocr_used`
in its detail. A rubric drafted from OCR'd text was drafted from an
approximation, and that is part of answering for an adverse decision later.

**Failures are sentences, not flags.** `PARSER_TIMEOUT` and its neighbours exist
to make a *candidate* unscoreable and put them in front of a human. There is no
candidate here and nobody is disadvantaged by a job description that would not
parse, so the service raises with a sentence the reviewer can act on. Every
failure is logged with the parser's own error — without that, "could not be read"
is all anyone has to work from, including whoever is asked why.

**Injection findings on a job description are advisory and never block.** §10.2's
patterns were calibrated against résumé prose — "fire on what a résumé cannot
plausibly say" — and a job description says several of those things routinely
("your task is to…"). The correction step of decision #33 is the control here,
and it is stronger than the heuristic; the signals only say where to look. Do not
retune `core/detect_injection.py` for job-description prose: that would degrade
the résumé path, which is the one that was measured.

---

## 6. Unchanged

§8.2 validation, §8.6 sanitization and §10.2 detection are reused as they stand —
this document adds no new parsing, no new sanitizer and no new patterns. §9.1
rubric extraction, its approval gate, and everything downstream of `jd_text` are
untouched. The résumé path through `pipeline.py` is untouched: it composes 8.2,
8.4 and 8.6 itself because it must interleave them with redaction and offset
mapping, and it does not use `DocumentExtractor`.
