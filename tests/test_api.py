"""HTTP control plane (spec 15, build gate 20 step 15).

The gate names three things: **routes ≤4 lines**, **sync handlers**, and
**`TestClient` covers every mutating path**.

The third is the real one, and the last section enumerates the routes from 15.1
so a new endpoint added without a test fails here rather than shipping untested.

Two properties get more attention than the rest, because both fail silently:
`model_verdict` must never leave the building (15.4), and no route may screen a
candidate (15.3) — a batch inside a request would work perfectly on three files
and fall over on a thousand.
"""

import ast
import inspect
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from config.settings import settings
from screener.api import deps as api_deps
from screener.api.app import create_app
from screener.api.routes import candidates, dashboard, health, positions, rubrics, runs
from screener.core.verify_evidence import align
from screener.models import Candidate, Flag, ScoredCriterion
from screener.ports import CacheKey
from screener.service import ScreenerService
from screener.storage import results_store, runs_store
from screener.storage.connection import apply_migrations, close_connection
from screener.storage.uow import UnitOfWork, unit_of_work

JD = "Senior Backend Engineer. Required: 5+ years backend. Kubernetes essential."


class FakeLLM:
    def __init__(self, *, healthy: bool = True) -> None:
        self._healthy = healthy

    def chat_json(
        self, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        if "support_checks" in schema.get("properties", {}):
            # A phase-2 call. Empty is a valid `VerifyOutput`: the verifier
            # agreed with everything and found nothing for the `none` criteria.
            return {"support_checks": [], "absence_checks": []}
        return {
            "criteria": [
                {"text": "5+ years backend", "must_have": True, "weight": 5},
                {"text": "Kubernetes", "must_have": True, "weight": 4},
                {"text": "Python", "must_have": False, "weight": 3},
                {"text": "Mentoring", "must_have": False, "weight": 1},
            ]
        }

    def count_tokens(self, model: str, text: str) -> int:
        return len(text) // 4

    def count_prompt_tokens(self, model: str, system: str, user: str) -> int:
        return 900

    def health(self) -> bool:
        return self._healthy

    def digest(self, model: str) -> str:
        return "sha256:aaa"

    def ensure_loaded(self, model: str) -> None:
        self.loaded = model

    def unload(self, model: str) -> None:
        self.loaded = None


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "screener.db"
    apply_migrations(path)
    monkeypatch.setattr(settings, "db_path", str(path))
    monkeypatch.setattr(settings, "resumes_dir", str(tmp_path / "resumes"))
    return path


@pytest.fixture
def uow_factory(db: Path) -> Iterator[Callable[[], UnitOfWork]]:
    """Thread-local, exactly as production does it.

    A single shared connection would fail the moment FastAPI dispatched a sync
    handler onto its threadpool — `sqlite3` sets `check_same_thread=True` and a
    connection cannot cross threads (12.3). Pinning one in the fixture would
    test a topology the deployment never runs.
    """
    yield unit_of_work
    close_connection()


@pytest.fixture
def service(uow_factory: Callable[[], UnitOfWork]) -> ScreenerService:
    return ScreenerService(llm=FakeLLM(), uow_factory=uow_factory)  # type: ignore[arg-type]


@pytest.fixture
def app(service: ScreenerService) -> Iterator[FastAPI]:
    application = create_app(check_migrations=False)
    application.dependency_overrides[api_deps.get_service] = lambda: service
    yield application
    application.dependency_overrides.clear()


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


def resumes(reference: str = "REQ-1", count: int = 3) -> Path:
    folder = Path(settings.resumes_dir) / reference
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (folder / f"cv{i}.pdf").write_bytes(b"%PDF-1.4\n")
    return folder


def make_position(client: TestClient, reference: str = "REQ-1") -> str:
    response = client.post(
        "/positions",
        json={"reference": reference, "title": "Backend Engineer", "jd_text": JD},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def make_approved_rubric(client: TestClient, position_id: str) -> str:
    rubric = client.post(f"/positions/{position_id}/rubric/extract").json()
    approved = client.post(f"/rubrics/{rubric['id']}/approve")
    assert approved.status_code == 200, approved.text
    return str(rubric["id"])


def make_run(client: TestClient, reference: str = "REQ-1") -> str:
    position_id = make_position(client, reference)
    rubric_id = make_approved_rubric(client, position_id)
    resumes(reference)
    response = client.post("/runs", json={"position_id": position_id, "rubric_id": rubric_id})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


# --- the flow ----------------------------------------------------------------


def test_the_whole_control_plane_flow(client: TestClient) -> None:
    """JD → rubric → approve → run → start, over HTTP."""
    position_id = make_position(client)

    rubric = client.post(f"/positions/{position_id}/rubric/extract").json()
    assert len(rubric["criteria"]) == 4
    assert rubric["approved_at"] is None  # a draft until a human says otherwise

    approved = client.post(f"/rubrics/{rubric['id']}/approve").json()
    assert approved["approved_by"] == settings.dev_actor_id

    resumes(count=3)
    run = client.post("/runs", json={"position_id": position_id, "rubric_id": rubric["id"]}).json()
    assert run["status"] == "pending"
    assert run["judge_digest"] == "sha256:aaa"

    started = client.post(f"/runs/{run['id']}/start")
    assert started.json() == {"count": 3}

    status = client.get(f"/runs/{run['id']}/status").json()
    assert status["total"] == 3
    assert status["eta_seconds"] > 0


def test_get_latest_and_approved_rubric_routes(client: TestClient) -> None:
    position_id = make_position(client, "REQ-ROUTE-TEST")

    res_latest = client.get(f"/positions/{position_id}/rubric")
    assert res_latest.status_code == 200
    assert res_latest.json() is None

    res_app = client.get(f"/positions/{position_id}/rubric/approved")
    assert res_app.status_code == 200
    assert res_app.json() is None

    rubric = client.post(f"/positions/{position_id}/rubric/extract").json()

    res_latest2 = client.get(f"/positions/{position_id}/rubric")
    assert res_latest2.status_code == 200
    assert res_latest2.json()["id"] == rubric["id"]

    res_app2 = client.get(f"/positions/{position_id}/rubric/approved")
    assert res_app2.status_code == 200
    assert res_app2.json() is None

    client.post(f"/rubrics/{rubric['id']}/approve")

    res_app3 = client.get(f"/positions/{position_id}/rubric/approved")
    assert res_app3.status_code == 200
    assert res_app3.json()["id"] == rubric["id"]


def test_list_runs_route(client: TestClient) -> None:
    position_id = make_position(client, "REQ-RUNS-ROUTE")
    rubric = client.post(f"/positions/{position_id}/rubric/extract").json()
    client.post(f"/rubrics/{rubric['id']}/approve")

    run_res = client.post("/runs", json={"position_id": position_id, "rubric_id": rubric["id"]})
    assert run_res.status_code == 201

    runs_res = client.get("/runs")
    assert runs_res.status_code == 200
    data = runs_res.json()
    assert len(data) >= 1
    target = next(r for r in data if r["id"] == run_res.json()["id"])
    assert "created_at" in target
    assert "created_by" in target
    assert "file_count" in target
    assert "escalation_rate" in target


def test_a_run_against_an_unapproved_rubric_is_409(client: TestClient) -> None:
    """Well-formed request, forbidden state — not a 400."""
    position_id = make_position(client)
    rubric = client.post(f"/positions/{position_id}/rubric/extract").json()
    resumes()

    response = client.post("/runs", json={"position_id": position_id, "rubric_id": rubric["id"]})

    assert response.status_code == 409
    assert "approved" in response.json()["detail"]


def test_unknown_ids_are_404_not_500(client: TestClient) -> None:
    for url in ("/runs/nope/status", "/runs/nope/candidates", "/candidates/999"):
        assert client.get(url).status_code == 404, url


def test_a_rule_violation_is_400(client: TestClient) -> None:
    run_id = make_run(client)
    client.post(f"/runs/{run_id}/start")
    # An empty run cannot be started — the folder had no eligible files.
    other = make_position(client, "REQ-EMPTY")
    rubric_id = make_approved_rubric(client, other)
    resumes("REQ-EMPTY", count=0)
    empty = client.post("/runs", json={"position_id": other, "rubric_id": rubric_id}).json()

    response = client.post(f"/runs/{empty['id']}/start")

    assert response.status_code == 400


def test_malformed_input_is_rejected_by_the_schema(client: TestClient) -> None:
    assert (
        client.post("/positions", json={"reference": "", "title": "t", "jd_text": "j"}).status_code
        == 422
    )
    assert (
        client.post(
            "/positions", json={"reference": "../../etc", "title": "t", "jd_text": "j"}
        ).status_code
        == 422
    )
    assert client.post("/positions", json={"reference": "r", "title": "t"}).status_code == 422
    # extra="forbid" — an unexpected field is a caller bug worth surfacing.
    assert (
        client.post(
            "/positions",
            json={"reference": "r", "title": "t", "jd_text": "j", "extra": "x"},
        ).status_code
        == 422
    )


def test_list_resume_folders_route(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resumes_dir = tmp_path / "resumes"
    resumes_dir.mkdir()
    (resumes_dir / "folder_a").mkdir()
    (resumes_dir / "folder_a" / "cv1.pdf").write_bytes(b"%PDF-1.4 test")
    (resumes_dir / "folder_a" / "cv2.pdf").write_bytes(b"%PDF-1.4 test")
    (resumes_dir / "folder_b").mkdir()

    monkeypatch.setattr(settings, "resumes_dir", str(resumes_dir))

    res = client.get("/positions/folders")
    assert res.status_code == 200
    data = res.json()["folders"]
    assert len(data) == 2
    assert data[0] == {
        "name": "folder_a",
        "path": "folder_a",
        "file_count": 2,
        "has_subfolders": False,
    }
    assert data[1] == {
        "name": "folder_b",
        "path": "folder_b",
        "file_count": 0,
        "has_subfolders": False,
    }


def test_list_resume_folders_route_nonexistent_dir(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "resumes_dir", str(tmp_path / "nonexistent"))
    res = client.get("/positions/folders")
    assert res.status_code == 200
    assert res.json() == {"folders": [], "total": 0}


def test_the_folder_browser_descends_one_level_at_a_time(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`?path=` walks the share so a nested folder can be reached and screened."""
    resumes_dir = tmp_path / "resumes"
    nested = resumes_dir / "2026" / "engineering"
    nested.mkdir(parents=True)
    (nested / "cv.pdf").write_bytes(b"%PDF-1.4 test")
    monkeypatch.setattr(settings, "resumes_dir", str(resumes_dir))

    top = client.get("/positions/folders").json()["folders"]
    assert top == [
        {"name": "2026", "path": "2026", "file_count": 1, "has_subfolders": True},
    ]

    inner = client.get("/positions/folders", params={"path": "2026"}).json()["folders"]
    assert inner == [
        {
            "name": "engineering",
            "path": "2026/engineering",
            "file_count": 1,
            "has_subfolders": False,
        },
    ]

    # And the nested path is accepted as a reference, so the browse result is usable.
    created = client.post(
        "/positions",
        json={"reference": "2026/engineering", "title": "t", "jd_text": "j"},
    )
    assert created.status_code == 201


def test_the_folder_browser_pages_and_filters_a_large_share(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A share with hundreds of folders must not render as one flat list.

    Counting a folder's resumes is a recursive walk, so an unpaged listing costs
    thousands of filesystem operations per render — seconds on a network mount,
    repeated on every Streamlit interaction.
    """
    resumes_dir = tmp_path / "resumes"
    for i in range(40):
        team = "engineering" if i % 2 else "finance"
        (resumes_dir / f"req-{i:03d}-{team}").mkdir(parents=True)
    monkeypatch.setattr(settings, "resumes_dir", str(resumes_dir))

    first = client.get("/positions/folders", params={"limit": 15}).json()
    assert first["total"] == 40
    assert len(first["folders"]) == 15
    assert first["folders"][0]["name"] == "req-000-finance"

    second = client.get("/positions/folders", params={"limit": 15, "offset": 15}).json()
    assert second["total"] == 40
    assert [f["name"] for f in second["folders"]] != [f["name"] for f in first["folders"]]

    # The tail page is short, and `total` is what tells a client it is the last.
    last = client.get("/positions/folders", params={"limit": 15, "offset": 30}).json()
    assert len(last["folders"]) == 10

    # Filtering narrows `total`, not just the page — otherwise paging is wrong.
    filtered = client.get("/positions/folders", params={"q": "engineering"}).json()
    assert filtered["total"] == 20
    assert all("engineering" in f["name"] for f in filtered["folders"])

    # Case-insensitive, because a recruiter typing a folder name will not match case.
    assert client.get("/positions/folders", params={"q": "ENGINEERING"}).json()["total"] == 20


def test_the_folder_browser_caps_the_page_size(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A caller cannot ask for the whole share and reintroduce the cost."""
    resumes_dir = tmp_path / "resumes"
    for i in range(120):
        (resumes_dir / f"req-{i:03d}").mkdir(parents=True)
    monkeypatch.setattr(settings, "resumes_dir", str(resumes_dir))

    page = client.get("/positions/folders", params={"limit": 10_000}).json()
    assert page["total"] == 120
    assert len(page["folders"]) == 100


def test_the_folder_browser_refuses_to_leave_the_share(
    client: TestClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The picker constrains choices; the API accepts whatever is posted to it.

    A traversal must list as empty rather than exposing the host filesystem to
    anything that can reach the port.
    """
    resumes_dir = tmp_path / "resumes"
    (resumes_dir / "real").mkdir(parents=True)
    (tmp_path / "secret").mkdir()
    (tmp_path / "secret" / "private.pdf").write_bytes(b"%PDF-1.4 secret")
    monkeypatch.setattr(settings, "resumes_dir", str(resumes_dir))

    for attempt in ["../secret", "..", "/etc", "real/../../secret", "..\\secret"]:
        res = client.get("/positions/folders", params={"path": attempt})
        assert res.status_code == 200, attempt
        assert res.json() == {"folders": [], "total": 0}, attempt


# --- gate: model_verdict never leaves the building (15.4) -------------------


def test_the_candidate_response_omits_model_verdict(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Audit data, not reviewer data.

    A screen showing both "none" and "the model originally said strong" invites
    the second-guessing 10.5(a) exists to remove, and it is a number nobody can
    defend in an adverse-action conversation.
    """
    run_id, candidate_id = _seed_scored_candidate(client, service, uow_factory)

    listed = client.get(f"/runs/{run_id}/candidates").json()
    single = client.get(f"/candidates/{candidate_id}").json()

    assert "model_verdict" not in single["criteria"][0]
    for partition in ("meets_must_haves", "missing_must_have", "needs_review"):
        for candidate in listed[partition]:
            for criterion in candidate["criteria"]:
                assert "model_verdict" not in criterion


def test_the_response_keeps_what_a_reviewer_needs_to_disagree(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Withholding diagnostics must not mean withholding the evidence.

    Whether oversight is real is a function of whether this screen shows enough
    to disagree (15). The quote, what it was measured against, and how far to
    trust it all have to survive the trim.
    """
    _, candidate_id = _seed_scored_candidate(client, service, uow_factory)

    criterion = client.get(f"/candidates/{candidate_id}").json()["criteria"][0]

    assert criterion["evidence"]
    assert criterion["text"], "the criterion the verdict answers is not shown"
    assert criterion["evidence_status"] == "verified"
    assert criterion["highlights"], "nothing to locate the quote in the resume"
    assert criterion["weight"] == 5  # rejoined from the rubric
    assert criterion["must_have"] is True


def test_raw_diagnostics_are_withheld_from_a_recruiter_and_shown_to_an_auditor(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """15.2, as a table with two rows.

    `match_ratio` is withheld because a bare `0.42` is not actionable — the
    highlights are, and they answer the question the number provokes.
    `model_verdict` is withheld because "the model said strong, we corrected it
    to none" invites second-guessing a correction made on unambiguous grounds.
    Both are audit data, and an auditor is exactly who should have them.
    """
    _, candidate_id = _seed_scored_candidate(client, service, uow_factory)
    withheld = ("match_ratio", "longest_span", "model_verdict", "support", "absence_confirmed")

    recruiter = client.get(
        f"/candidates/{candidate_id}", headers={"X-Actor-Roles": "recruiter"}
    ).json()
    auditor = client.get(f"/candidates/{candidate_id}", headers={"X-Actor-Roles": "auditor"}).json()

    for field in withheld:
        assert field not in recruiter["criteria"][0], field
        assert field in auditor["criteria"][0], field
    assert "sent_text" not in recruiter
    assert "sent_text" in auditor, "the auditor cannot see what the model actually read"


def test_candidates_come_back_in_three_partitions(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """`needs_review` is its own list, so a UI cannot bury it under a ranking."""
    run_id, _ = _seed_scored_candidate(client, service, uow_factory)

    body = client.get(f"/runs/{run_id}/candidates").json()

    assert set(body) == {
        "meets_must_haves",
        "missing_must_have",
        "needs_review",
        "escalation_rate",
    }


def test_an_unscoreable_candidate_reports_null_not_zero(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Over the wire too. `0.0` would rank an unread resume among weak ones."""
    run_id, _ = _seed_scored_candidate(client, service, uow_factory, unscoreable=True)

    review = client.get(f"/runs/{run_id}/candidates").json()["needs_review"]

    assert review[0]["score"] is None
    assert review[0]["band"] is None


# --- actor threading over HTTP ----------------------------------------------


def test_the_actor_header_reaches_the_audit_log(
    client: TestClient, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Separation of duties has to survive the HTTP boundary.

    A stub that hardcoded one identity would make the two-person flow
    untestable, which is the flow an audit trail exists to record.
    """
    from screener.storage import audit_store

    run_id = make_run(client)
    client.post(f"/runs/{run_id}/start")

    response = client.post(f"/runs/{run_id}/sign-off", headers={"X-Actor": "second-reviewer"})
    assert response.status_code == 204

    with uow_factory() as tx:
        entry = next(e for e in audit_store.recent(tx, 50) if e["action"] == "sign_off_run")
    assert entry["actor_id"] == "second-reviewer"


def test_an_absent_header_falls_back_to_the_dev_actor(client: TestClient) -> None:
    position = client.post(
        "/positions", json={"reference": "REQ-9", "title": "t", "jd_text": JD}
    ).json()

    assert position["created_by"] == settings.dev_actor_id


# --- gate: every mutating path is exercised ---------------------------------


def test_decision_and_purge(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    run_id, candidate_id = _seed_scored_candidate(client, service, uow_factory)

    decision = client.post(
        f"/candidates/{candidate_id}/decision",
        json={"decision": "advance", "reason": "strong payments background"},
    )
    assert decision.status_code == 204

    purge = client.delete("/candidates/sha-qualified")
    assert purge.status_code == 200
    assert purge.json() == {"count": 0}  # no trace files existed


def test_a_decision_without_a_reason_is_rejected(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    _, candidate_id = _seed_scored_candidate(client, service, uow_factory)

    response = client.post(
        f"/candidates/{candidate_id}/decision", json={"decision": "advance", "reason": ""}
    )

    assert response.status_code == 422


def test_an_invented_decision_is_rejected(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    _, candidate_id = _seed_scored_candidate(client, service, uow_factory)

    response = client.post(
        f"/candidates/{candidate_id}/decision", json={"decision": "teleport", "reason": "why"}
    )

    assert response.status_code == 422


def test_a_bulk_decision_names_the_ones_it_skipped(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Bulk excludes `review_required`, and says which ones (15.5)."""
    run_id, decidable = _seed_scored_candidate(client, service, uow_factory)
    escalated = _add_escalated_candidate(uow_factory, run_id)

    body = client.post(
        "/candidates/decisions",
        json={
            "candidate_ids": [decidable, escalated],
            "decision": "reject",
            "reason": "does not meet the Python must-have",
        },
    ).json()

    assert body["decided"] == [decidable]
    assert body["skipped"] == [escalated]


def test_a_stale_rubric_save_is_a_conflict_not_a_bad_request(client: TestClient) -> None:
    """409: the body is fine, the world moved. A 400 would read as "you sent
    something wrong" to someone whose only mistake was having the page open."""
    position_id = make_position(client)
    draft = client.post(f"/positions/{position_id}/rubric/extract").json()
    body = {"criteria": draft["criteria"], "base_version": draft["version"]}

    assert client.put(f"/positions/{position_id}/rubric", json=body).status_code == 201
    assert client.put(f"/positions/{position_id}/rubric", json=body).status_code == 409


def test_the_original_document_is_streamed_not_buffered(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """`FileResponse`, so four reviewers opening 5 MB PDFs do not land in the
    API's heap at once."""
    _, candidate_id = _seed_scored_candidate(client, service, uow_factory)
    with uow_factory() as tx:
        run_id = results_store.get(tx, candidate_id).run_id  # type: ignore[union-attr]
        run = runs_store.get(tx, run_id)
    assert run is not None
    Path(run.folder).mkdir(parents=True, exist_ok=True)
    Path(run.folder, "asha_nair.pdf").write_bytes(b"%PDF-1.4\nreal bytes\n")

    response = client.get(f"/candidates/{candidate_id}/file")

    assert response.status_code == 200
    assert response.content.startswith(b"%PDF")
    assert "asha_nair.pdf" in response.headers["content-disposition"]


def test_a_missing_original_document_is_a_404_not_a_500(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    _, candidate_id = _seed_scored_candidate(client, service, uow_factory)

    assert client.get(f"/candidates/{candidate_id}/file").status_code == 404


def test_rescan_abort_and_save_rubric(client: TestClient) -> None:
    position_id = make_position(client)
    rubric_id = make_approved_rubric(client, position_id)
    folder = resumes(count=2)
    run = client.post("/runs", json={"position_id": position_id, "rubric_id": rubric_id}).json()

    (folder / "late.pdf").write_bytes(b"%PDF-1.4\n")
    assert client.post(f"/runs/{run['id']}/rescan").json() == {"count": 1}

    assert client.post(f"/runs/{run['id']}/abort").status_code == 204
    assert client.get(f"/runs/{run['id']}/status").json()["status"] == "aborted"

    edited = client.put(
        f"/positions/{position_id}/rubric",
        json={
            "criteria": [
                {"id": "C1", "text": "8+ years backend", "must_have": True, "weight": 5},
                {"id": "C2", "text": "Kubernetes", "must_have": True, "weight": 4},
                {"id": "C3", "text": "Go", "must_have": False, "weight": 2},
                {"id": "C4", "text": "Mentoring", "must_have": False, "weight": 1},
            ]
        },
    )
    assert edited.status_code == 201
    assert edited.json()["version"] == 2  # a new version, never an in-place edit


def test_list_positions(client: TestClient) -> None:
    make_position(client, "REQ-A")
    make_position(client, "REQ-B")

    assert len(client.get("/positions").json()) == 2


MUTATING_ROUTES = {
    ("POST", "/positions"),
    ("POST", "/positions/{position_id}/rubric/extract"),
    ("PUT", "/positions/{position_id}/rubric"),
    ("POST", "/rubrics/{rubric_id}/approve"),
    ("POST", "/runs"),
    ("POST", "/runs/{run_id}/start"),
    ("POST", "/runs/{run_id}/rescan"),
    ("POST", "/runs/{run_id}/abort"),
    ("POST", "/runs/{run_id}/sign-off"),
    ("POST", "/candidates/{candidate_id}/decision"),
    ("POST", "/candidates/decisions"),
    ("DELETE", "/candidates/{file_sha256}"),
}


def _flatten_routes(routes: object) -> list[Any]:
    """Every route the app actually serves, including nested routers.

    FastAPI 0.141 stopped flattening `include_router` into `app.routes` — an
    included router now appears as a single `_IncludedRouter` holding its own
    routes. Reading the top level alone silently yields nothing, which would
    make the tripwire below pass while checking an empty set.
    """
    flat: list[Any] = []
    for route in routes:  # type: ignore[attr-defined]
        # `_IncludedRouter` exposes the router it wrapped as `original_router`;
        # a plain `Mount` exposes `.routes`. Neither is the flat list older
        # FastAPI produced.
        nested = getattr(getattr(route, "original_router", None), "routes", None) or getattr(
            route, "routes", None
        )
        if nested:
            flat.extend(_flatten_routes(nested))
        else:
            flat.append(route)
    return flat


def test_every_mutating_route_is_declared_here(app: FastAPI) -> None:
    """The gate, as a tripwire.

    A new mutating endpoint added without a test fails this immediately, rather
    than shipping unexercised behind a plausible-looking handler.
    """
    live = {
        (method, route.path)
        for route in _flatten_routes(app.routes)
        for method in (getattr(route, "methods", None) or set())
        if method in ("POST", "PUT", "DELETE", "PATCH")
    }

    assert live, "no routes discovered — the flattener is not finding them"
    assert live == MUTATING_ROUTES


def test_every_mutating_route_requires_an_actor() -> None:
    """15.2: `actor` is threaded from day one, on every mutation.

    Retrofitting the plumbing is the expensive part, not the authentication.
    """
    for module in (positions, rubrics, runs, candidates):
        for route in module.router.routes:
            methods = getattr(route, "methods", set())
            if not methods & {"POST", "PUT", "DELETE", "PATCH"}:
                continue
            params = inspect.signature(route.endpoint).parameters  # type: ignore[attr-defined]
            assert "actor" in params, f"{methods} {route.path}"  # type: ignore[attr-defined]


# --- gate: sync handlers, thin handlers (15.3, 15.4) ----------------------


def test_no_handler_is_async() -> None:
    """`sqlite3` is synchronous.

    `async def` with a blocking database call inside blocks the event loop and
    produces something slower than the sync version while looking more
    sophisticated.
    """
    for module in (positions, rubrics, runs, candidates, health, dashboard):
        for route in module.router.routes:
            endpoint = route.endpoint  # type: ignore[attr-defined]
            assert not inspect.iscoroutinefunction(endpoint), endpoint.__name__


def test_handlers_stay_thin() -> None:
    """Parse, authorize, delegate, serialize — roughly four lines.

    A fat handler means a business rule has leaked out of `service.py`, where
    the CLI and the worker can no longer reach it. The bound is generous; the
    property is that it does not drift.
    """
    for module in (positions, rubrics, runs, candidates, health, dashboard):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[arg-type]
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            body = [n for n in node.body if not isinstance(n, ast.Expr)]  # drop docstrings
            assert len(body) <= 6, f"{module.__name__}.{node.name} has {len(body)} statements"


def test_no_route_module_can_screen_a_candidate() -> None:
    """15.3: screening never runs in a request lifecycle.

    It would work perfectly on three files and fall over on a thousand, losing
    orphan reclaim and resumption on the way.

    Checked against the **import graph**, not the source text. A substring search
    trips on documentation *about* the rule — `runs.py`'s own docstring explains
    why `BackgroundTasks` is forbidden, and the first version of this test failed
    on that sentence. A test that cannot tell an explanation from a violation
    will eventually be silenced rather than fixed.
    """
    forbidden = ("screener.pipeline", "screener.intake")

    for module in (positions, rubrics, runs, candidates, health, dashboard):
        tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))  # type: ignore[arg-type]
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)

        assert not any(m.startswith(forbidden) for m in imported), module.__name__
        assert "fastapi.BackgroundTasks" not in imported
        assert "BackgroundTasks" not in {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }, module.__name__


# --- ops ---------------------------------------------------------------------


def test_health_and_ready(client: TestClient) -> None:
    body = client.get("/health").json()
    assert body["llm_reachable"] is True
    assert body["migrations_current"] is True

    ready = client.get("/ready")
    assert ready.status_code in (200, 503)


def test_ready_reports_503_when_the_model_is_gone(
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    """So `systemctl` or a load balancer can act on it, rather than a log line."""
    broken = ScreenerService(llm=FakeLLM(healthy=False), uow_factory=uow_factory)  # type: ignore[arg-type]
    app = create_app(check_migrations=False)
    app.dependency_overrides[api_deps.get_service] = lambda: broken

    with TestClient(app) as test_client:
        response = test_client.get("/ready")

    assert response.status_code == 503
    assert response.json()["ok"] is False


def test_responses_are_not_cached(client: TestClient) -> None:
    """A proxy holding a ranked list of applicants is candidate data outside
    `data/`, where the erasure path cannot reach it."""
    assert client.get("/positions").headers["cache-control"] == "no-store"


# --- helpers -----------------------------------------------------------------


def _seed_scored_candidate(
    client: TestClient,
    service: ScreenerService,
    uow_factory: Callable[[], UnitOfWork],
    *,
    unscoreable: bool = False,
) -> tuple[str, int]:
    run_id = make_run(client)
    with uow_factory() as tx:
        run = runs_store.get(tx, run_id)
    assert run is not None

    evidence = "Senior Backend Engineer with 7 years of experience"
    resume_text = f"Asha Nair. {evidence}. Led the payments platform at a retail bank."
    # Blocks are computed rather than written by hand so the fixture cannot drift
    # into offsets that no aligner would ever produce.
    alignment = align(evidence, resume_text)

    candidate = Candidate(
        run_id=run_id,
        filename="asha_nair.pdf",
        file_sha256="sha-qualified",
        resume_text=resume_text,
        sent_text=resume_text,  # no redaction in this fixture: identity offsets
        score=None if unscoreable else 7.8,
        band=None if unscoreable else "A",
        must_haves_met=not unscoreable,
        criteria=[
            ScoredCriterion(
                id="C1",
                verdict="none" if unscoreable else "strong",
                model_verdict="strong",  # the field that must not leak
                evidence=evidence,
                verified=not unscoreable,
                match_ratio=1.0,
                longest_span=8,
                match_blocks=[] if unscoreable else alignment.blocks,
                weight=1,
                must_have=False,
            )
        ],
        scoreable=not unscoreable,
        review_required=unscoreable,
        flags=[Flag.EVIDENCE_UNVERIFIED] if unscoreable else [],
        # A candidate that has been all the way through both phases. `pending`
        # is the *provisional* state and is excluded from bulk decisions and
        # sign-off, so seeding it here would silently test the wrong rule.
        verification_status="skipped" if unscoreable else "done",
    )
    key = CacheKey(
        file_sha256="sha-qualified",
        position_id=run.position_id,
        rubric_hash="r" * 64,
        judge_digest="sha256:aaa",
        prompt_hash="p" * 64,
        redaction_on=True,
        num_ctx=settings.num_ctx,
        app_version="v0.1.0",
    )
    with uow_factory() as tx:
        candidate_id = results_store.save(tx, run_id, candidate, key)
    return run_id, candidate_id


def _add_escalated_candidate(uow_factory: Callable[[], UnitOfWork], run_id: str) -> int:
    """A second candidate in the *same* run, flagged for review.

    Seeding a second run would need a second position, and `positions.reference`
    is unique — the bulk rule is about two candidates side by side anyway.
    """
    with uow_factory() as tx:
        run = runs_store.get(tx, run_id)
        assert run is not None
        candidate = Candidate(
            run_id=run_id,
            filename="unreadable.pdf",
            file_sha256="sha-escalated",
            criteria=[],
            scoreable=False,
            review_required=True,
            flags=[Flag.EVIDENCE_UNVERIFIED],
            verification_status="skipped",
        )
        return results_store.save(
            tx,
            run_id,
            candidate,
            CacheKey(
                file_sha256="sha-escalated",
                position_id=run.position_id,
                rubric_hash="r" * 64,
                judge_digest="sha256:aaa",
                prompt_hash="p" * 64,
                redaction_on=True,
                num_ctx=settings.num_ctx,
                app_version="v0.1.0",
            ),
        )


def test_audit_log_requires_auditor_role(client: TestClient) -> None:
    response = client.get("/audit", headers={"X-Actor": "poc-operator", "X-Actor-Roles": "admin"})
    assert response.status_code == 403


def test_audit_log_returns_paginated_results(client: TestClient) -> None:
    client.post(
        "/positions",
        json={"reference": "ref-1", "title": "T", "jd_text": "J"},
        headers={"X-Actor": "u-1", "X-Actor-Roles": "auditor,admin"},
    )

    response = client.get(
        "/audit?action=create_position&limit=1",
        headers={"X-Actor": "u-1", "X-Actor-Roles": "auditor"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["total"] >= 1
    assert len(data["entries"]) == 1
    assert data["entries"][0]["action"] == "create_position"


def test_audit_log_caps_limit_at_200(client: TestClient) -> None:
    response = client.get(
        "/audit?limit=5000", headers={"X-Actor": "u-1", "X-Actor-Roles": "auditor"}
    )
    assert response.status_code == 200
    # Capped at 200 means it won't crash or complain, but returns at most 200 items.
    assert len(response.json()["entries"]) <= 200


def test_the_run_story_requires_the_auditor_role(client: TestClient) -> None:
    """Same gate as `GET /audit`: it returns decision reasons and who gave them.

    A control that depends on which URL the facts arrive through is a routing
    detail, not a control.
    """
    run_id = make_run(client)

    refused = client.get(f"/runs/{run_id}/story")
    assert refused.status_code == 403

    allowed = client.get(f"/runs/{run_id}/story", headers={"X-Actor-Roles": "auditor"})
    assert allowed.status_code == 200
    story = allowed.json()
    assert story["run_id"] == run_id
    assert story["position_reference"] == "REQ-1"
    assert [e["action"] for e in story["events"]], "the story carried no events"

    missing = client.get("/runs/run-nope/story", headers={"X-Actor-Roles": "auditor"})
    assert missing.status_code == 404


def test_the_decision_record_requires_the_auditor_role(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """It carries `model_verdict` and the stated grounds — the auditor surface.

    15.4 keeps `model_verdict` off the reviewer's screen deliberately; the role
    gate is what keeps this route from becoming a way around that.
    """
    run_id, candidate_id = _seed_scored_candidate(client, service, uow_factory)
    client.post(
        f"/candidates/{candidate_id}/decision",
        json={"decision": "reject", "reason": "Missing the must-have."},
    )

    refused = client.get(f"/candidates/{candidate_id}/record")
    assert refused.status_code == 403

    allowed = client.get(f"/candidates/{candidate_id}/record", headers={"X-Actor-Roles": "auditor"})
    assert allowed.status_code == 200
    record = allowed.json()
    assert record["decision"] == "reject"
    assert record["history"][0]["reason"] == "Missing the must-have."
    assert record["run_id"] == run_id
    # The pre-gate verdict is present here and nowhere a recruiter can reach.
    assert all("model_verdict" in c for c in record["criteria"])

    missing = client.get("/candidates/999999/record", headers={"X-Actor-Roles": "auditor"})
    assert missing.status_code == 404


def test_the_recruiter_candidate_view_still_hides_model_verdict(client: TestClient) -> None:
    """The record route must not have widened the reviewer surface by accident."""
    run_id = make_run(client)
    listed = client.get(f"/runs/{run_id}/candidates").json()
    for group in ("needs_review", "meets_must_haves", "missing_must_have"):
        for candidate in listed[group]:
            for criterion in candidate["criteria"]:
                assert "model_verdict" not in criterion


def test_the_record_carries_both_the_judge_and_the_verifier(
    client: TestClient, service: ScreenerService, uow_factory: Callable[[], UnitOfWork]
) -> None:
    """Phase 2's reading has to be legible whatever it concluded.

    `CriterionView.verifier` is populated only on disagreement, by design — a
    panel that always appears becomes furniture. That makes silence ambiguous on
    an audit surface between "the second model agreed", "it was skipped" and "it
    has not run", so the record carries the verifier's fields outright and states
    `verification_status` alongside them.
    """
    _run_id, candidate_id = _seed_scored_candidate(client, service, uow_factory)
    client.post(
        f"/candidates/{candidate_id}/decision",
        json={"decision": "reject", "reason": "Missing the must-have."},
    )

    record = client.get(
        f"/candidates/{candidate_id}/record", headers={"X-Actor-Roles": "auditor"}
    ).json()

    assert record["verification_status"] in {"pending", "done", "skipped"}
    for criterion in record["criteria"]:
        # The judge's two verdicts: what it said, and what stood after the gate.
        assert "model_verdict" in criterion
        assert "verdict" in criterion
        # Stage B: was the quote actually found in the document.
        assert "verified" in criterion
        assert "match_ratio" in criterion
        # Phase 2, present whether or not it disagreed.
        for field in (
            "support",
            "suggested_verdict",
            "verifier_rationale",
            "absence_confirmed",
            "absence_evidence",
        ):
            assert field in criterion, field


# --- dashboard ---------------------------------------------------------------


def test_dashboard_counts_nothing_on_an_empty_system(client: TestClient) -> None:
    """Zeroes, not an error and not an empty body.

    The first thing anyone sees is this screen on a system with nothing in it.
    """
    body = client.get("/dashboard").json()

    assert body == {
        "open_positions": 0,
        "applications": 0,
        "awaiting_review": 0,
        "runs_in_progress": 0,
        "unscreened_files": 0,
        "queues": [],
    }


def test_dashboard_counts_open_requisitions_and_queued_files(client: TestClient) -> None:
    make_position(client, "REQ-A")
    run_id = make_run(client, "REQ-B")
    client.post(f"/runs/{run_id}/start")

    body = client.get("/dashboard").json()

    assert body["open_positions"] == 2
    # Snapshotted and queued, but not yet judged: they are applications the
    # system has received and has not read, which is a different fact from an
    # application it has assessed.
    assert body["unscreened_files"] == 3
    assert body["applications"] == 0
    assert body["runs_in_progress"] == 1


def test_dashboard_review_queue_matches_the_sign_off_gate(
    client: TestClient,
    service: ScreenerService,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    """The count and the refusal have to agree.

    A screen that reads zero while the sign-off button answers 400 teaches
    reviewers to distrust the screen, so both read the same predicate: undecided,
    and either escalated or not yet verified.
    """
    run_id, _ = _seed_scored_candidate(client, service, uow_factory)
    _add_escalated_candidate(uow_factory, run_id)

    body = client.get("/dashboard").json()
    assert body["awaiting_review"] == 1
    assert body["applications"] == 2

    refused = client.post(f"/runs/{run_id}/sign-off")
    assert refused.status_code == 400
    assert "1 candidate(s) need review" in refused.json()["detail"]


def test_dashboard_names_the_runs_holding_the_queue(
    client: TestClient,
    service: ScreenerService,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    """A total alone is not actionable — review happens inside a run."""
    run_id, _ = _seed_scored_candidate(client, service, uow_factory)
    _add_escalated_candidate(uow_factory, run_id)

    queues = client.get("/dashboard").json()["queues"]

    assert len(queues) == 1
    assert queues[0]["run_id"] == run_id
    assert queues[0]["position_reference"] == "REQ-1"
    assert queues[0]["awaiting_review"] == 1


def test_dashboard_queue_empties_when_the_candidate_is_decided(
    client: TestClient,
    service: ScreenerService,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    run_id, _ = _seed_scored_candidate(client, service, uow_factory)
    escalated_id = _add_escalated_candidate(uow_factory, run_id)

    client.post(
        f"/candidates/{escalated_id}/decision",
        json={"decision": "reject", "reason": "unreadable document, asked for a resend"},
    )

    body = client.get("/dashboard").json()
    assert body["awaiting_review"] == 0
    assert body["queues"] == []
    # The application does not stop existing because somebody answered for it.
    assert body["applications"] == 2


def test_dashboard_counts_an_application_once_per_requisition(
    client: TestClient,
    service: ScreenerService,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    """Re-screening is not a new applicant.

    Rescanning after a parser failure writes a second candidate row for the same
    file. Counting rows would report that as an application arriving.
    """
    run_id, _ = _seed_scored_candidate(client, service, uow_factory)
    with uow_factory() as tx:
        run = runs_store.get(tx, run_id)
        assert run is not None
        results_store.save(
            tx,
            run_id,
            Candidate(
                run_id=run_id,
                filename="asha_nair.pdf",
                file_sha256="sha-qualified",  # the same file, screened again
                criteria=[],
                scoreable=True,
                verification_status="done",
            ),
            CacheKey(
                file_sha256="sha-qualified",
                position_id=run.position_id,
                rubric_hash="r" * 64,
                judge_digest="sha256:aaa",
                prompt_hash="p" * 64,
                redaction_on=True,
                num_ctx=settings.num_ctx,
                app_version="v0.1.0",
            ),
        )

    assert client.get("/dashboard").json()["applications"] == 1


def test_dashboard_writes_no_audit_row(
    client: TestClient,
    service: ScreenerService,
    uow_factory: Callable[[], UnitOfWork],
) -> None:
    """Reading counts is not a mutation.

    A row per dashboard visit would bury the decisions an auditor is looking for
    under refreshes — the same reasoning that keeps `search_audit` silent.
    """
    make_position(client, "REQ-AUDIT")
    before = client.get("/audit", headers={"X-Actor-Roles": "auditor"}).json()["total"]

    client.get("/dashboard")
    client.get("/dashboard")

    after = client.get("/audit", headers={"X-Actor-Roles": "auditor"}).json()["total"]
    assert after == before
