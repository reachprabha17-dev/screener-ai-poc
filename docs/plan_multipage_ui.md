# Implementation Plan: Multi-page UI + Audit page

Split `ui/screener_app.py` into real Streamlit pages, revamp the sidebar, and add an
Audit page over the existing `audit_log`.

---

## Context

`ui/screener_app.py` is **1,059 lines and 24 top-level functions** in one file, with the
three sections rendered as `st.tabs`. Two problems follow from that shape:

**1. Tabs execute every body on every render.** This is not a style complaint — it caused
a live bug. When the Runs tab gained a run selector alongside the Review tab's, both wrote
`st.session_state["run_id"]` and each undid the other: measured at **1,207 script passes
in 150 s, 119% CPU, the API hit on every pass**, with the browser stuck on *Running…*. It
was fixed with `on_change` callbacks, but the *structure* that allowed it remains — any
future pair of widgets sharing state across tabs can do it again.

`st.navigation` removes the class of bug rather than the instance. Verified against
Streamlit 1.60.0: with two pages registered, one render executes **only the active page's
body**. Cross-tab state fights become impossible because the other page's code does not run.

**2. Everything is in one module.** Sidebar, folder browser, requisitions, rubric editor,
runs, review, candidate detail, CSV export. Any change touches the same file.

There is also a real gap the Audit page closes: `audit_log` is written on every mutation
and enforced append-only by database triggers, but **nothing reads it back**. `audit_store`
has `recent()` and `list_for_entity()`; there is no service method and no API endpoint. The
compliance record the system is built around is currently invisible to the people it exists
for.

---

## 0. Verified constraints — read before designing

- **Streamlit 1.60.0 supports `st.navigation` / `st.Page`.** Confirmed present.
- **Only the active page runs.** Confirmed by test; this is the load-bearing claim.
- **Layering tests cover new files for free.** `package_imports("ui")` in
  `tests/test_layering.py:68-78` uses `rglob("*.py")`, so anything added under `ui/`
  is already asserted not to import `sqlite3`, `screener.*`, or `config`. **Do not
  relax these.** New page modules talk to `ApiClient` and nothing else.
- **Do not name the directory `ui/pages/`.** That is Streamlit's magic auto-discovery
  convention and it interacts confusingly with explicit `st.navigation`. Use **`ui/views/`**.
- **`sys.path` fragility.** `screener_app.py:40-42` does
  `sys.path.insert(0, str(Path(__file__).resolve().parent))` then `from api_client import …`.
  A module at `ui/views/runs.py` has a different `__file__` parent, so a copied import
  breaks. **Register pages as callables, not file paths** — the entrypoint sets `sys.path`
  once and imports the view functions normally, so each view is an ordinary module import
  with no path games.

---

## 1. Target layout

```
ui/
  screener_app.py      entrypoint: sys.path, sidebar, st.navigation, pg.run()
  api_client.py        unchanged except the new audit method (§4)
  common.py            get_client, call, adopt_run, FLAG_HELP, BAND_HELP, PAGE_SIZE
  views/
    __init__.py
    requisitions.py    positions_page, folder_browser, rubric_section
    runs.py            runs_page, controls, live_status, failed_files, escalation_meter
    review.py          review_page, partition, candidate_detail, criterion_block,
                       decision_form, bulk_decision_form, sign_off_section, highlighted,
                       escalation_breakdown, _to_csv
    audit.py           audit_page  (new)
```

`common.py` exists because `call()` and `get_client()` are used by every view and must not
be duplicated — `call()` is the single place `ApiError` becomes a sentence instead of a
traceback (`screener_app.py:104-114`), and a second copy is a second thing to keep correct.

**Move code, do not rewrite it.** Everything except the sidebar and the new Audit page is a
cut-and-paste plus imports. Docstrings carry decisions that took measurements to arrive at
(the escalation-rate-on-screen rationale, the band-not-score rationale, the
`partition(section=…)` key collision) — they move with their functions, unedited.

---

## 2. Entrypoint and sidebar revamp

`ui/screener_app.py` becomes small:

```python
def main() -> None:
    st.set_page_config(page_title="Screener", page_icon="📄", layout="wide")
    sidebar()
    pg = st.navigation(
        [
            st.Page(requisitions.positions_page, title="Requisitions", icon="📋", default=True),
            st.Page(runs.runs_page, title="Runs", icon="⚙️"),
            st.Page(review.review_page, title="Review", icon="✅"),
            st.Page(audit.audit_page, title="Audit", icon="🔎"),
        ]
    )
    pg.run()
```

**Sidebar changes:**

- **Keep** the health block (model / schema / disk, digest-pin warning) and "Reviewing as" —
  the identity reaches the audit log, and the two-person flow depends on being able to change it.
- **Keep `Run id` in the sidebar.** The comment at `screener_app.py:154-160` explains why it
  lives there: it is the context every page works within, not a field belonging to one of them.
  With pages, the original element-id collision it also solved can no longer occur — but the
  "shared context" reason stands on its own, and Runs/Review both still read it.
- **Add** the current run's status as a caption under it (e.g. `run-53a2 · Running · 12 files`)
  so the shared context is legible from any page rather than only on the page that set it.
- **Move** the `API` URL input into an expander (`⚙ Connection`). It is an operator control,
  not a per-session one, and it currently sits above the identity field it is less important than.
- `st.navigation` renders page links in the sidebar automatically, above whatever `sidebar()`
  writes. No manual radio/selectbox.

---

## 3. Audit page — the new work

### 3.1 Storage (`screener/storage/audit_store.py`)

`recent(limit)` is not enough: an auditor asks "what did *this person* do", "everything on
*this run*", "what happened *that week*". Add one function rather than four:

```python
def search(tx, *, actor_id="", action="", entity="", entity_id="",
           since="", until="", offset=0, limit=50) -> tuple[list[dict], int]:
```

Build the `WHERE` from supplied filters only, parameterised — **never f-string the values**
(`jobs_store.claim_next` has a `# noqa: S608` precisely because its fragments are
module-local constants and not caller input; this one takes user input and must not follow
that shape). Return the page plus the total, so the UI can paginate — the same lesson as the
folder picker, where an unbounded listing was the defect.

Keep `recent()` and `list_for_entity()`; they have callers and tests.

### 3.2 Service (`screener/service.py`)

```python
def search_audit(self, **filters) -> tuple[list[AuditEntry], int]
```

Read-only, one transaction. **This method must not write an audit row** — reading the log is
not a mutation, and self-logging reads would bury the mutations an auditor is looking for
(the same concern stated at `service.py:740-743` about a thousand claim rows burying overrides).

Add an `AuditEntry` domain model in `models.py` (`ts: datetime`, `actor_id: str | None`,
`action: str`, `entity: str | None`, `entity_id: str | None`, `detail: dict[str, Any]`).

### 3.3 API (`screener/api/routes/`)

New router `audit.py`, registered in `screener/api/app.py`:

```python
@router.get("/audit", response_model=AuditPageResponse)
def search_audit(actor_id: str = "", action: str = "", entity: str = "",
                 entity_id: str = "", since: str = "", until: str = "",
                 offset: int = 0, limit: int = 50,
                 actor: Actor = Depends(get_actor),
                 service: ScreenerService = Depends(get_service)) -> AuditPageResponse:
```

- `def`, not `async def` — `test_no_handler_is_async` enforces it.
- **Read-only, so it stays out of `MUTATING_ROUTES`** (`tests/test_api.py:544`). That tripwire
  asserts `live == MUTATING_ROUTES`; adding the route there would fail it.
- **Take `actor` anyway and gate on the auditor role.** The audit log names who did what and
  carries `detail_json` (folder paths, rubric ids, decision reasons) — it is the most
  sensitive read surface in the system. `AUDITOR_ROLE` already exists (`schemas.py:241`) and
  `candidate_response` already gates on it (`schemas.py:373`); follow that precedent and
  return 403 otherwise. Note this is the **first GET in the codebase to require a role** —
  say so in the docstring so the next reader does not think it was copied by accident.
- Cap `limit` at 200 server-side, as `/positions/folders` caps at 100.

Response models in `schemas.py`: `AuditEntryResponse` + `AuditPageResponse{entries, total}`,
both `ConfigDict(extra="forbid")`. Mapper `to_audit_entry` in `api/deps.py` beside the others.

### 3.4 Client (`ui/api_client.py`)

```python
def search_audit(self, **filters) -> dict[str, Any]:
    return dict(self._request("GET", f"/audit?{urlencode(filters)}"))
```

Use `urlencode`, not manual `quote` concatenation — there are eight parameters and hand-built
query strings are where quoting bugs live.

### 3.5 View (`ui/views/audit.py`)

- Filter row: actor, action (selectbox of the twelve known actions), entity type, free-text
  entity id, and a date range.
- `st.dataframe` of `ts · actor · action · entity · entity_id`, newest first.
- Expander per row (or a detail panel) rendering `detail` as JSON — that is where
  `rubric_hash`, `folder`, `queued`, decision reasons live.
- Pagination identical to the folder picker: `‹ Prev` / `Next ›` with `1–50 of 214`.
- A caption stating the log is **append-only, enforced by database triggers**
  (`0001.initial-schema.sql:178-182`). That is the property that makes it evidence, and it
  should be visible on the screen an auditor uses.
- Non-auditor actors get a clear "requires the auditor role" message, not a raw 403.

**Do not add an export button in this pass.** A CSV of the audit log is a plausible next step
and a data-egress decision worth taking deliberately, not as a UI convenience.

---

## 4. What must not change

- **`ui/` imports nothing from `screener` or `config`.** Four new files, same rule.
- **`partition(..., section=…)` keys.** The `section` argument exists because
  `run_id + unranked + len(candidates)` collided when two groups had equal counts and crashed
  the page with `StreamlitDuplicateElementKey`. Moving the function must not drop it.
- **`adopt_run` stays an `on_change` callback.** With pages the loop can no longer occur, but
  the callback is also *correct* — it fires on user action rather than on every render. Keep
  it, and keep the docstring explaining why the button paths still use `pending_run_id`.
- **No behaviour changes bundled in.** This is a structural refactor plus one new page. Fixing
  something else in passing makes the diff unreviewable.

---

## 5. Verification

**Tests**
- `tests/test_layering.py` — must pass unchanged. This is the gate that proves the split did
  not smuggle a `screener.*` import into a view. Run it explicitly.
- `tests/test_api.py` — `GET /audit` returns filtered, paginated results; `limit` is capped;
  a non-auditor gets 403; **`MUTATING_ROUTES` is unchanged** (assert by running
  `test_every_mutating_route_is_declared_here`).
- `tests/test_service.py` — `search_audit` filters by actor/action/entity and **writes no
  audit row of its own** (assert the row count is unchanged across a read).
- New `tests/test_audit_store.py` — filter combinations, ordering (newest first), pagination
  boundaries, and that a quote/`%`/`_` in a filter value cannot alter the query.

**Gates:** `bash scripts/dev.sh check` — ruff format, ruff, mypy, pytest at 85% coverage.

**Manual, and this is the one that matters:** the loop that motivated the split cannot be
reproduced by a unit test. After the refactor, open Runs, select a run for position A; open
Review, select a run belonging to position B; return to Runs. Confirm the page settles
immediately and the API log shows no repeated `GET /positions` burst while idle
(`grep -c "GET /positions " data/logs/dev-api.log` twice, five seconds apart, should not move).

**Sequencing:** do the mechanical split first and confirm all four gates plus the manual check
pass with **no** new features. Then add the Audit page. A refactor and a feature in one diff
means a regression cannot be attributed to either.
