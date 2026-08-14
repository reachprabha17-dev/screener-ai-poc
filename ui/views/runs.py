from typing import Any

import pandas as pd
import streamlit as st

from ui.api_client import ApiClient
from ui.common import _humanize, adopt_run, call, escalation_meter, get_client


def runs_page() -> None:
    st.header("Runs")
    client = get_client()

    positions = call(client.list_positions) or []
    if not positions:
        st.info("No open requisitions yet.")
        return

    current_pos_id = st.session_state.get("position_id")
    default_index = 0
    if current_pos_id:
        for idx, p in enumerate(positions):
            if p["id"] == current_pos_id:
                default_index = idx
                break

    chosen_pos_id = st.selectbox(
        "Position for run",
        options=[p["id"] for p in positions],
        index=default_index,
        format_func=lambda pid: next(
            (f"{p['reference']} — {p['title']}" for p in positions if p["id"] == pid),
            pid,
        ),
        key="runs_position_select",
    )
    st.session_state["position_id"] = chosen_pos_id

    approved_rubric = call(client.get_approved_rubric, chosen_pos_id)
    if approved_rubric:
        r_ver = approved_rubric["version"]
        r_hash = approved_rubric["rubric_hash"][:12]
        r_crit = len(approved_rubric["criteria"])
        r_by = approved_rubric["approved_by"]
        st.success(
            f"Approved Rubric v{r_ver} (`{r_hash}`) · {r_crit} criteria · Approved by {r_by}"
        )

        if st.button("Create a run over the folder"):
            run = call(client.create_run, chosen_pos_id, approved_rubric["id"])
            if run:
                # Direct now that no sidebar widget is bound to `run_id`.
                st.session_state["run_id"] = run["id"]
                st.success(f"Snapshotted the folder into run {run['id']}")
                st.rerun()

    else:
        st.info(
            "No approved rubric found for this position. "
            "Please approve a rubric on the Requisitions tab before creating a run."
        )

    all_runs = call(client.list_runs) or []
    pos_runs = [r for r in all_runs if r["position_id"] == chosen_pos_id]

    if pos_runs:
        current_run_id = st.session_state.get("run_id", "")
        default_run_idx = 0
        if current_run_id:
            for idx, r in enumerate(pos_runs):
                if r["id"] == current_run_id:
                    default_run_idx = idx
                    break

        def _format_pos_run(rid: str) -> str:
            r = next((run for run in pos_runs if run["id"] == rid), None)
            if not r:
                return rid
            status = r["status"].capitalize()
            files = r.get("file_count", 0)
            created = r.get("created_at")
            if isinstance(created, str):
                created_str = created[:10] if created else ""
            elif hasattr(created, "strftime"):
                created_str = created.strftime("%Y-%m-%d")
            else:
                created_str = ""
            date_part = f" ({created_str})" if created_str else ""
            return f"{r['id']} · {status} · {files} files{date_part}"

        chosen_run_id = st.selectbox(
            "Select run for this position",
            options=[r["id"] for r in pos_runs],
            index=default_run_idx,
            format_func=_format_pos_run,
            key="runs_run_select",
            on_change=adopt_run,
            args=("runs_run_select",),
        )

        run_id = chosen_run_id
    else:
        st.info(
            "No runs created for this position yet. "
            "Click 'Create a run over the folder' above to create one."
        )
        return

    controls(client, run_id)
    live_status(run_id)


def controls(client: ApiClient, run_id: str) -> None:
    start, rescan, abort = st.columns(3)

    if start.button("Start"):
        queued = call(client.start_run, run_id)
        if queued is not None:
            st.success(f"{queued} file(s) queued — the worker will pick them up.")

    if rescan.button("Rescan folder", help="Pick up files added since the snapshot"):
        added = call(client.rescan_run, run_id)
        if added is not None:
            st.success(f"{added} new file(s) added.")

    if abort.button("Abort", help="Stops the run. Already-screened results are kept."):
        if call(client.abort_run, run_id) is None:
            st.warning("Run aborted. It can be started again later.")


@st.fragment(run_every="5s")
def live_status(run_id: str) -> None:
    """Polling, not WebSockets.

    A job that updates every few seconds over more than an hour does not justify
    a persistent connection (22.2). The fragment re-runs on its own so the rest
    of the page is not rebuilt underneath the reviewer.
    """
    status = call(get_client().run_status, run_id)
    if not status:
        return

    st.subheader(f"Status: {status['status']}")

    done, total = status["done"] + status["failed"], status["total"]
    st.progress(done / total if total else 0.0, text=f"{done} of {total} screened")

    a, b, c, d = st.columns(4)
    a.metric("Screened", status["done"])
    b.metric("Failed", status["failed"])
    c.metric("Remaining", status["pending"] + status["claimed"])
    d.metric("ETA", _humanize(status["eta_seconds"]))

    if status["queue_depth_ahead"]:
        st.caption(
            f"{status['queue_depth_ahead']} file(s) from earlier runs are ahead in the queue."
        )

    failed_files(status.get("failed_files", []))
    escalation_meter(status["escalation_rate"])


def failed_files(files: list[dict[str, Any]]) -> None:
    """The files behind the `Failed` count, named.

    These produced no candidate, so they appear in none of the review sections —
    a count alone leaves a reviewer no way to find out who is missing from the
    results, or to tell "nobody applied" from "three CVs never got read".
    """
    if not files:
        return

    st.error(
        f"**{len(files)} file(s) were never screened** and are missing from the results "
        "below. This is an infrastructure failure, not a judgement about the applicant. "
        "Sign-off is blocked until they are resolved — fix the cause and use "
        "**Rescan folder**, or abort the run."
    )
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "File": f["filename"],
                    "Phase": f["phase"],
                    "Attempts": f["attempts"],
                    "Last error": f["last_error"],
                }
                for f in files
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )
