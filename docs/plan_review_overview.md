# Implementation Plan: Historic Runs Overview & Detailed Review View

This plan details the changes required to display an overview of all historic screening runs on the **Review** tab and enable a detailed candidate evaluation view upon selecting any run.

---

## 0a. Domain Model (`screener/models.py`) — prerequisite, currently missing
* `Run` has **no `created_at` field**, even though the `runs` table stores it (`runs_store.create()` inserts it) and nothing maps it back out in `runs_store._to_record()`. §4A wants a "Created At" column, and `list_all`'s `ORDER BY created_at DESC` sorts correctly at the SQL layer but there is nothing to display without this.
* Add **`created_at: datetime`** to `Run`. Low-risk: `Run(...)` is constructed in exactly one place, `runs_store._to_record()`, so nothing else breaks.
* Map it in `runs_store._to_record` — the row already has the column, just add `created_at=row["created_at"]`.

---

## 0. Storage Layer (`screener/storage/runs_store.py`)
* Add **`list_all(tx: Tx) -> list[Run]`**:
  `SELECT * FROM runs ORDER BY created_at DESC`.
  Returns all runs sorted by creation date descending so the most recent run appears first.

---

## 1. Backend Service Layer (`screener/service.py`)
* Add **`list_runs(self) -> list[Run]`**:
  Thin wrapper around `runs_store.list_all`. Returns a list of all historic runs across all positions.

---

## 2. API Control Plane (`screener/api/routes/runs.py`)
* Add **`GET /runs`** (`response_model=list[RunResponse]`):
  Returns a list of all screening runs.
* **`RunResponse` is currently missing fields this view needs.** Checked `screener/schemas.py`: `RunResponse` only has `id, position_id, rubric_id, folder, status, judge_digest, prompt_hash, app_version` — no `file_count`, `escalation_rate`, or (until §0a) `created_at`, even though `Run` (the domain model) already carries `file_count` and `escalation_rate`. "Matches existing response serialization standards used in `GET /positions`" undersells this: `PositionResponse` already had everything `list_positions` needed; `RunResponse` does not, for this view. Add `created_at`, `file_count`, and `escalation_rate` to `RunResponse`, and map all three in `to_run()` (`screener/api/deps.py`) — currently `to_run()` builds only the eight fields above.

---

## 3. HTTP API Client (`ui/api_client.py`)
* Add **`list_runs(self) -> list[dict[str, Any]]`**:
  Executes `GET /runs` and returns the decoded JSON list of run objects.

---

## 4. Streamlit UI Overhaul (`ui/screener_app.py`)

### **A. Historic Runs Overview Section**
* At the top of `review_page()`, call `client.list_runs()` to fetch all historic runs.
* If no runs exist, display an informative banner:
  > ℹ️ *No screening runs found yet. Create and start a run on the Runs tab.*
* Also call `client.list_positions()` and join on `position_id` — needed for §B's dropdown label (see below) and to keep the table's position column human-readable rather than a raw id.
* Render a summary table (`st.dataframe`) listing all historic runs with key columns:
  * `Run ID`, `Position` (reference — title, from the join above, not the raw `position_id`), `Status`, `Created At`, `File Count`, `Escalation Rate`.

### **B. Run Selector & Detailed View**
* Add a dropdown (`st.selectbox`) allowing the user to select any historic run:
  ```text
  Select Run to Review: [ run-a1b2c3d4 — Position: REQ-1 (Completed, 12 files) ▼ ]
  ```
  `REQ-1` here is the position's `reference` from the `list_positions()` join above — the original draft's table showed the raw `position_id` while this mockup showed the reference; use the reference in both places, since a raw id isn't useful to a reviewer scanning a list of runs.
* Default the dropdown selection to `st.session_state.get("run_id")` if set, or the most recent run in the list.
* Update `st.session_state["run_id"]` to match the selected run. This dropdown is a friendlier picker over the same shared session state the sidebar's "Run id" field already owns (see the design comment in `sidebar()` explaining why run selection is consolidated there) — it is not a second, competing source of truth, just another way to set the same value.
* **The candidate evaluation detail view below already exists — reuse it as-is, don't rebuild it.** `review_page()` currently starts with:
  ```python
  run_id = st.session_state.get("run_id", "")
  if not run_id:
      st.info("Enter a run id in the sidebar.")
      return
  ```
  followed by working code for the escalation meter, needs-review/meets-must-haves/missing-must-haves partitions, and sign-off section, all driven purely by `run_id`. The only change here is replacing that early-return guard with the overview table + dropdown from §A/§B, which sets `run_id` and falls through into the existing, unmodified code beneath it. Do not duplicate `escalation_meter`, `partition`, or `sign_off_section` under new names.

---

## 5. Verification & Testing
* Add unit test in `tests/test_service.py` verifying `service.list_runs()` returns runs ordered by creation timestamp descending.
* Add API route test in `tests/test_api.py` asserting `GET /runs` returns `200` with the array of `RunResponse` objects, **and explicitly asserts `created_at`, `file_count`, and `escalation_rate` are present in the response** — these are the fields most likely to be silently dropped if `to_run()` is extended by copying its existing eight fields rather than deliberately adding the three new ones.
* Run full test suite (`pytest`) to confirm 100% pass rate and coverage.

---

## Note on scale
`GET /runs` has no limit/pagination — fine for this POC's current volume, but worth knowing: 13 test runs accumulated in one afternoon during earlier testing of the Runs tab. Not a blocker, just don't be surprised when the table grows past what fits on screen.
