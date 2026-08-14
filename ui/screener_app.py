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
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from ui.common import call, get_client
from ui.views import audit, requisitions, review, runs


def sidebar() -> None:
    """Navigation, identity, and health. **Not a place to operate the app from.**

    The run selectors live on the pages that use a run, so the sidebar does not
    have to answer "which run" on screens where the question is meaningless.
    Removing the `Run id` box also retires the `pending_run_id` hand-off: that
    existed only because a sidebar widget was bound to `run_id`, which made any
    mid-script write to the key raise. With no widget on it, `adopt_run` can set
    it directly from anywhere.
    """
    with st.sidebar.expander("Connection"):
        # Explicit `value` handles the case where the operator clears the box — it
        # falls back to the environment immediately rather than sticking on "".
        url = st.text_input(
            "API URL",
            value=st.session_state.get(
                "api_url", os.environ.get("SCREENER_API_URL", "http://127.0.0.1:8000")
            ),
            key="api_url_input",
            help="The address of the Screener API. Changes take effect on the next interaction.",
        )
        if url:
            st.session_state["api_url"] = url

    actor = st.sidebar.text_input(
        "Reviewing as",
        value=st.session_state.get("actor", "poc-operator"),
        help=(
            "Your identity for the audit log. The PoC uses this instead of a "
            "login screen. Try changing it after approving a rubric to see the "
            "separation of duties enforcement."
        ),
    )
    if actor:
        st.session_state["actor"] = actor

    st.sidebar.divider()
    health = call(get_client().health)
    if not health:
        st.sidebar.error("API unreachable")
    else:
        if not health["model_digest_matches_pin"]:
            st.sidebar.warning("Model digest differs from config pin — reproducibility broken")
        if not health["migrations_current"]:
            st.sidebar.error("Database schema is stale")
        if not health["disk_ok"]:
            st.sidebar.error(f"Disk full ({health['free_disk_gb']} GB free)")
        if health["ok"]:
            st.sidebar.success(f"System healthy (v{health['app_version']})")

    # Stubbed authorisation, at the foot because it is a demonstration control
    # rather than something touched per task (15.2). `X-Actor-Roles` exists so
    # role-scoped exposure is something an operator can *show* working rather
    # than take on trust, and the Audit reads are the first that require it —
    # without this the audit pages would answer 403 to everybody.
    st.sidebar.divider()
    with st.sidebar.expander("Roles"):
        roles = st.multiselect(
            "Roles",
            options=["admin", "auditor"],
            default=st.session_state.get("roles", ["admin"]),
            label_visibility="collapsed",
            help=(
                "`auditor` is required to read the audit log, a run's story, and "
                "a candidate's pre-verification scores."
            ),
        )
    st.session_state["roles"] = roles


def main() -> None:
    st.set_page_config(page_title="Enterprise Talent Screener (ETS)", layout="wide")

    st.title("Enterprise Talent Screener")
    st.caption("On-premise · no data leaves this host")

    sidebar()

    pg = st.navigation(
        [
            st.Page(requisitions.positions_page, title="Requisitions", default=True),
            st.Page(runs.runs_page, title="Runs"),
            st.Page(review.review_page, title="Review"),
            st.Page(audit.audit_page, title="Audit"),
        ]
    )
    pg.run()


if __name__ == "__main__":
    main()
