# Implementation Plan: Persistable Rubrics & Fixed Runs Creation Flow

This plan details the changes required to ensure rubrics persist across browser reloads and that screening runs can be reliably created from the **Runs** tab.

---

## 0. Storage Layer (`screener/storage/rubrics_store.py`)
* `latest_for_position` already exists and covers the "latest, any status" case — reuse it, do not duplicate it.
* There is **no existing query for "the approved rubric for a position"** — `is_approved(tx, rubric_id)` only checks a rubric whose id you already have. Add **`approved_for_position(tx, position_id) -> Rubric | None`**:
  `SELECT * FROM rubrics WHERE position_id = ? AND approved_at IS NOT NULL ORDER BY version DESC LIMIT 1`.
  Without this, step 1's `get_approved_rubric` has nothing to call.

---

## 1. Backend Service Layer (`screener/service.py`)
* Add **`get_latest_rubric(position_id: str) -> Rubric | None`** — thin wrapper over `rubrics_store.latest_for_position`.
* Add **`get_approved_rubric(position_id: str) -> Rubric | None`** — thin wrapper over the new `rubrics_store.approved_for_position` (§0).
* Neither method raises `NotFoundError` when nothing exists. That's a deliberate departure from this file's other single-resource lookups (e.g. `approve_rubric`, `create_run`, which raise on a missing/invalid id): here, "no rubric yet" and "no approved rubric" are the ordinary state of a freshly created position, not an error condition. Return `None` and let the caller decide what to show.

---

## 2. API Control Plane (`screener/api/routes/positions.py`)
* Add **`GET /positions/{position_id}/rubric`** (`response_model=RubricResponse | None`): returns the latest rubric for the position, or `200` with a `null` body when none exists (used by the Requisitions tab).
* Add **`GET /positions/{position_id}/rubric/approved`** (`response_model=RubricResponse | None`): returns the active approved rubric, or `200` with `null` when none exists (used by the Runs tab).
* **Do not** raise `NotFoundError` from either handler on the missing case — that maps to a `404` via the global handler in `api/app.py`, which is correct for "this id doesn't exist" but wrong here: there is no bad id, just an expected absence. A `404` here would make the UI's generic error path (see §4) fire on every normal visit to a fresh position.
* `response_model=RubricResponse | None` (verified against this repo's FastAPI 0.141.1 / Pydantic 2.13.4) validates and serializes a `None` return as `200` + `null` body — this is what to use. **Do not use `response_model=None`** — that's a different, existing convention in this codebase (`candidates.py`, `runs.py`) that *disables* response validation entirely because the return shape is polymorphic. It is not a "nullable" marker; using it here would silently drop validation on the non-null path too. The handler body is simply `return to_rubric(rubric) if rubric else None` — `to_rubric` itself needs no change.

---

## 3. HTTP API Client (`ui/api_client.py`)
* Add **`get_latest_rubric(position_id: str) -> dict[str, Any] | None`** method — plain `GET`, returns whatever `_request` decodes (`None` for a `null` body, no exception).
* Add **`get_approved_rubric(position_id: str) -> dict[str, Any] | None`** method — same shape.
* Because §2 returns `200`/`null` rather than `404` for the empty case, these two calls will *not* raise `ApiError` when nothing exists — no special-casing needed here or in `call()` (`ui/screener_app.py`). Confirm this while implementing §2; if a `404` route slips in instead, these methods will need to catch it explicitly to keep the "no error banner for absence" behavior.

---

## 4. Streamlit UI Overhaul (`ui/screener_app.py`)

### **A. Requisitions Tab Persistence Fix**
* When the selected position **changes** (i.e. `st.session_state.get("rubric", {}).get("position_id") != position_id` — the existing guard at `rubric_section`'s current "No draft yet" check), call `client.get_latest_rubric(position_id)` before falling back to the "No draft yet" message.
* Store the returned rubric in `st.session_state["rubric"]`.
* Fetch only on that change, not on every Streamlit rerun — `rubric_section` reruns on every widget interaction (editing a cell, moving a slider), and an unconditional fetch would overwrite whatever the reviewer is mid-editing in the `st.data_editor` with whatever is still saved server-side.
* **Result:** Refreshing the browser window (F5) will no longer display "No draft yet" for an existing position—it will immediately reload the rubric from the backend database!

### **B. Runs Tab Workflow Overhaul**
* **Position Selector:** Add a dropdown at the top of the Runs tab allowing the user to select any open position:
  ```text
  Select Position: [ software_engineer — Senior Software Engineer ▼ ]
  ```
* **Auto-Load Approved Rubric:** Once a position is selected, call `client.get_approved_rubric(position_id)`:
  * **If Approved:** Display a success banner showing Version, Rubric Hash, Criteria Count, and Approver Info, and enable the **"Create Run over Folder"** button.
  * **If Unapproved / Missing:** Display an informative warning:
    > ⚠️ *No approved rubric found for this position. Please approve a rubric in the Requisitions tab before creating a run.*

---

## 5. Verification & Testing
* Add unit tests in `tests/test_service.py` for `get_latest_rubric` / `get_approved_rubric`: both return `None` (not raise) for a position with no rubric / no approved rubric, and return the expected `Rubric` otherwise. Follow the existing style, e.g. `test_a_run_cannot_be_created_against_an_unapproved_rubric`.
* Add route tests in `tests/test_api.py` for the two new `GET` endpoints asserting `200` + `null` body on the empty case (not `404`) — this is the behavior most likely to regress if someone "fixes" the routes to match the rest of the file's `NotFoundError` convention.
* Run `pytest` to verify all layer constraints (`tests/test_layering.py`) and route tests pass cleanly.
* Manually verify: create a position, draft+approve a rubric, refresh the browser (F5), confirm the Requisitions tab reloads the rubric and the Runs tab's new position selector shows the approved-rubric success banner without having visited Requisitions first.
