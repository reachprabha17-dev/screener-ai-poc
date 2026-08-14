from collections import Counter
from html import escape
from typing import Any

import pandas as pd
import streamlit as st

from ui.api_client import ApiClient
from ui.common import (
    CONTEXT_CHARS,
    ESCALATION_LABELS,
    EVIDENCE_BADGE,
    FLAG_HELP,
    VERIFICATION_BADGE,
    adopt_run,
    call,
    escalation_meter,
    get_client,
)


def review_page() -> None:
    st.header("Review")
    client = get_client()

    runs = call(client.list_runs) or []
    positions = call(client.list_positions) or []
    pos_map = {p["id"]: f"{p['reference']} — {p['title']}" for p in positions}

    if not runs:
        st.info("No screening runs found yet. Create and start a run on the Runs tab.")
        return

    st.subheader("Historic Runs")
    table_data = []
    for r in runs:
        rate = r.get("escalation_rate")
        rate_str = f"{rate:.0%}" if rate is not None else "N/A"
        created = r.get("created_at")
        if isinstance(created, str):
            created_str = created[:10] if created else "N/A"
        elif hasattr(created, "strftime"):
            created_str = created.strftime("%Y-%m-%d")
        else:
            created_str = "N/A"
        pos_title = pos_map.get(r["position_id"], r["position_id"])
        table_data.append(
            {
                "Run ID": r["id"],
                "Position": pos_title,
                "Status": r["status"],
                "Created At": created_str,
                "Created By": r.get("created_by", "N/A"),
                "Files": r.get("file_count", 0),
                "Escalation Rate": rate_str,
            }
        )

    st.dataframe(table_data, use_container_width=True)

    current_run_id = st.session_state.get("run_id", "")
    default_index = 0
    if current_run_id:
        for idx, r in enumerate(runs):
            if r["id"] == current_run_id:
                default_index = idx
                break

    def _format_run_option(rid: str) -> str:
        r = next((run for run in runs if run["id"] == rid), None)
        if not r:
            return rid
        title = pos_map.get(r["position_id"], r["position_id"])
        status = r["status"].capitalize()
        files = r.get("file_count", 0)
        return f"{r['id']} — Position: {title} ({status}, {files} files)"

    chosen_run_id = st.selectbox(
        "Select a run to review",
        options=[r["id"] for r in runs],
        index=default_index,
        format_func=_format_run_option,
        key="review_run_select",
        on_change=adopt_run,
        args=("review_run_select",),
    )
    run_id = chosen_run_id

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
    with st.expander(f"Needs review — {len(review)}", expanded=True):
        st.caption(
            "The system could not produce a reliable result for these. They are "
            "**not ranked and not scored** — that is a statement about the "
            "system, not about the candidate."
        )
        escalation_breakdown(review)
        partition(review, run_id, section="needs-review", unranked=True)

    with st.expander(f"Meets all must-haves — {len(qualified)}", expanded=not review):
        partition(qualified, run_id, section="qualified")

    with st.expander(f"Missing a must-have — {len(unqualified)}", expanded=False):
        st.caption(
            "These did not meet a stated hard requirement. They are ranked "
            "among themselves, never against the group above — the two are not "
            "comparable."
        )
        partition(unqualified, run_id, section="unqualified")

    sign_off_section(client, run_id, result)


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


def partition(
    candidates: list[dict[str, Any]], run_id: str, *, section: str, unranked: bool = False
) -> None:
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
                "Verified": VERIFICATION_BADGE.get(c.get("verification_status", ""), "—"),
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
        key=f"open-{run_id}-{section}",
    )
    if chosen is not None:
        candidate_detail(candidates[chosen], run_id)

    st.download_button(
        "Export CSV",
        _to_csv(candidates),
        file_name=f"{run_id}-candidates.csv",
        mime="text/csv",
        key=f"csv-{run_id}-{section}",
        help="Includes the numeric score for audit. On screen, reviewers see bands.",
    )

    if not unranked:
        bulk_decision_form(candidates, run_id, section=section)


def bulk_decision_form(candidates: list[dict[str, Any]], run_id: str, *, section: str) -> None:
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
        with st.form(f"bulk-{run_id}-{section}"):
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
        st.caption("A negation appears just before this quote — read the full sentence.")

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
