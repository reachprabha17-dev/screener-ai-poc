"""The audit log, as a narrative rather than a table (15.4).

**Scoped to one run by default, searchable second.** The question people bring to
an audit log is "show me how this hiring decision was made and prove a human was
involved", and that question is always about one thing. A chronological dump of
every row answers a different and rarer question — "what has this person done
across everything" — which is what the search view is still here for.

Events are rendered as sentences through `EVENT_TEXT`. Any action without an
entry falls back to its raw action and detail: a newly audited action must
degrade to the old behaviour, never to a `KeyError` on a compliance screen.
"""

import datetime
from collections.abc import Callable
from typing import Any

import pandas as pd
import streamlit as st

from ui.common import call, get_client

KNOWN_ACTIONS = [
    "",
    "create_position",
    "extract_rubric",
    "save_rubric",
    "approve_rubric",
    "create_run",
    "start_run",
    "abort_run",
    "rescan_run",
    "advance_phase",
    "complete_run",
    "decision",
    "sign_off_run",
    "purge_candidate",
    "reclaim_orphaned",
]

# The two events that record a human accepting responsibility. Marked so they are
# findable at a glance — they are what "a human was involved" actually means.
HUMAN_GATES = {"approve_rubric", "sign_off_run"}


def _short(value: Any, n: int = 8) -> str:  # noqa: ANN401 — detail values are untyped JSON
    text = str(value or "")
    return f"{text[:n]}…" if len(text) > n else text


EVENT_TEXT: dict[str, Callable[[dict[str, Any]], str]] = {
    "create_position": lambda d: f"Raised requisition **{d.get('reference', '?')}**.",
    "extract_rubric": lambda d: (
        f"Model drafted a rubric of **{d.get('criteria', '?')} criteria** "
        f"(prompt `{_short(d.get('prompt_hash'))}`, model `{_short(d.get('judge_digest'))}`)."
    ),
    "save_rubric": lambda d: (
        f"Edited the rubric → **version {d.get('version', '?')}** "
        f"(`{_short(d.get('rubric_hash'))}`)."
    ),
    "approve_rubric": lambda d: f"**Approved the rubric** (`{_short(d.get('rubric_hash'))}`).",
    "create_run": lambda d: (
        f"Created a run over `{d.get('folder', '?')}` — "
        f"**{d.get('queued', 0)} file(s)** snapshotted."
    ),
    "start_run": lambda d: f"Started screening — {d.get('queued', 0)} file(s) queued.",
    "advance_phase": lambda d: f"Advanced to the **{d.get('phase', '?')}** phase.",
    "complete_run": lambda d: (
        f"Screening complete — {d.get('done', 0)} judged, {d.get('failed', 0)} failed, "
        f"**{float(d.get('escalation_rate') or 0):.0%} escalated**."
    ),
    "rescan_run": lambda d: f"Rescanned the folder — {d.get('added', 0)} new file(s) added.",
    "abort_run": lambda d: f"Aborted the run — {d.get('released', 0)} job(s) released.",
    "sign_off_run": lambda _d: "**Signed off the run**, accepting the results.",
    "reclaim_orphaned": lambda d: f"Recovered {d.get('jobs', 0)} job(s) from a stopped worker.",
    "purge_candidate": lambda d: (
        f"Erased a candidate's stored data ({d.get('reason', 'no reason')})."
    ),
}


def _decision_text(detail: dict[str, Any]) -> str:
    """Decisions get their own renderer because the reason is the point.

    That string is the adverse-action justification — the answer to "why was this
    person rejected" — so it is never truncated and never hidden behind an
    expander.
    """
    verdict = str(detail.get("decision", "?")).upper()
    score = detail.get("old_score")
    scored = f" (system scored {score})" if score is not None else ""
    reason = detail.get("reason") or "_no reason recorded_"
    return f"**{verdict}**{scored} — {reason}"


def describe(entry: dict[str, Any]) -> str:
    detail = entry.get("detail") or {}
    action = entry.get("action", "")
    if action == "decision":
        return _decision_text(detail)
    renderer = EVENT_TEXT.get(action)
    if renderer is None:
        # Unknown action — show what the row says rather than nothing. A new
        # audited action appears here as raw data until it earns a sentence.
        return f"`{action}`" + (f" · `{detail}`" if detail else "")
    try:
        return renderer(detail)
    except (KeyError, TypeError, ValueError):
        # A malformed detail must not blank the compliance screen.
        return f"`{action}` · `{detail}`"


def audit_page() -> None:
    st.header("Audit")
    st.caption(
        "Append-only record of every decision and mutation, enforced by database "
        "triggers rather than by convention."
    )

    # A radio rather than st.tabs: tab bodies all execute on every render, so
    # tabs here would fetch the run list, the story and a page of the log on
    # every interaction regardless of which one is being looked at.
    view = st.radio(
        "View",
        ["Run story", "Decision record", "Search the log"],
        horizontal=True,
        label_visibility="collapsed",
        key="audit_view",
    )
    st.divider()
    if view == "Run story":
        _run_story_view()
    elif view == "Decision record":
        _decision_record_view()
    else:
        _search_view()


def _run_story_view() -> None:
    client = get_client()
    runs = call(client.list_runs) or []
    if not runs:
        st.info("No runs yet. A run's story appears here once one has been created.")
        return

    positions = call(client.list_positions) or []
    pos_map = {p["id"]: p["reference"] for p in positions}

    def label(rid: str) -> str:
        run = next((r for r in runs if r["id"] == rid), None)
        if run is None:
            return rid
        where = pos_map.get(run["position_id"], run["position_id"])
        created = str(run.get("created_at") or "")[:10]
        return f"{rid} · {where} · {run['status'].capitalize()} · {created}"

    # No comparison against `run_id` and no st.rerun(): this selector reads the
    # shared run but does not fight the ones on Runs and Review for it.
    chosen = st.selectbox(
        "Run",
        options=[r["id"] for r in runs],
        format_func=label,
        key="audit_run_select",
    )

    story = call(client.run_story, chosen)
    if not story:
        return

    st.subheader(f"{story['position_reference']}")
    version = story.get("rubric_version")
    st.caption(
        f"Run `{story['run_id']}` · rubric "
        + (f"v{version}" if version else "unknown version")
        + f" · approved by **{story.get('approved_by') or '—'}**"
        + f" · signed off by **{story.get('signed_off_by') or 'not yet'}**"
    )

    _separation_of_duties(story)

    if story.get("candidate_events_truncated"):
        st.info(
            "This run has more candidates than the story shows individually. "
            "Per-candidate decisions are truncated; the run-level record is complete."
        )

    events = story.get("events", [])
    if not events:
        st.info("No audit events recorded for this run.")
        return

    st.markdown("#### Timeline")
    for entry in events:
        _event_row(entry)


def _separation_of_duties(story: dict[str, Any]) -> None:
    """Whether one person both approved the rubric and accepted its results.

    Permitted by the system — 14 allows it and the flow is demonstrable with one
    operator — but it is the first thing an auditor checks, so the record says it
    outright instead of leaving it to be reconstructed from two lines of the log.
    """
    approved, signed = story.get("approved_by"), story.get("signed_off_by")
    if not signed:
        st.warning("Not signed off yet — no one has accepted these results.")
    elif story.get("separation_of_duties"):
        st.success(
            f"Separation of duties: **{approved}** approved the rubric and "
            f"**{signed}** signed off the run."
        )
    else:
        st.warning(
            f"**{signed}** both approved the rubric and signed off the run. "
            "Permitted, but a second reviewer is the stronger record."
        )


def _event_row(entry: dict[str, Any]) -> None:
    when = str(entry.get("ts", ""))[:19].replace("T", " ")
    actor = entry.get("actor_id")
    action = entry.get("action", "")

    # `actor_id` is NULL for work no person did. Rendering that as a blank cell
    # loses a fact the record is making deliberately.
    who = f"**{actor}**" if actor else "_System_"
    # The human gates and the decisions are what an auditor scans for, so they
    # keep a label; everything else is unmarked so the labels stay meaningful.
    marker = ""
    if action in HUMAN_GATES:
        marker = "**[HUMAN GATE]** "
    elif action == "decision":
        marker = "**[DECISION]** "

    stamp, body = st.columns([1, 5])
    stamp.caption(when)
    subject = f"candidate #{entry['entity_id']}" if entry.get("entity") == "candidate" else ""
    body.markdown(f"{marker}{who} {subject} — {describe(entry)}".replace("  ", " "))


def _search_view() -> None:
    client = get_client()
    st.caption("Every recorded action, filterable. Use this to follow a person or a date.")

    with st.form("audit_filters"):
        col1, col2, col3 = st.columns(3)
        actor_id = col1.text_input("Actor", st.session_state.get("audit_actor_id", ""))
        stored_action = st.session_state.get("audit_action", "")
        action = col2.selectbox(
            "Action",
            KNOWN_ACTIONS,
            index=KNOWN_ACTIONS.index(stored_action) if stored_action in KNOWN_ACTIONS else 0,
        )
        entity = col3.text_input("Entity type", st.session_state.get("audit_entity", ""))

        col4, col5 = st.columns(2)
        entity_id = col4.text_input("Entity id", st.session_state.get("audit_entity_id", ""))
        date_range = col5.date_input(
            "Date range", value=st.session_state.get("audit_date_range", [])
        )

        if st.form_submit_button("Search"):
            st.session_state["audit_actor_id"] = actor_id
            st.session_state["audit_action"] = action
            st.session_state["audit_entity"] = entity
            st.session_state["audit_entity_id"] = entity_id
            st.session_state["audit_date_range"] = date_range
            st.session_state["audit_offset"] = 0
            st.rerun()

    actor_id = st.session_state.get("audit_actor_id", "")
    action = st.session_state.get("audit_action", "")
    entity = st.session_state.get("audit_entity", "")
    entity_id = st.session_state.get("audit_entity_id", "")
    date_range = st.session_state.get("audit_date_range", [])
    offset = st.session_state.get("audit_offset", 0)
    limit = 50

    since = until = ""
    if isinstance(date_range, tuple) and date_range:
        since = date_range[0].isoformat()
        # `until` advances a day so the end date is included rather than cut off
        # at midnight, which would silently drop everything done that day.
        end = date_range[1] if len(date_range) == 2 else date_range[0]
        until = (end + datetime.timedelta(days=1)).isoformat()

    response = call(
        client.search_audit,
        actor_id=actor_id,
        action=action,
        entity=entity,
        entity_id=entity_id,
        since=since,
        until=until,
        offset=offset,
        limit=limit,
    )
    if response is None:
        return

    entries = response.get("entries", [])
    total = response.get("total", 0)
    if not entries:
        st.info("No audit records match those filters.")
        return

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "When": str(e.get("ts", ""))[:19].replace("T", " "),
                    "Who": e.get("actor_id") or "system",
                    "What": describe(e),
                    "Entity": f"{e.get('entity') or ''} {e.get('entity_id') or ''}".strip(),
                }
                for e in entries
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

    prev_col, count_col, next_col = st.columns([1, 2, 1])
    if prev_col.button("‹ Prev", disabled=offset == 0, key="audit_prev"):
        st.session_state["audit_offset"] = max(0, offset - limit)
        st.rerun()
    count_col.caption(f"{offset + 1}–{min(offset + limit, total)} of {total}")
    if next_col.button("Next ›", disabled=offset + limit >= total, key="audit_next"):
        st.session_state["audit_offset"] = offset + limit
        st.rerun()


VERDICT_MARK = {"strong": "met", "partial": "partial", "none": "not met"}


def _decision_record_view() -> None:
    """One person's outcome, and everything that produced it.

    The artefact handed to a regulator or to the applicant. It is deliberately
    available for every decision rather than only rejections: a record that
    exists solely for adverse outcomes cannot be checked against a favourable
    one, and that comparison is the first test of whether it is honest.
    """
    client = get_client()
    runs = call(client.list_runs) or []
    if not runs:
        st.info("No runs yet.")
        return

    chosen_run = st.selectbox(
        "Run",
        options=[r["id"] for r in runs],
        format_func=lambda rid: next(
            (f"{rid} · {r['status']}" for r in runs if r["id"] == rid), rid
        ),
        key="record_run_select",
    )

    result = call(client.list_candidates, chosen_run)
    if not result:
        return
    everyone = [
        *result.get("needs_review", []),
        *result.get("meets_must_haves", []),
        *result.get("missing_must_have", []),
    ]
    decided = [c for c in everyone if c.get("id") and c.get("decision", "undecided") != "undecided"]
    if not decided:
        st.info(
            "No decisions recorded for this run yet. A record exists once a "
            "reviewer has advanced, held or rejected someone."
        )
        return

    picked = st.selectbox(
        "Candidate",
        options=[c["id"] for c in decided],
        format_func=lambda cid: next(
            (
                f"{c['filename']} — {str(c.get('decision', '')).upper()}"
                for c in decided
                if c["id"] == cid
            ),
            str(cid),
        ),
        key="record_candidate_select",
    )

    record = call(client.adverse_action_record, int(picked))
    if not record:
        return
    _render_record(record)


def _render_record(rec: dict[str, Any]) -> None:
    outcome = str(rec.get("decision", "")).upper()
    st.subheader(f"{rec['filename']} — {outcome}")
    st.caption(
        f"Requisition **{rec['position_reference']}** · run `{rec['run_id']}` · "
        f"screened {str(rec.get('scored_at') or '')[:19].replace('T', ' ')}"
    )

    if outcome == "REJECT":
        st.error("This is an adverse outcome. The grounds below are the record of why.")

    st.markdown("#### Grounds given")
    for step in rec.get("history", []):
        st.markdown(
            f"**{step['actor_id']}** changed *{step['from_decision']}* → "
            f"**{step['to_decision']}** on {str(step['at'])[:19].replace('T', ' ')}"
        )
        # Never truncated: this is the answer to "why was I rejected".
        st.info(step["reason"])
    if not rec.get("history"):
        st.warning("No stated reason is recorded for this decision.")

    st.markdown("#### Assessment against the approved rubric")
    band = rec.get("band") or "—"
    met = "met" if rec.get("must_haves_met") else "**not met**"
    if rec.get("scoreable"):
        st.markdown(f"Band **{band}** · score {rec.get('score')}/10 · hard requirements {met}")
    else:
        # None, never 0.0 — an unreadable document is not a weak applicant.
        st.markdown(f"**Not scored** — hard requirements {met}. See the flags below.")

    _verification_banner(rec.get("verification_status", "pending"))

    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Met?": VERDICT_MARK.get(c["verdict"], "?"),
                    "Criterion": c["text"],
                    "Must": "yes" if c["must_have"] else "",
                    "W": c["weight"],
                    # Three columns, three passes: what the judge first said,
                    # what stood after the consistency gate, and what the second
                    # model made of it. Collapsing them loses the disagreement.
                    "Judge said": c.get("model_verdict", ""),
                    "Final verdict": c["verdict"],
                    "Quote found": "yes" if c.get("verified", True) else "NO",
                    "Verifier": _verifier_cell(c),
                }
                for c in rec.get("criteria", [])
            ]
        ),
        use_container_width=True,
        hide_index=True,
    )

    st.caption(
        "**Judge said** is the first model's verdict before the consistency gate; "
        "**Final verdict** is what stood. **Quote found** is whether the judge's "
        "evidence was located in the document. Open a criterion for the quote and "
        "the verifier's reasoning."
    )

    for criterion in rec.get("criteria", []):
        _criterion_detail(criterion)

    if rec.get("flags"):
        st.markdown("**Flags:** " + ", ".join(rec["flags"]))
    if rec.get("summary"):
        st.markdown(f"**Summary:** {rec['summary']}")

    st.markdown("#### Provenance")
    st.caption(
        "What determined this outcome, captured when it was scored. The model tag "
        "will have moved and the prompt will have been edited; these will not."
    )
    approved = rec.get("rubric_approved_by") or "—"
    signed = rec.get("run_signed_off_by") or "not signed off"
    st.code(
        f"rubric        v{rec.get('rubric_version')}  {rec.get('rubric_hash', '')[:32]}…\n"
        f"judge model   {rec.get('judge_digest', '')[:32]}…\n"
        f"verifier      {(rec.get('verifier_digest') or '—')[:32]}…\n"
        f"prompt        {rec.get('prompt_hash', '')[:32]}…\n"
        f"app version   {rec.get('app_version')}\n"
        f"redaction     {'on' if rec.get('redaction_on') else 'off'}\n"
        f"rubric approved by  {approved}\n"
        f"run signed off by   {signed}",
        language="text",
    )


def _verification_banner(status: str) -> None:
    """Say whether phase 2 ran, rather than leaving silence to be interpreted.

    A record with no verifier disagreement means one of three different things —
    the second model agreed, it was skipped, or it has not run yet — and an
    auditor cannot tell them apart from the criteria table alone.
    """
    if status == "done":
        st.caption("A second model independently checked the judge's evidence on this candidate.")
    elif status == "skipped":
        st.warning(
            "**Phase 2 did not run for this candidate.** Verification is skipped for "
            "unscoreable candidates and for runs with it disabled, so nothing here has "
            "had a second opinion."
        )
    else:
        st.warning(
            "**Verification is still pending.** This assessment is provisional — the "
            "second model has not checked it, and escalations it would raise are not shown."
        )


def _verifier_cell(criterion: dict[str, Any]) -> str:
    """One-word summary of phase 2 for the scannable table."""
    support = criterion.get("support")
    absence = criterion.get("absence_confirmed")
    if support:
        return {
            "supported": "agrees",
            "insufficient": "insufficient",
            "contradicted": "disputes",
        }.get(support, support)
    if absence is True:
        return "absence confirmed"
    if absence is False:
        return "found evidence"
    return "—"


def _criterion_detail(criterion: dict[str, Any]) -> None:
    """The quote, the match numbers, and the verifier's reasoning in full.

    In an expander rather than the table because these are paragraphs; on the
    record rather than omitted because they are the substance of the assessment.
    """
    verdict = criterion["verdict"]
    must = " · MUST-HAVE" if criterion["must_have"] else ""
    with st.expander(f"{criterion['id']} — {criterion['text'][:70]} ({verdict}{must})"):
        st.markdown("**Judge**")
        if criterion.get("evidence"):
            st.markdown(f"> {criterion['evidence']}")
        else:
            st.caption("No evidence quoted — the criterion was judged absent.")
        if criterion.get("model_verdict") != verdict:
            st.warning(
                f"The judge first said **{criterion['model_verdict']}**; the consistency "
                f"gate forced it to **{verdict}** because the quote contradicted the claim."
            )
        if not criterion.get("verified", True):
            st.error(
                "The quoted evidence could not be matched back to the document. "
                "The verdict was left as returned and the candidate escalated — "
                "the system could not verify its own output."
            )
        else:
            st.caption(
                f"Quote located · match ratio {criterion.get('match_ratio', 0):.2f} · "
                f"longest run {criterion.get('longest_span', 0)} tokens"
            )
        if criterion.get("negation_suspected"):
            st.warning("A negation appears just before this quote — read the full sentence.")

        st.markdown("**Verifier (second model)**")
        support = criterion.get("support")
        absence = criterion.get("absence_confirmed")
        if support == "supported":
            st.success("Agrees: the excerpt supports the claim.")
        elif support:
            suggested = (criterion.get("suggested_verdict") or "—").upper()
            st.error(f"Disagrees ({support}) — would have said **{suggested}**.")
        elif absence is True:
            st.success("Agrees the criterion is genuinely absent from the document.")
        elif absence is False:
            st.error("Disputes the absence — it located evidence the judge missed.")
        else:
            st.caption("Not checked. See the note above the table for why.")

        if criterion.get("verifier_rationale"):
            st.markdown(f"_{criterion['verifier_rationale']}_")
        if criterion.get("absence_evidence"):
            # Re-verified through stage B before it was allowed to escalate, so
            # this is a quote from the document rather than a second assertion.
            st.markdown(f"It located: > {criterion['absence_evidence']}")
