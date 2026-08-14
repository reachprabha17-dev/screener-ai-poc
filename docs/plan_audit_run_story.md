# Implementation Plan: Audit page → "story of a run"

Turn the Audit page from a filtered table of `audit_log` rows into a readable
narrative of how one run happened, end to end.

---

## Context

The Audit page currently renders `ts · actor · action · entity · entity_id · detail`
with filter boxes. That is a database view, not an answer. The question an auditor,
an HR manager, or an employment lawyer actually arrives with is **"show me how this
hiring decision was made and prove a human was involved"** — and answering it today
means filtering the table four times and reconstructing the sequence by eye.

The data already supports the answer. Reconstructed from the live database for a
single completed run:

```
2026-08-10T18:55:04  poc-operator  create_position  {'reference': 'e2e-test-depot-technician'}
2026-08-10T18:55:28  poc-operator  extract_rubric   {'criteria': 5, 'prompt_hash': '4e3034…',
                                                     'judge_digest': '444af1c…'}
2026-08-10T18:55:35  poc-operator  approve_rubric   {'rubric_hash': '6560ce…'}
2026-08-10T18:56:09  poc-operator  create_run       {'folder': '…', 'queued': 2, 'rubric_id': 'rub-c7f6…'}
2026-08-10T18:56:23  (system)      advance_phase    {'phase': 'verify', 'queued': 2}
2026-08-10T18:57:10  (system)      complete_run     {'done': 2, 'failed': 0, 'escalation_rate': 0.5}
2026-08-10T19:09:18  poc-operator  decision         {'decision': 'advance', 'old_score': 9.4,
                                                     'reason': 'Meets all must-haves…'}
2026-08-10T19:09:18  poc-operator  decision         {'decision': 'reject', 'old_score': 0.0,
                                                     'reason': 'No relevant depot maintenance experience…'}
2026-08-10T19:09:18  poc-operator  sign_off_run     {}
```

Every element of a defensible record is there — which model and prompt drafted the
rubric, which human approved it, what was screened, each decision with its stated
reason, who accepted the result. The page throws it away and shows rows.

**`audit_store.list_for_entity(tx, entity, entity_id)` already exists and has no
callers** (grep confirms zero outside its own definition). It is exactly the primitive
this needs.

**One finding worth building for.** In the trace above, `poc-operator` approved the
rubric *and* signed off the run. Separation of duties is *supported* by the system
(`test_a_second_person_can_approve_override_and_sign_off`) but not *required*, so this
is not a rule violation — it is precisely the observation an auditor is hunting for,
and the timeline should state it rather than leave it to be noticed.

---

## Scope

Add a run-scoped narrative view. **Keep the existing searchable log** — it answers a
different question ("what did this person do across everything") and is already built.
The story becomes the default view; the raw log moves behind a tab or expander.

Out of scope, deliberately: the per-candidate adverse-action one-pager and the
automated exception dashboard. Both are worth doing and both are larger; this lands
the spine they would hang from.

---

## 1. Storage — nothing new

`audit_store.list_for_entity` is sufficient. Do **not** add a bespoke join: the entity
chain is assembled in the service layer where the cross-store orchestration belongs,
and `service.py`'s docstring is explicit that a method earning its place must add "a
transaction boundary, an audit row, orchestration across stores, or a rule."

---

## 2. Service — `run_story(run_id)`

```python
def run_story(self, run_id: str) -> RunStory
```

Real orchestration across four stores in one transaction:

1. `runs_store.get(run_id)` → `position_id`, `rubric_id`, plus the reproducibility
   fields already frozen on the row (`judge_digest`, `prompt_hash`, `app_version`).
   `NotFoundError` if absent.
2. `audit_store.list_for_entity(tx, "position", run.position_id)`
3. `audit_store.list_for_entity(tx, "rubric", run.rubric_id)`
4. `audit_store.list_for_entity(tx, "run", run_id)`
5. `results_store.list_for_run(tx, run_id)` → candidate ids, then
   `list_for_entity(tx, "candidate", str(c.id))` for each.
   **`entity_id` for a candidate is the integer id rendered as text** (verified:
   rows carry `'1'`, `'2'`), so the `str()` is required, not cosmetic.

Merge, sort by `ts` ascending, return as `RunStory`:

```python
class RunStory(BaseModel):
    run_id: str
    position_reference: str
    rubric_version: int | None
    events: list[AuditEntry]
    approved_by: str | None  # from the rubric row
    signed_off_by: str | None  # from the run row
    separation_of_duties: bool  # approved_by != signed_off_by, both present
```

`separation_of_duties` is computed here, not in the UI: it is a statement about the
record, and a second implementation in a template is a second thing to keep true.

**Reads only — this method must not write an audit row.** Reading the log is not a
mutation, and self-logging reads would bury the mutations an auditor came for.

**One N+1 caveat worth bounding.** Step 5 is one query per candidate. At 1,000
candidates that is 1,000 queries against a local SQLite file — acceptable, but cap it:
take candidate events for at most the first 200 candidates and set a
`candidate_events_truncated: bool`, so a 1,000-CV run renders rather than stalls. The
UI says so plainly when it fires. If this ever needs to be exact, the fix is a single
`WHERE entity='candidate' AND entity_id IN (…)` query, not a bigger cap.

---

## 3. API — `GET /runs/{run_id}/story`

Lives in `routes/runs.py`, beside the other run reads, not in `routes/audit.py` — it is
a view of a run, and the URL should say so.

- `def`, not `async def` (`test_no_handler_is_async`).
- **Requires the auditor role**, same as `GET /audit`. It exposes decision reasons and
  actor identities; the gate must not depend on which URL the data arrives through.
- Read-only, so it stays out of `MUTATING_ROUTES` (`tests/test_api.py:544`).
- Response `RunStoryResponse` in `schemas.py`, `ConfigDict(extra="forbid")`, reusing
  `AuditEntryResponse` for `events`. Mapper `to_run_story` in `api/deps.py`.

Client: `ApiClient.run_story(run_id)`. Note `_headers()` now sends `X-Actor-Roles` when
the operator has selected roles, so the auditor gate is reachable from the UI.

---

## 4. UI — `ui/views/audit.py`

Two views on the page. Default to the story; keep the search behind a second tab.

**Selector:** reuse the run picker shape from the Review page — a `st.selectbox` over
`client.list_runs()` formatted as `run-id · Position · Status · date`. Do **not** write
`st.session_state["run_id"]` from a comparison; if this selector should drive the shared
run, use the existing `adopt_run` `on_change` callback. A third selector added with the
compare-and-rerun pattern would reintroduce the loop that cost 1,207 script passes.

**Header band** — the reproducibility record, stated once:
> Depot Technician · rubric v3 (`6560ce…`) · judge `granite4.1:8b` (`444af1c…`) ·
> prompt `4e3034…` · app 0.1.0-step20

**Separation of duties**, stated rather than left to be noticed:
> ✅ Rubric approved by *priya*; run signed off by *omar*.
> ⚠️ Rubric approved and run signed off by the same person (*poc-operator*). Permitted,
> but a second reviewer is the stronger record.

**Timeline** — one line per event, chronological, rendered as sentences not JSON:

| event | rendered as |
|---|---|
| `create_position` | *Priya raised requisition `e2e-test-depot-technician`.* |
| `extract_rubric` | *Model drafted 5 criteria (prompt `4e3034…`, model `444af1c…`).* |
| `save_rubric` | *Priya edited the rubric → version 2 (`6560ce…`).* |
| `approve_rubric` | *Priya approved rubric `6560ce…`.* ← mark as the human gate |
| `create_run` | *Priya created a run over `…/depot-technician` — 2 files queued.* |
| `start_run` / `advance_phase` / `complete_run` | *System: screening complete — 2 done, 0 failed, 50% escalated.* |
| `decision` | *Priya **rejected** ahmed.docx (was scored 0.0): "No relevant depot maintenance experience…"* |
| `sign_off_run` | *Priya signed off the run.* ← mark as the human gate |
| `rescan_run` / `abort_run` | *Priya aborted the run — 4 jobs released.* |

Implement as a `dict[str, Callable[[dict], str]]` keyed on action, with a fallback that
renders the raw action and detail for any action not yet templated — a new audited
action must degrade to the current behaviour, never to a `KeyError`.

**Distinguish system from human.** `advance_phase` and `complete_run` have
`actor_id IS NULL` (verified: 8 such rows). Render them with a distinct marker and the
word *System* — "no human did this" is a fact the record is making deliberately, and
flattening it to a blank actor column loses it.

**Decisions are the payload.** Give them visual weight and always show the reason in
full; that string is the adverse-action justification and truncating it defeats the point.

---

## 5. Verification

**Tests**
- `tests/test_service.py`: `run_story` assembles events from all four entity types in
  timestamp order; `separation_of_duties` is `False` when one actor approved and signed
  off and `True` when two did; unknown run → `NotFoundError`; **writes no audit row**
  (assert the log row count is unchanged across the call).
- `tests/test_api.py`: `GET /runs/{id}/story` returns 200 for an auditor and **403
  without the role**; `MUTATING_ROUTES` unchanged.
- `tests/test_ui.py`: the renderer produces a sentence for every action currently in
  `audit_log` — enumerate the twelve known actions — and falls back cleanly for an
  invented one. This is the test that stops a new audited action rendering as a crash.
- A NULL-`detail` event must render (regression: a required `detail` dict already caused
  a 500 on 15 of 90 real rows).

**Gates:** `bash scripts/dev.sh check`.

**Manual:** open the Audit page, pick `run-b21f65e485a7`, and confirm the timeline reads
as the narrative above — including the same-person sign-off warning, which that run
genuinely exhibits.

---

## Note on existing data

~22 rows with actions like `action_inject` and `action_c_<uuid>` are permanently in
`data/screener.db`, left by an unisolated test run before the fixtures were fixed.
`audit_log` is append-only by database trigger, so they cannot be removed. The
fallback renderer will show them as raw events; that is correct behaviour and worth
seeing rather than hiding.
