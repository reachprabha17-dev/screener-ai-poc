import pandas as pd
import streamlit as st

from ui.api_client import ApiClient
from ui.common import PAGE_SIZE, call, get_client


def folder_browser(client: ApiClient) -> str:
    """Navigate the resume share and return the selected folder, or "".

    **The server's filesystem, not the reviewer's.** The share is mounted on the
    Screener host, so browsing here is browsing the share. A browser cannot hand
    a server a path from the machine it is running on — that is why this is a
    server-side navigator rather than a native folder dialog.

    Paths are relative to `settings.resumes_dir` throughout; the reviewer never
    sees or types an absolute path, and `folder_for` re-checks containment on
    every use regardless of what this widget produced.
    """
    st.markdown("**Resume folder**")

    path = st.session_state.get("browse_path", "")
    query = st.session_state.get("browse_query", "")
    offset = st.session_state.get("browse_offset", 0)

    page = call(client.list_resume_folders, path, query, offset, PAGE_SIZE) or {}
    folders = page.get("folders", [])
    total = page.get("total", 0)

    crumbs, refresh = st.columns([4, 1])
    crumbs.caption(f"In: `{path or 'share root'}`")
    if refresh.button("Refresh", help="Re-read the share — use after adding a folder"):
        st.rerun()

    # Filtering server-side, because the cost being avoided is server-side: only
    # the folders on the visible page get their resumes counted.
    typed = st.text_input(
        "Search folders",
        value=query,
        key="browse-query-input",
        placeholder="Type to narrow the list…",
        label_visibility="collapsed",
    )
    if typed != query:
        st.session_state["browse_query"] = typed
        st.session_state["browse_offset"] = 0
        st.rerun()

    if path:
        # Selecting where you *are*, not only what is below it. Without this the
        # share root has to be the parent of every requisition folder, and a
        # `RESUMES_DIR` pointing straight at a folder of CVs offers nothing to
        # pick — the folder is visible, browsable, and unselectable.
        up_col, here_col = st.columns(2)
        if up_col.button("Up one level", key="browse-up", use_container_width=True):
            st.session_state["browse_path"] = path.rpartition("/")[0]
            st.rerun()
        if here_col.button(
            "Use this folder",
            key="browse-pick-here",
            use_container_width=True,
            help=f"Screen the CVs directly inside {path}",
        ):
            st.session_state["picked_folder"] = path
            st.rerun()

    if not folders:
        if query:
            st.caption(f"No folders matching “{query}”.")
        elif path:
            st.caption("No subfolders here — use **Use this folder** to screen this one.")
        else:
            st.warning(
                "Nothing found on the resume share. Either it holds no folders yet, or "
                "`RESUMES_DIR` is not pointing where you think — it is currently a folder "
                "the server can see but which is empty. Add a folder of CVs inside it, "
                "then use **Refresh** to re-read."
            )
    else:
        for folder in folders:
            label_col, open_col = st.columns([5, 1])
            if folder["has_subfolders"]:
                if open_col.button(
                    "Open", key=f"open-{folder['path']}", help="Look inside this folder"
                ):
                    st.session_state["browse_path"] = folder["path"]
                    st.session_state["browse_offset"] = 0
                    st.rerun()
            if label_col.button(
                f"{folder['name']} — {folder['file_count']} CV(s)",
                key=f"pick-{folder['path']}",
                use_container_width=True,
            ):
                st.session_state["browse_path"] = folder["path"]
                st.session_state["browse_offset"] = 0
                st.session_state["picked_folder"] = folder["path"]
                st.rerun()

        if total > PAGE_SIZE:
            prev_col, count_col, next_col = st.columns([1, 3, 1])
            if prev_col.button("‹ Prev", disabled=offset == 0, use_container_width=True):
                st.session_state["browse_offset"] = max(0, offset - PAGE_SIZE)
                st.rerun()
            count_col.caption(
                f"<div style='text-align:center'>{offset + 1}–{offset + len(folders)} "
                f"of {total}</div>",
                unsafe_allow_html=True,
            )
            if next_col.button(
                "Next ›", disabled=offset + PAGE_SIZE >= total, use_container_width=True
            ):
                st.session_state["browse_offset"] = offset + PAGE_SIZE
                st.rerun()

    picked = st.session_state.get("picked_folder", "")
    if picked:
        st.success(f"Selected: `{picked}`")
    return str(picked)


def positions_page() -> None:
    st.header("Requisitions")
    client = get_client()

    with st.expander("New requisition"):
        reference = folder_browser(client)

        with st.form("create_position"):
            title = st.text_input("Job title")
            jd_text = st.text_area("Job description", height=200)
            submitted = st.form_submit_button("Create requisition")

        if submitted:
            # Checked after submit rather than by disabling the button, so the
            # reason is stated. A disabled control with no explanation is the
            # dead end this screen had before.
            if not reference:
                st.error("Select a resume folder above first.")
            elif not (title and jd_text):
                st.error("A job title and description are both required.")
            elif call(client.create_position, reference, title, jd_text):
                st.success(f"Created {reference}")
                st.session_state.pop("browse_path", None)
                st.rerun()

    positions = call(client.list_positions) or []
    if not positions:
        st.info("No open requisitions yet.")
        return

    st.dataframe(
        pd.DataFrame(positions)[["reference", "title", "created_by", "created_at"]],
        use_container_width=True,
        hide_index=True,
    )

    chosen = st.selectbox(
        "Work on",
        options=[p["id"] for p in positions],
        format_func=lambda pid: next(
            f"{p['reference']} — {p['title']}" for p in positions if p["id"] == pid
        ),
    )
    if chosen:
        st.session_state["position_id"] = chosen
        rubric_section(chosen)


def rubric_section(position_id: str) -> None:
    st.subheader("Rubric")
    st.caption(
        "Drafted from the job description by the model, then **edited and "
        "approved by you**. Nothing is screened against an unapproved rubric — "
        "an invented requirement would silently reject every applicant who "
        "lacks something the job never asked for."
    )
    client = get_client()

    if st.button("Draft from the job description"):
        with st.spinner("Reading the job description…"):
            rubric = call(client.extract_rubric, position_id)
        if rubric:
            st.session_state["rubric"] = rubric

    rubric = st.session_state.get("rubric")
    if not rubric or rubric.get("position_id") != position_id:
        fetched = call(client.get_latest_rubric, position_id)
        if fetched:
            st.session_state["rubric"] = fetched
            rubric = fetched

    if not rubric or rubric.get("position_id") != position_id:
        st.info("No draft yet.")
        return

    st.caption(f"Version {rubric['version']} · `{rubric['rubric_hash'][:12]}`")

    edited = st.data_editor(
        pd.DataFrame(rubric["criteria"]),
        use_container_width=True,
        hide_index=True,
        num_rows="dynamic",
        column_config={
            "id": st.column_config.TextColumn("ID", disabled=True),
            "text": st.column_config.TextColumn("Criterion", width="large"),
            "must_have": st.column_config.CheckboxColumn(
                "Must have",
                help=(
                    "A hard requirement. Failing one moves the candidate to the "
                    "unqualified group — mark sparingly."
                ),
            ),
            "weight": st.column_config.NumberColumn("Weight", min_value=1, max_value=5),
        },
    )

    left, right = st.columns(2)
    if left.button("Save as new version"):
        saved = call(client.save_rubric, position_id, edited.to_dict("records"))
        if saved:
            st.session_state["rubric"] = saved
            st.success(f"Saved version {saved['version']}")
            st.rerun()

    if right.button("Approve", type="primary", disabled=bool(rubric.get("approved_at"))):
        approved = call(client.approve_rubric, rubric["id"])
        if approved:
            st.session_state["rubric"] = approved
            st.success(f"Approved by {approved['approved_by']}")

    if rubric.get("approved_at"):
        st.success(f"Approved by {rubric['approved_by']} at {rubric['approved_at']}")
