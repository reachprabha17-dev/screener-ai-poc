"""Command line interface (spec 14, build gate 20 step 16).

The gate is one sentence: **run a batch with the API stopped**. That is the last
test in this file, and it drives the whole flow — migrate, seed, position,
rubric, approve, run, screen, export — with no HTTP client involved at all.

The property underneath it matters more than the commands. A break-glass path
that reached the stores directly would bypass the transaction boundaries and the
audit log, so the one occasion you most need a record of who did what — under
pressure, out of hours, with something already broken — is exactly when there
would not be one. Every mutating command goes through `service.py`, and the
tests below check the audit rows rather than the console output.
"""

import ast
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from config.settings import settings
from screener.cli import app
from screener.storage import audit_store, results_store
from screener.storage.connection import dispose_engine
from screener.storage.uow import unit_of_work

CLI_SOURCE = Path(__file__).resolve().parent.parent / "screener" / "cli.py"

JD = (
    "Senior Backend Engineer. Required: 5+ years backend services. Strong Python. "
    "Kubernetes is essential. Preferred: Go, mentoring."
)

RESUME = (
    "Asha Nair. Senior Backend Engineer, 7 years. Led the migration of a payments "
    "monolith to microservices in Go. Owned the Kubernetes platform for 12 services. "
    "Built REST APIs in Python and Django with PostgreSQL."
)

EVIDENCE = "Senior Backend Engineer, 7 years"


class FakeLLM:
    """Stands in for Ollama so the CLI flow runs without a GPU."""

    def chat_json(
        self, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> dict[str, Any]:
        if "support_checks" in schema.get("properties", {}):
            # A phase-2 call. Empty is a valid `VerifyOutput`: the verifier
            # agreed with everything and found nothing for the `none` criteria.
            return {"support_checks": [], "absence_checks": []}
        if "CRITERIA:" not in user:
            return {
                "criteria": [
                    {"text": "5+ years backend services", "must_have": True, "weight": 5},
                    {"text": "Strong Python", "must_have": True, "weight": 4},
                    {"text": "Kubernetes in production", "must_have": False, "weight": 3},
                    {"text": "Mentoring", "must_have": False, "weight": 1},
                ]
            }
        return {
            "criteria": [
                {
                    "id": f"C{i}",
                    "verdict": "strong",
                    "evidence": "Led the migration of a payments monolith to microservices in Go",
                }
                for i in range(1, 5)
            ],
            "summary": "",
            "notable_strengths": [],
            "red_flags": [],
        }

    def count_tokens(self, model: str, text: str) -> int:
        return len(text) // 4

    def count_prompt_tokens(self, model: str, system: str, user: str) -> int:
        return 900

    def health(self) -> bool:
        return True

    def digest(self, model: str) -> str:
        return "sha256:aaa"

    def ensure_loaded(self, model: str) -> None:
        self.loaded = model

    def unload(self, model: str) -> None:
        self.loaded = None


class FakeParser:
    def parse(self, path: Path) -> Any:  # noqa: ANN401
        from screener.models import ParsedResume, ParseResult

        return ParseResult(
            parsed=ParsedResume(
                text=RESUME, page_count=1, ocr_used=False, parser_version="fake/1.0"
            )
        )


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """A database and resume folder the CLI will find through settings."""
    monkeypatch.setattr(settings, "db_path", str(tmp_path / "screener.db"))
    monkeypatch.setattr(settings, "resumes_dir", str(tmp_path / "resumes"))
    monkeypatch.setattr(settings, "failure_dir", str(tmp_path / "failures"))
    monkeypatch.setattr(settings, "min_free_disk_gb", 0)

    # The CLI builds its own service and worker; point both at fakes so the flow
    # is exercised without Ollama or a subprocess parse.
    monkeypatch.setattr("screener.clients.ollama_client.OllamaClient", lambda *a, **k: FakeLLM())
    monkeypatch.setattr("screener.intake.sandbox.SandboxedParser", lambda *a, **k: FakeParser())

    dispose_engine()
    yield tmp_path
    dispose_engine()


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def invoke(runner: CliRunner, *args: str) -> Any:  # noqa: ANN401
    result = runner.invoke(app, list(args))
    assert result.exit_code == 0, f"{' '.join(args)}\n{result.output}\n{result.exception}"
    return result


def resumes(count: int = 2, reference: str = "REQ-1") -> Path:
    folder = Path(settings.resumes_dir) / reference
    folder.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        (folder / f"cv{i}.pdf").write_bytes(
            b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
            b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
            b"3 0 obj<</Type/Page/Parent 2 0 R>>endobj\n"
            + f"% cv {i}\n".encode()
            + b"trailer<</Root 1 0 R>>\n%%EOF\n"
        )
    return folder


def audit_actions() -> list[tuple[str, str | None]]:
    with unit_of_work() as tx:
        return [(e["action"], e["actor_id"]) for e in audit_store.recent(tx, 100)]


def candidate_ids(run_id: str) -> list[int]:
    with unit_of_work() as tx:
        return [c.id for c in results_store.list_for_run(tx, run_id) if c.id is not None]


def only_id(table: str) -> str:
    import sqlite3

    connection = sqlite3.connect(settings.db_path)
    try:
        order = "version DESC" if table == "rubrics" else "created_at DESC"
        # noqa: S608 — `table` and `order` come from this function's own literals,
        # never from input. Interpolation is unavoidable: SQLite does not accept
        # a bound parameter in a table name or an ORDER BY clause.
        sql = f"SELECT id FROM {table} ORDER BY {order} LIMIT 1"  # noqa: S608
        return str(connection.execute(sql).fetchone()[0])
    finally:
        connection.close()


# --- the CLI is not an HTTP client -------------------------------------------


def test_the_cli_talks_to_the_service_not_the_api() -> None:
    """The whole point of a break-glass path.

    If it went over HTTP it would be useless in the situation it exists for —
    the API being down.
    """
    tree = ast.parse(CLI_SOURCE.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)

    assert not any(m.startswith(("httpx", "requests", "urllib", "fastapi")) for m in imported)
    assert not any(m.startswith("screener.api") for m in imported)


def test_the_cli_does_not_reach_past_the_service_into_storage() -> None:
    """12.2: the transaction boundary is the service layer.

    Two exceptions are deliberate and named — `migrate` and `seed-user` run
    *before* a service can exist, because the API and worker both refuse to
    start against a stale schema.
    """
    source = CLI_SOURCE.read_text(encoding="utf-8")
    storage_imports = [
        line for line in source.splitlines() if "from screener.storage" in line and "import" in line
    ]

    assert len(storage_imports) <= 3, storage_imports  # connection, positions_store, uow


# --- bootstrap commands ------------------------------------------------------


def test_migrate_creates_the_schema(runner: CliRunner, workspace: Path) -> None:
    result = invoke(runner, "migrate")

    assert "0001.initial-schema" in result.output
    assert Path(settings.db_path).exists()


def test_migrate_is_idempotent(runner: CliRunner, workspace: Path) -> None:
    invoke(runner, "migrate")
    result = invoke(runner, "migrate")

    assert "already current" in result.output


def test_health_exits_nonzero_when_not_ready(runner: CliRunner, workspace: Path) -> None:
    """Usable in a shell script or a pre-flight check — no migrations applied yet."""
    result = runner.invoke(app, ["health"])

    assert result.exit_code == 1
    assert "PENDING" in result.output


def test_health_reports_each_dependency(runner: CliRunner, workspace: Path) -> None:
    invoke(runner, "migrate")
    result = invoke(runner, "health")

    assert "model     ok" in result.output
    assert "schema    current" in result.output


def test_seed_user(runner: CliRunner, workspace: Path) -> None:
    invoke(runner, "migrate")
    invoke(runner, "seed-user", "--id", "ops-oncall", "--name", "Ops On-Call")

    with unit_of_work() as tx:
        from screener.storage import positions_store

        assert positions_store.user_exists(tx, "ops-oncall")


# --- the actor reaches the audit log -----------------------------------------


def test_every_mutating_command_records_its_actor(runner: CliRunner, workspace: Path) -> None:
    """Under pressure, at an odd hour, is exactly when the record matters."""
    jd = workspace / "jd.txt"
    jd.write_text(JD)
    invoke(runner, "migrate")

    invoke(
        runner,
        "position",
        "create",
        "--reference",
        "REQ-1",
        "--title",
        "Backend",
        "--jd-file",
        str(jd),
        "--actor",
        "ops-oncall",
    )
    position_id = only_id("positions")
    invoke(runner, "rubric", "extract", position_id, "--actor", "ops-oncall")
    rubric_id = only_id("rubrics")
    # A different person approves — separation of duties survives the CLI too.
    invoke(runner, "rubric", "approve", rubric_id, "--actor", "manager-dana")

    actions = dict(audit_actions())
    assert actions["create_position"] == "ops-oncall"
    assert actions["extract_rubric"] == "ops-oncall"
    assert actions["approve_rubric"] == "manager-dana"


def test_an_unapproved_rubric_blocks_a_run(runner: CliRunner, workspace: Path) -> None:
    """The human gate holds on this path as well (9.1)."""
    jd = workspace / "jd.txt"
    jd.write_text(JD)
    invoke(runner, "migrate")
    invoke(
        runner,
        "position",
        "create",
        "--reference",
        "REQ-1",
        "--title",
        "Backend",
        "--jd-file",
        str(jd),
        "--actor",
        "ops-oncall",
    )
    position_id = only_id("positions")
    invoke(runner, "rubric", "extract", position_id, "--actor", "ops-oncall")
    resumes()

    result = runner.invoke(
        app, ["run", "create", "--position", position_id, "--rubric", only_id("rubrics")]
    )

    assert result.exit_code != 0


def test_an_override_requires_a_reason(runner: CliRunner, workspace: Path) -> None:
    invoke(runner, "migrate")
    result = runner.invoke(app, ["override", "1", "--decision", "advance", "--reason", "   "])

    assert result.exit_code != 0


def test_a_missing_jd_file_is_a_clear_error(runner: CliRunner, workspace: Path) -> None:
    invoke(runner, "migrate")
    result = runner.invoke(
        app,
        ["position", "create", "--reference", "R", "--title", "T", "--jd-file", "/nope.txt"],
    )

    assert result.exit_code == 1
    assert "No such file" in result.output


# --- the gate: a batch with the API stopped ----------------------------------


def test_a_whole_batch_runs_with_no_api_and_no_daemon(runner: CliRunner, workspace: Path) -> None:
    """The 20 step 16 gate, end to end.

    Nothing is listening on a port. No worker daemon exists. This is the path
    that gets a shortlist out of a box where everything else has failed — and it
    still produces the same audit trail, the same partitions, and the same
    escalation figure as the normal path, because it runs the same code.
    """
    jd = workspace / "jd.txt"
    jd.write_text(JD)

    invoke(runner, "migrate")
    invoke(runner, "seed-user", "--id", "ops-oncall")
    invoke(
        runner,
        "position",
        "create",
        "--reference",
        "REQ-1",
        "--title",
        "Backend",
        "--jd-file",
        str(jd),
        "--actor",
        "ops-oncall",
    )
    position_id = only_id("positions")
    invoke(runner, "rubric", "extract", position_id, "--actor", "ops-oncall")
    rubric_id = only_id("rubrics")
    invoke(runner, "rubric", "approve", rubric_id, "--actor", "manager-dana")

    resumes(count=3)
    invoke(runner, "run", "create", "--position", position_id, "--rubric", rubric_id)
    run_id = only_id("runs")
    invoke(runner, "run", "start", run_id, "--actor", "ops-oncall")

    screened = invoke(runner, "work")
    # Three resumes, two passes each: the break-glass path drives the same
    # two-phase worker the daemon does (17.4).
    assert "6 job(s) processed" in screened.output

    status = invoke(runner, "run", "status", run_id, "--json")
    parsed = json.loads(status.output)
    assert parsed["status"] == "completed"
    assert parsed["done"] == 3
    assert parsed["failed"] == 0

    csv_path = workspace / "shortlist.csv"
    listed = invoke(runner, "candidates", run_id, "--csv", str(csv_path))
    assert "MEETS REQUIREMENTS" in listed.output
    assert "NEEDS REVIEW" in listed.output

    rows = csv_path.read_text().strip().splitlines()
    assert len(rows) == 4  # header + three candidates
    assert "partition,filename,file_sha256,band,score" in rows[0]

    # Same audit trail as the API path.
    actions = dict(audit_actions())
    for expected in ("create_position", "approve_rubric", "create_run", "start_run"):
        assert expected in actions

    # Sign-off is refused while anything still needs a human (23.1.6). This is
    # the break-glass path, so it is exactly where the shortcut would be taken:
    # an operator with the API down still cannot mark a run reviewed without
    # having reviewed it.
    blocked = runner.invoke(app, ["run", "sign-off", run_id, "--actor", "manager-dana"])
    assert blocked.exit_code == 1
    # Read off the exception rather than stdout: the CLI has no error handler, so
    # a `ServiceError` surfaces as a traceback. Pre-existing and true of every
    # command, not something this precondition introduced.
    assert "need review and have no decision" in str(blocked.exception)

    for candidate_id in candidate_ids(run_id):
        invoke(
            runner,
            "decide",
            str(candidate_id),
            "--decision",
            "hold",
            "--reason",
            "reviewed by hand during the outage",
            "--actor",
            "manager-dana",
        )

    invoke(runner, "run", "sign-off", run_id, "--actor", "manager-dana")
    assert dict(audit_actions())["sign_off_run"] == "manager-dana"


def test_work_can_be_limited(runner: CliRunner, workspace: Path) -> None:
    """So an operator can screen a few, look at them, and decide to continue."""
    jd = workspace / "jd.txt"
    jd.write_text(JD)
    invoke(runner, "migrate")
    invoke(
        runner,
        "position",
        "create",
        "--reference",
        "REQ-1",
        "--title",
        "Backend",
        "--jd-file",
        str(jd),
        "--actor",
        "ops-oncall",
    )
    position_id = only_id("positions")
    invoke(runner, "rubric", "extract", position_id, "--actor", "ops-oncall")
    invoke(runner, "rubric", "approve", only_id("rubrics"), "--actor", "ops-oncall")
    resumes(count=4)
    invoke(runner, "run", "create", "--position", position_id, "--rubric", only_id("rubrics"))
    run_id = only_id("runs")
    invoke(runner, "run", "start", run_id)

    invoke(runner, "work", "--limit", "2")

    status = json.loads(invoke(runner, "run", "status", run_id, "--json").output)
    assert status["done"] == 2
    assert status["pending"] == 2  # the rest are still queued, not lost


def test_the_escalation_rate_is_printed_with_results(runner: CliRunner, workspace: Path) -> None:
    """Printed on every result, not buried in a report (18.2)."""
    jd = workspace / "jd.txt"
    jd.write_text(JD)
    invoke(runner, "migrate")
    invoke(
        runner,
        "position",
        "create",
        "--reference",
        "REQ-1",
        "--title",
        "Backend",
        "--jd-file",
        str(jd),
        "--actor",
        "ops-oncall",
    )
    position_id = only_id("positions")
    invoke(runner, "rubric", "extract", position_id, "--actor", "ops-oncall")
    invoke(runner, "rubric", "approve", only_id("rubrics"), "--actor", "ops-oncall")
    resumes(count=1)
    invoke(runner, "run", "create", "--position", position_id, "--rubric", only_id("rubrics"))
    run_id = only_id("runs")
    invoke(runner, "run", "start", run_id)
    invoke(runner, "work")

    output = invoke(runner, "candidates", run_id).output

    assert "needing review" in output
    # Unset by default in v6 (19.2): 3% was a guess, and a guessed budget reads
    # as a measurement everywhere it is quoted. The nag has to be visible on
    # every result, or "measure it on the first real run" becomes "never".
    assert "no budget set" in output
    assert "escalation_budget_source_run" in output
