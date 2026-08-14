"""Reviewer interface (build gate 20 step 18).

The gate names four properties: **polls status**, **survives worker restart**,
**worker survives UI restart**, and **no DB driver importable**.

The last one needs care. 3.1 claims decision #11 is "enforced by the dependency
graph, not by discipline" — that is not quite true, because `sqlite3` is in the
Python standard library and cannot be uninstalled. Omitting a driver from the
`ui` extra removes the temptation and the connection string; what actually
enforces the boundary is the import check below (and `test_layering.py` at step
20). Stating this plainly matters more than the claim being tidy.

The client is tested directly rather than through Streamlit's runtime: Streamlit
re-executes its script per interaction and has no useful headless harness, so
driving the widgets would test the framework. What is ours is the HTTP contract
and the error handling, and both are in `api_client`.
"""

import ast
from pathlib import Path
from typing import Any

import httpx
import pytest

from ui.api_client import ApiClient, ApiError

UI_DIR = Path(__file__).resolve().parent.parent / "ui"


@pytest.fixture
def transport_factory() -> Any:  # noqa: ANN401
    def build(handler: Any) -> Any:  # noqa: ANN401
        return httpx.MockTransport(handler)

    return build


@pytest.fixture
def client() -> ApiClient:
    return ApiClient(base_url="http://127.0.0.1:8000", actor="reviewer-1")


def patched(monkeypatch: pytest.MonkeyPatch, handler: Any) -> None:  # noqa: ANN401
    """Route `httpx.request` through a mock transport."""

    def fake_request(method: str, url: str, **kwargs: Any) -> httpx.Response:
        request = httpx.Request(method, url, headers=kwargs.get("headers"))
        return handler(request)

    monkeypatch.setattr("ui.api_client.httpx.request", fake_request)


# --- gate: no database driver reachable from the UI --------------------------


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
    return found


def test_the_ui_imports_no_database_driver_and_no_internals() -> None:
    """Decision #11, checked where it can actually be checked.

    `sqlite3` ships with Python, so no packaging choice can make it absent. This
    is the enforcement — the UI reaches the data only through HTTP, across a
    process boundary, which is what keeps a Streamlit rerun from holding a
    database connection.
    """
    forbidden = ("sqlite3", "screener.storage", "screener.pipeline", "screener.service")

    for source in UI_DIR.glob("*.py"):
        imported = _imports(source)
        for module in imported:
            assert not module.startswith(forbidden), f"{source.name} imports {module}"


def test_the_ui_does_not_import_the_screener_package_at_all() -> None:
    """Stronger than the rule requires, and worth keeping.

    Sharing domain models would make the UI redeployable only in lockstep with
    the server, and would quietly reintroduce a path to the storage layer through
    a transitive import.
    """
    for source in UI_DIR.glob("*.py"):
        assert not any(m.startswith("screener") for m in _imports(source)), source.name


# --- gate: survives the API or worker being restarted ------------------------


def test_a_dead_api_produces_a_sentence_not_a_traceback(
    client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The API is a separate process that gets restarted.

    A stack trace mid-page is useless to a recruiter and indistinguishable from
    a bug in the screening itself.
    """

    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    patched(monkeypatch, refuse)

    with pytest.raises(ApiError, match="Cannot reach the screener API"):
        client.list_positions()


def test_a_slow_api_explains_itself(client: ApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    def stall(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    patched(monkeypatch, stall)

    with pytest.raises(ApiError, match="did not respond"):
        client.run_status("run1")


def test_polling_status_is_a_plain_get(client: ApiClient, monkeypatch: pytest.MonkeyPatch) -> None:
    """Polling, not WebSockets.

    A job updating every few seconds over 78 minutes does not justify a
    persistent connection — and a stateless GET is what lets the UI survive the
    API restarting underneath it.
    """
    seen: list[tuple[str, str]] = []

    def ok(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"run_id": "run1", "status": "running", "done": 3})

    patched(monkeypatch, ok)

    for _ in range(3):
        client.run_status("run1")

    assert seen == [("GET", "/runs/run1/status")] * 3


def test_the_ui_holds_no_worker_state() -> None:
    """The worker survives a UI restart because the UI owns nothing.

    All run state lives in the database behind the API. Streamlit re-executes
    its whole script on every interaction, so anything held here would be lost
    on the next click anyway — which is precisely why screening cannot live in
    this process (2).
    """
    source = (UI_DIR / "screener_app.py").read_text(encoding="utf-8")

    assert "threading" not in source
    assert "subprocess" not in source
    assert "judge_one" not in source


# --- the actor reaches the audit trail --------------------------------------


def test_the_reviewer_identity_is_sent_on_every_call(
    client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Separation of duties has to survive the UI too.

    One person approving a rubric and another signing off the run is the flow an
    audit trail exists to record.
    """
    actors: list[str | None] = []

    def capture(request: httpx.Request) -> httpx.Response:
        actors.append(request.headers.get("X-Actor"))
        return httpx.Response(200, json={"count": 1})

    patched(monkeypatch, capture)

    client.start_run("run1")
    client.rescan_run("run1")

    assert actors == ["reviewer-1", "reviewer-1"]


# --- error translation -------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "expected"),
    [
        (404, "Not found"),
        (409, "has not been approved"),
        (503, "not ready"),
    ],
)
def test_http_errors_become_actionable_sentences(
    client: ApiClient, monkeypatch: pytest.MonkeyPatch, status_code: int, expected: str
) -> None:
    """A recruiter trying to fill a vacancy cannot act on a status code."""
    patched(monkeypatch, lambda request: httpx.Response(status_code, json={"detail": "x"}))

    with pytest.raises(ApiError, match=expected):
        client.list_candidates("run1")


def test_a_no_content_response_is_not_parsed_as_json(
    client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`abort` and `sign-off` return 204. Parsing an empty body would raise."""
    patched(monkeypatch, lambda request: httpx.Response(204))

    assert client.abort_run("run1") is None
    assert client.sign_off("run1") is None


def test_rubric_extraction_gets_a_longer_timeout() -> None:
    """It is a live LLM call. Timing out mid-generation reads as a failure when
    it was only a cold model load."""
    from ui.api_client import DEFAULT_TIMEOUT, EXTRACT_TIMEOUT

    assert EXTRACT_TIMEOUT > DEFAULT_TIMEOUT


# --- presentation rules that carry weight ------------------------------------


def test_the_app_shows_bands_and_keeps_the_score_for_export() -> None:
    """Three verdict levels cannot support a rendered precision of `7.8` (10.6).

    The float is exported for audit; the reviewer sees a band.
    """
    source = (UI_DIR / "views" / "review.py").read_text(encoding="utf-8")

    assert '"Band"' in source
    assert "Export CSV" in source
    assert "exported for audit" in source


def test_needs_review_is_its_own_section() -> None:
    """Never the tail of a ranking.

    At 1,000 applicants a reviewer reads the top of Band A and stops; anything
    at the bottom of one long list is invisible in practice (10.5b).
    """
    source = (UI_DIR / "views" / "review.py").read_text(encoding="utf-8")

    assert "Needs review" in source
    assert "st.expander" in source


def test_the_escalation_rate_is_shown_against_its_budget() -> None:
    """Surfaced live, not discovered afterwards (18.2)."""
    from ui.common import ESCALATION_BUDGET

    assert ESCALATION_BUDGET == 0.03


def test_weak_signals_are_labelled_as_weak() -> None:
    """`POSSIBLE_DUPLICATE` catches byte-identical files only.

    The same CV re-exported from Word has a different hash, so the UI says so
    rather than implying coverage the check does not have (12.6).
    """
    from ui.common import FLAG_HELP

    assert "exact copies" in FLAG_HELP["POSSIBLE_DUPLICATE"]
    assert "not fraud detection" not in FLAG_HELP  # it is stated in the detail view
    assert "not a judgement about the candidate" in FLAG_HELP["SUSPECTED_INJECTION"]


def test_evidence_is_not_presented_as_fraud_detection() -> None:
    """It confirms the model quoted the resume faithfully. Nothing more (1)."""
    source = (UI_DIR / "views" / "review.py").read_text(encoding="utf-8")

    assert "not fraud detection" in source


def test_every_view_module_imports() -> None:
    """The gate that was missing when the pages were split.

    `escalation_meter` was imported from `ui.common` while it lived in
    `ui.views.runs`, so the app raised `ImportError` before rendering a single
    widget — and the full suite stayed green. Nothing else covers this: `mypy`'s
    `files` list is `screener`, `config`, `worker.py`, so `ui/` is unchecked, and
    ruff resolves names within a file but not across modules. Importing each
    module is the cheapest thing that would have caught it.
    """
    import importlib

    for module in (
        "ui.common",
        "ui.api_client",
        "ui.views.requisitions",
        "ui.views.runs",
        "ui.views.review",
        "ui.views.audit",
        "ui.screener_app",
    ):
        importlib.import_module(module)


def test_the_entrypoint_registers_every_page() -> None:
    """A view that exists but is not registered is unreachable in the browser."""
    source = (Path(__file__).resolve().parent.parent / "ui" / "screener_app.py").read_text()
    for page in ("positions_page", "runs_page", "review_page", "audit_page"):
        assert page in source, f"{page} is not registered with st.navigation"


def test_every_recorded_action_renders_as_a_sentence() -> None:
    """A compliance screen must not show raw JSON, or crash on a new action.

    The renderer is a lookup keyed on action, so an action added to the service
    layer later has no entry here. It must degrade to showing the raw row —
    never to a `KeyError` in front of an auditor.
    """
    from ui.views.audit import KNOWN_ACTIONS, describe

    details = {
        "create_position": {"reference": "REQ-1"},
        "extract_rubric": {"criteria": 5, "prompt_hash": "a" * 64, "judge_digest": "b" * 64},
        "save_rubric": {"version": 2, "rubric_hash": "c" * 64},
        "approve_rubric": {"rubric_hash": "c" * 64},
        "create_run": {"folder": "/share/req", "queued": 12, "rubric_id": "rub-1"},
        "start_run": {"queued": 12},
        "advance_phase": {"phase": "verify", "queued": 12},
        "complete_run": {"done": 12, "failed": 0, "escalation_rate": 0.25},
        "decision": {"decision": "reject", "old_score": 3.1, "reason": "Missing the must-have."},
        "sign_off_run": None,
        "rescan_run": {"added": 3},
        "abort_run": {"released": 4},
        "reclaim_orphaned": {"jobs": 2},
        "purge_candidate": {"reason": "erasure request"},
    }
    for action in KNOWN_ACTIONS:
        if not action:
            continue
        rendered = describe({"action": action, "detail": details.get(action)})
        assert rendered and "{" not in rendered, f"{action} rendered as raw data: {rendered}"

    # An action nobody has templated yet, and a malformed detail, both degrade.
    assert "future_action" in describe({"action": "future_action", "detail": {"a": 1}})
    assert describe({"action": "complete_run", "detail": {"escalation_rate": "not-a-number"}})
    # A NULL detail is ordinary: several actions record none at all.
    assert describe({"action": "sign_off_run", "detail": None})


def test_the_decision_reason_is_never_truncated() -> None:
    """That string is the adverse-action justification.

    Shortening it to fit a table is how a rejection stops being explainable to
    the person it affected.
    """
    from ui.views.audit import describe

    reason = "Missing the Kubernetes must-have; " + "verified against the resume. " * 12
    rendered = describe(
        {"action": "decision", "detail": {"decision": "reject", "reason": reason, "old_score": 2.0}}
    )
    assert reason in rendered
