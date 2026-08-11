"""Reviewer interface (spec 18 of the build order, 10.6, 18.2).

**An HTTP client and nothing else.** No database driver, no `screener.storage`,
no `screener.pipeline`. Streamlit re-executes this whole script on every
interaction, which is exactly why screening cannot live here: a long-lived thread
in this process produces zombie threads, leaked connections, and work lost on
reconnect (2).

Three presentation decisions carry real weight, and each exists to prevent a
specific way that human oversight quietly stops working.

**Reviewers see a band, not a score.** Three verdict levels across at most twelve
criteria cannot support a rendered precision of `7.8`. The decimal implies
resolution that does not exist and invites over-reliance on a number the system
cannot actually justify to that precision. The float is exported for audit and
shown in the detail view where its provenance is visible alongside it (10.6).

**`needs_review` is its own tab, never the tail of a ranking.** At 1,000
applicants a reviewer reads the top of Band A and stops. An unscoreable candidate
parked at the bottom of one long list is invisible in practice — which is the
adverse outcome escalation was redesigned to prevent (10.5b).

**The escalation rate is on screen the whole time.** Oversight collapses into
rubber-stamping the moment the queue exceeds what a person will actually read,
and that failure is silent — the control still *looks* like it is working. The
number is shown against its budget so it is noticed while a run is in progress
rather than discovered afterwards (18.2).
"""

import os
import sys
from collections import Counter
from html import escape
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from api_client import ApiClient, ApiError  # noqa: E402

# Read from the environment so a deployment does not need the file edited, and
# so the UI does not silently point at whatever else happens to be on port 8000.
# Overridable at runtime in the sidebar too — an operator moving the API should
# not need a restart to follow it.
DEFAULT_API = os.environ.get("SCREENER_API_URL", "http://127.0.0.1:8000")
ESCALATION_BUDGET = 0.03

BAND_HELP = {
    "A": "Strong match against the rubric",
    "B": "Good match, some gaps",
    "C": "Partial match",
    "D": "Weak match",
}

FLAG_HELP = {
    "POSSIBLE_DUPLICATE": (
        "Byte-identical to another file in this run. Only catches exact copies — "
        "the same CV re-exported from Word has a different hash, so absence of "
        "this flag is not evidence of no duplicate."
    ),
    "SUSPECTED_INJECTION": (
        "The document contains instruction-like text. This is a prompt for a "
        "human look, not a judgement about the candidate — a security "
        "engineer's CV legitimately trips it."
    ),
    "EVIDENCE_UNVERIFIED": (
        "The quoted evidence could not be matched back to the document. The "
        "system could not verify its own output, so the candidate is not ranked."
    ),
    "EVIDENCE_CONTRADICTS": (
        "The model claimed support and simultaneously said there was none. The "
        "verdict was forced to 'none'."
    ),
    "BUDGET_EXCEEDED": "Too long to judge without truncation. Nothing was truncated.",
    "INPUT_REJECTED": "Rejected before parsing — see the reason in the summary.",
    "EXTRACTION_FAILED": "No text could be read from the file, including by OCR.",
    "SANITIZED_TEXT": "Invisible or bidirectional characters were removed before judging.",
    "FREETEXT_SCREENED": "Non-job-relevant commentary was removed from the summary.",
    "MISSING_MUST_HAVE": "Does not meet a stated hard requirement.",
    "VERDICT_SET_MISMATCH": "The model did not return one verdict per criterion.",
    "PARSER_TIMEOUT": "The parser exceeded its time limit. Transient — retry.",
    "PARSER_CRASHED": "The parser failed on this file. Reported as a security event.",
    "LLM_ERROR": "The model was unreachable. Transient — retry.",
    "SCHEMA_INVALID": "The model returned malformed output. Transient — retry.",
}


# --- plumbing ----------------------------------------------------------------


def get_client() -> ApiClient:
    return ApiClient(
        base_url=st.session_state.get("api_url", DEFAULT_API),
        actor=st.session_state.get("actor", "poc-operator"),
    )


def call(fn: Any, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
    """Run an API call, surfacing failures as a message rather than a traceback.

    A stack trace mid-page is useless to a recruiter and indistinguishable from
    a bug in the screening itself.
    """
    try:
        return fn(*args, **kwargs)
    except ApiError as exc:
        st.error(str(exc))
        return None




def sidebar() -> None:
    st.sidebar.title("Screener")
    st.sidebar.caption("On-prem. No candidate data leaves this host.")

    if "pending_run_id" in st.session_state:
        st.session_state["run_id"] = st.session_state.pop("pending_run_id")

    st.session_state.setdefault("api_url", DEFAULT_API)
    st.session_state.setdefault("actor", "poc-operator")
    st.session_state.setdefault("run_id", "")

    st.sidebar.text_input("API", key="api_url")
    # Reviewer identity. Stubbed auth (15.2) — but it reaches the audit log, so
    # the two-person flow (one approves, another signs off) is demonstrable.
    st.sidebar.text_input("Reviewing as", key="actor")
    # One run selector for the whole app. Two tabs each rendering their own
    # `text_input("Run id")` collided on Streamlit's auto-generated element id —
    # it derives one from the widget type and parameters, so two identical
    # widgets are indistinguishable to it and the second raises. Keeping the run
    # in the sidebar is also the truer model: it is the context every tab is
    # working within, not a field belonging to any one of them.
    st.sidebar.text_input("Run id", key="run_id")


    health = None
    try:
        health = get_client().health()
    except ApiError as exc:
        st.sidebar.error(str(exc))

    if health:
        if health["ok"]:
            st.sidebar.success("Ready")
        else:
            st.sidebar.warning("Degraded")
        for label, key in (
            ("Model", "llm_reachable"),
            ("Schema", "migrations_current"),
            ("Disk", "disk_ok"),
        ):
            st.sidebar.caption(f"{'✓' if health[key] else '✗'} {label}")
        if not health["model_digest_matches_pin"]:
            st.sidebar.warning(
                "Model weights differ from the pinned digest — results stored "
                "before this change are no longer reproducible."
            )


# --- positions and rubrics ---------------------------------------------------


def positions_page() -> None:
    st.header("Requisitions")
    client = get_client()

    with st.expander("New requisition"):
        with st.form("create_position"):
            reference = st.text_input(
                "Folder reference",
                help="Resumes are read from data/resumes/<reference>/",
            )
            title = st.text_input("Job title")
            jd_text = st.text_area("Job description", height=200)
            if st.form_submit_button("Create") and reference and title and jd_text:
                if call(client.create_position, reference, title, jd_text):
                    st.success(f"Created {reference}")
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


# --- runs --------------------------------------------------------------------


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
        st.success(
            f"Approved Rubric v{approved_rubric['version']} (`{approved_rubric['rubric_hash'][:12]}`) "
            f"· {len(approved_rubric['criteria'])} criteria · Approved by {approved_rubric['approved_by']}"
        )
        if st.button("Create a run over the folder"):
            run = call(client.create_run, chosen_pos_id, approved_rubric["id"])
            if run:
                st.session_state["pending_run_id"] = run["id"]
                st.success(f"Snapshotted the folder into run {run['id']}")
                st.rerun()



    else:
        st.info(
            "No approved rubric found for this position. "
            "Please approve a rubric on the Requisitions tab before creating a run."
        )

    run_id = st.session_state.get("run_id", "")
    if not run_id:
        st.info("Create a run above, or enter a run id in the sidebar.")
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

    escalation_meter(status["escalation_rate"])


def escalation_meter(rate: float) -> None:
    """Shown during the run, not after it.

    Human oversight collapses into rubber-stamping the moment the review queue
    exceeds what a person will actually read, and that failure is silent — the
    control still looks like it is working (18.2).
    """
    st.metric(
        "Needing review",
        f"{rate:.0%}",
        delta=f"{(rate - ESCALATION_BUDGET) * 100:+.1f} pts vs budget",
        delta_color="inverse",
    )
    if rate > ESCALATION_BUDGET:
        st.warning(
            f"{rate:.0%} of candidates need a human decision, against a {ESCALATION_BUDGET:.0%} "
            "design budget. A queue larger than a person will genuinely read is "
            "the point at which review stops being meaningful."
        )


def _humanize(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


# --- review ------------------------------------------------------------------


def review_page() -> None:
    st.header("Review")
    client = get_client()

    run_id = st.session_state.get("run_id", "")
    if not run_id:
        st.info("Enter a run id in the sidebar.")
        return

    result = call(client.list_candidates, run_id)
    if not result:
        return

    escalation_meter(result["escalation_rate"])

    qualified = result["meets_must_haves"]
    unqualified = result["missing_must_have"]
    review = result["needs_review"]

    # Sections in fixed order, **needs-review first and open** (15.4). Tabs were
    # the previous shape and they were wrong for the same reason a long list is:
    # at 1,000 applicants a reviewer works the first thing on the screen, and a
    # third tab is a place escalations go to be not looked at.
    with st.expander(f"⚠ Needs review — {len(review)}", expanded=True):
        st.caption(
            "The system could not produce a reliable result for these. They are "
            "**not ranked and not scored** — that is a statement about the "
            "system, not about the candidate."
        )
        escalation_breakdown(review)
        partition(review, run_id, unranked=True)

    with st.expander(f"Meets all must-haves — {len(qualified)}", expanded=not review):
        partition(qualified, run_id)

    with st.expander(f"Missing a must-have — {len(unqualified)}", expanded=False):
        st.caption(
            "These did not meet a stated hard requirement. They are ranked "
            "among themselves, never against the group above — the two are not "
            "comparable."
        )
        partition(unqualified, run_id)

    sign_off_section(client, run_id, result)


ESCALATION_LABELS = {
    "unverified_evidence": "unverified evidence",
    "judge_disagreement": "judge disagreement",
    "absence_found": 'evidence found for an "absent" criterion',
    "negation": "negation suspected",
    "partial_must_have": "partial evidence on a must-have",
    "unprocessable": "could not be processed",
    "suspected_injection": "suspected injection",
}


def escalation_breakdown(candidates: list[dict[str, Any]]) -> None:
    """Grouped by reason, never one undifferentiated count (15.4).

    "23 need review" prompts a shrug. "8 unverified evidence, 7 judge
    disagreement" tells a reviewer that similar cases can be worked in a batch,
    which is the difference between a queue that gets cleared and one that does
    not.
    """
    counts = Counter(
        reason for candidate in candidates for reason in candidate.get("escalation_reasons", [])
    )
    for reason, count in counts.most_common():
        st.write(f"**{count}** &nbsp; {ESCALATION_LABELS.get(reason, reason)}")


def partition(candidates: list[dict[str, Any]], run_id: str, *, unranked: bool = False) -> None:
    if not candidates:
        st.info("Nobody in this group.")
        return

    table = pd.DataFrame(
        [
            {
                "Candidate": c["filename"],
                # Band, not score. The decimal implies a precision three verdict
                # levels cannot support (10.6).
                "Band": c["band"] or "—",
                "Flags": ", ".join(c["flags"]) or "",
            }
            for c in candidates
        ]
    )
    st.dataframe(table, use_container_width=True, hide_index=True)

    chosen = st.selectbox(
        "Open",
        options=range(len(candidates)),
        format_func=lambda i: candidates[i]["filename"],
        key=f"open-{run_id}-{unranked}-{len(candidates)}",
    )
    if chosen is not None:
        candidate_detail(candidates[chosen], run_id)

    st.download_button(
        "Export CSV",
        _to_csv(candidates),
        file_name=f"{run_id}-candidates.csv",
        mime="text/csv",
        key=f"csv-{run_id}-{unranked}-{len(candidates)}",
        help="Includes the numeric score for audit. On screen, reviewers see bands.",
    )

    if not unranked:
        bulk_decision_form(candidates, run_id)


def bulk_decision_form(candidates: list[dict[str, Any]], run_id: str) -> None:
    """One decision across a group, with a shared reason.

    Offered here and **not** on the needs-review section. A reviewer working 400
    clear rejections one modal at a time stops reading them, so the bulk path is
    real; but the escalated ones are exactly where the system said a human has to
    look, and the server skips them regardless of what this sends.
    """
    eligible = [c for c in candidates if not c["review_required"] and c.get("id")]
    if not eligible:
        return

    with st.expander(f"Decide on all {len(eligible)} at once"):
        with st.form(f"bulk-{run_id}-{len(candidates)}"):
            decision = st.radio("Decision", ["advance", "reject", "hold"], horizontal=True)
            reason = st.text_area("Shared reason", help="Required. Recorded against every one.")
            if st.form_submit_button(f"Record for {len(eligible)}"):
                if not reason.strip():
                    st.error("A reason is required.")
                    return
                result = call(
                    get_client().decide_bulk, [int(c["id"]) for c in eligible], decision, reason
                )
                if result is not None:
                    st.success(f"Recorded for {len(result['decided'])}.")
                    if result["skipped"]:
                        st.warning(
                            f"{len(result['skipped'])} skipped — they need review and "
                            "have to be opened individually."
                        )


def candidate_detail(candidate: dict[str, Any], run_id: str) -> None:
    st.subheader(candidate["filename"])

    if candidate.get("verification_status") == "pending":
        # A half-verified result that renders like a finished one is how someone
        # signs off on work that has not happened yet (17.6).
        st.warning(
            "**Provisional** — the second model has not checked this candidate "
            "yet. Escalations it would raise are not shown below."
        )

    if candidate.get("id"):
        st.link_button("Open the original document", get_client().file_url(candidate["id"]))

    left, right = st.columns([1, 2])
    left.metric("Band", candidate["band"] or "Not scored")
    if candidate["score"] is None:
        # None, never 0.0 — a resume that could not be read is not a weak
        # candidate, and showing 0 would put them among genuinely weak ones.
        right.info("Not scored. See the flags below for why.")
    else:
        right.caption(f"Internal score {candidate['score']} / 10 — exported for audit.")

    for flag in candidate["flags"]:
        st.warning(f"**{flag}** — {FLAG_HELP.get(flag, 'See the run log.')}")

    if candidate["summary"]:
        st.write(candidate["summary"])
    if candidate["notable_strengths"]:
        st.write("**Notable:** " + "; ".join(candidate["notable_strengths"]))
    if candidate["red_flags"]:
        st.write("**Flagged in the document:** " + ", ".join(candidate["red_flags"]))

    st.markdown("**Criteria**")
    st.caption(
        "Evidence is quoted from the candidate's own document. It confirms the "
        "model read the resume faithfully — it cannot tell a true claim from a "
        "false one, and it is not fraud detection."
    )
    for criterion in candidate["criteria"]:
        criterion_block(criterion, candidate)

    decision_form(candidate, run_id)


EVIDENCE_BADGE = {
    "verified": "✓ verified",
    "partial": "⚠ partially matched",
    "unverified": "⚠ not found in the résumé",
    "not_applicable": "",
}


def criterion_block(criterion: dict[str, Any], candidate: dict[str, Any]) -> None:
    """One criterion, its quote in context, and any disagreement about it (15.5)."""
    must = " · **MUST-HAVE**" if criterion["must_have"] else ""
    badge = EVIDENCE_BADGE.get(criterion["evidence_status"], "")
    st.markdown(
        f"**{criterion['id']}** {criterion['text']} — `{criterion['verdict'].upper()}` "
        f"w{criterion['weight']}{must} &nbsp; {badge}"
    )

    if criterion["evidence"]:
        # The quote inside its surrounding paragraph, not alone: a bare quote can
        # be cherry-picked from a sentence that said the opposite (15.3).
        st.markdown(
            highlighted(candidate.get("resume_text", ""), criterion["highlights"])
            or f"> {criterion['evidence']}",
            unsafe_allow_html=True,
        )

    if criterion.get("negation_suspected"):
        st.caption("⚠ A negation appears just before this quote — read the full sentence.")

    verifier = criterion.get("verifier")
    if verifier:
        # "A second model disagrees — you decide", never "the correct answer is".
        # Presented as an answer, reviewers defer to it, and automated
        # decision-making returns through the interface (15.5).
        suggested = (verifier.get("suggested_verdict") or "—").upper()
        st.info(
            f"**A second model disagrees — you decide.** It suggests `{suggested}`.\n\n"
            f"{verifier.get('rationale') or ''}"
            + (
                f"\n\nIt located: “{verifier['found_evidence']}”"
                if verifier.get("found_evidence")
                else ""
            )
        )


CONTEXT_CHARS = 240


def highlighted(resume_text: str, highlights: list[dict[str, int]]) -> str:
    """The matched span marked inside the surrounding text of `resume_text`.

    Offsets arrive already translated out of `sent_text` by the read layer, so
    this only has to slice. Doing the translation here would put it downstream of
    the boundary that owns it and inside the one process with no tests.
    """
    if not resume_text or not highlights:
        return ""

    start = min(h["start"] for h in highlights)
    end = max(h["end"] for h in highlights)
    left = max(0, start - CONTEXT_CHARS)
    right = min(len(resume_text), end + CONTEXT_CHARS)

    before = escape(resume_text[left:start])
    matched = escape(resume_text[start:end])
    after = escape(resume_text[end:right])
    ellipsis_l = "…" if left > 0 else ""
    ellipsis_r = "…" if right < len(resume_text) else ""
    return f"<blockquote>{ellipsis_l}{before}<mark>{matched}</mark>{after}{ellipsis_r}</blockquote>"


def decision_form(candidate: dict[str, Any], run_id: str) -> None:
    current = candidate.get("decision", "undecided")
    label = "Record a decision" if current == "undecided" else f"Decision: {current} — change it"
    with st.expander(label, expanded=current == "undecided"):
        st.caption(
            "Your decision is recorded against your name with the reason, and "
            "cannot be edited afterwards."
        )
        with st.form(f"decide-{run_id}-{candidate['file_sha256']}"):
            decision = st.radio("Decision", ["advance", "reject", "hold"], horizontal=True)
            reason = st.text_area("Reason", help="Required. This is the record.")
            if st.form_submit_button("Record"):
                if not reason.strip():
                    st.error("A reason is required.")
                elif call(get_client().decide, int(candidate["id"]), decision, reason) is None:
                    st.success("Recorded.")


def sign_off_section(client: ApiClient, run_id: str, result: dict[str, Any]) -> None:
    st.divider()
    # Mirrors the server's precondition rather than counting the section, so the
    # warning and the refusal agree. A screen that says "ready" and a button that
    # returns 400 teaches reviewers to distrust the screen.
    outstanding = [
        c
        for group in ("needs_review", "meets_must_haves", "missing_must_have")
        for c in result[group]
        if c.get("decision", "undecided") == "undecided"
        and (c["review_required"] or c.get("verification_status") == "pending")
    ]
    if outstanding:
        st.warning(
            f"{len(outstanding)} candidate(s) still need a decision. Sign-off is "
            "blocked until each one is advanced, held, or rejected — signing off "
            "with an unread queue is the failure this screen exists to prevent."
        )
    if st.button("Sign off this run", type="primary", disabled=bool(outstanding)):
        if call(client.sign_off, run_id) is None:
            st.success("Signed off. Recorded against your name.")


def _to_csv(candidates: list[dict[str, Any]]) -> str:
    return pd.DataFrame(
        [
            {
                "filename": c["filename"],
                "file_sha256": c["file_sha256"],
                "band": c["band"],
                "score": c["score"],
                "must_haves_met": c["must_haves_met"],
                "scoreable": c["scoreable"],
                "review_required": c["review_required"],
                # 15.6: the outcome and who owns it, not only the ranking.
                "decision": c.get("decision", "undecided"),
                "decided_by": c.get("decided_by") or "",
                "decided_at": c.get("decided_at") or "",
                "verification_status": c.get("verification_status", ""),
                "escalation_reasons": "|".join(c.get("escalation_reasons", [])),
                "flags": "|".join(c["flags"]),
                "scored_at": c["scored_at"],
            }
            for c in candidates
        ]
    ).to_csv(index=False)


# --- entry point -------------------------------------------------------------


def main() -> None:
    st.set_page_config(page_title="Screener", page_icon="📄", layout="wide")
    sidebar()

    requisitions, runs, review = st.tabs(["Requisitions", "Runs", "Review"])
    with requisitions:
        positions_page()
    with runs:
        runs_page()
    with review:
        review_page()


if __name__ == "__main__":
    # Streamlit executes this file with `__name__ == "__main__"`, so this is the
    # entry point under `streamlit run` as well as under `python`.
    #
    # There is deliberately no `else: main()`. Calling it on import would mean
    # merely importing this module renders the whole app — which breaks any test
    # that wants to read a constant, and turns an import into a live HTTP call
    # against the API.
    main()
