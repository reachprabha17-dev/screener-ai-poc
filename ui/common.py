import os
import sys
from pathlib import Path
from typing import Any

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ui.api_client import ApiClient, ApiError

DEFAULT_API = os.environ.get("SCREENER_API_URL", "http://127.0.0.1:8000")


ESCALATION_BUDGET = 0.03


PAGE_SIZE = 15


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


ESCALATION_LABELS = {
    "unverified_evidence": "unverified evidence",
    "judge_disagreement": "judge disagreement",
    "absence_found": 'evidence found for an "absent" criterion',
    "negation": "negation suspected",
    "partial_must_have": "partial evidence on a must-have",
    "unprocessable": "could not be processed",
    "suspected_injection": "suspected injection",
}


EVIDENCE_BADGE = {
    "verified": "verified",
    "partial": "partially matched",
    "unverified": "not found in the resume",
    "not_applicable": "",
}


VERIFICATION_BADGE = {
    "done": "verified",
    "pending": "provisional",
    "skipped": "not verified",
}


CONTEXT_CHARS = 240


def get_client() -> ApiClient:
    return ApiClient(
        base_url=st.session_state.get("api_url", DEFAULT_API),
        actor=st.session_state.get("actor", "poc-operator"),
        roles=",".join(st.session_state.get("roles", [])),
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


def adopt_run(widget_key: str) -> None:
    """Publish a run selector's choice as the app-wide run, on user action only.

    **Three selectors write this key, so the write must be an event and not a
    comparison.** Runs, Review and Audit each carry a run picker. A pattern of
    "if my widget disagrees with `run_id`, overwrite it and rerun" made two of
    them fight when they lived in tabs — measured at 1,207 script passes in 150 s
    with the API hit on each one. Pages made that impossible by only executing
    the active one, but `on_change` is also simply the correct trigger: it fires
    when a human moves the widget, not on every render.
    """
    st.session_state["run_id"] = st.session_state[widget_key]


def _humanize(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


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
