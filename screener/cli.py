"""Command line interface (spec 4, 14, build gate 20 step 16).

**The break-glass path.** Everything here runs in-process against `service.py`
and `pipeline.py` — no HTTP, no daemon. When the API will not start, when the
worker is wedged, or when someone needs to get a shortlist out of a box that is
otherwise not cooperating, this is the way in.

**It goes through the same service layer as everything else**, and that is the
whole point. A break-glass path that talked to the stores directly would bypass
the transaction boundaries and the audit log — so the one time you most need a
record of who did what, under pressure, at an odd hour, there would not be one.
Every mutating command below takes `--actor` and writes an audit row exactly as
the API does.

Two commands exist only here, because they run *before* anything else can:
`migrate` and `seed-user`. The API and worker both refuse to start against a
stale schema (12.1), so the tool that fixes that cannot be one of them.
"""

import json as jsonlib
import sys
from pathlib import Path
from typing import Annotated, Any

import typer

from config.settings import settings
from screener.models import Actor, Criterion

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="On-prem resume screener. Runs without the API or the worker.",
)
positions_app = typer.Typer(no_args_is_help=True, help="Requisitions")
rubric_app = typer.Typer(no_args_is_help=True, help="Rubrics")
run_app = typer.Typer(no_args_is_help=True, help="Runs")
app.add_typer(positions_app, name="position")
app.add_typer(rubric_app, name="rubric")
app.add_typer(run_app, name="run")

ActorOption = Annotated[str, typer.Option("--actor", help="Recorded in the audit log.")]


def _actor(actor_id: str) -> Actor:
    return Actor(id=actor_id, display_name=actor_id, roles=frozenset({"admin"}))


def _service() -> Any:  # noqa: ANN401 — importing lazily keeps `--help` fast and DB-free
    """Build the service on demand.

    Deferred so that `screener --help` and `screener migrate` do not construct an
    Ollama client or touch a database that may not exist yet — which is exactly
    the situation this tool is for.
    """
    from screener.clients.ollama_client import OllamaClient
    from screener.intake.document_text import DocumentTextExtractor
    from screener.service import ScreenerService

    return ScreenerService(llm=OllamaClient(), extractor=DocumentTextExtractor())


def _fail(message: str) -> None:
    typer.secho(message, fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _emit(data: Any, as_json: bool) -> None:  # noqa: ANN401 — several unrelated shapes
    if as_json:
        typer.echo(jsonlib.dumps(data, indent=2, default=str))


# --- bootstrap ---------------------------------------------------------------


@app.command()
def migrate() -> None:
    """Apply pending schema migrations.

    Must live here: the API and the worker both refuse to start while migrations
    are outstanding, so the thing that resolves that cannot be either of them.
    """
    from screener.storage.connection import apply_migrations, redacted_url

    applied = apply_migrations()
    if applied:
        typer.secho(
            f"Applied {len(applied)} migration(s) to {redacted_url()}", fg=typer.colors.GREEN
        )
        for name in applied:
            typer.echo(f"  {name}")
    else:
        typer.echo("Schema is already current.")


@app.command("seed-user")
def seed_user(
    id: Annotated[str, typer.Option("--id", help="Actor id")],  # noqa: A002 — the documented flag name
    name: Annotated[str, typer.Option("--name")] = "",
) -> None:
    """Create the operator row every actor foreign key points at (19)."""
    from screener.storage import positions_store
    from screener.storage.uow import unit_of_work

    with unit_of_work() as tx:
        positions_store.seed_user(tx, id, name or id)
    typer.secho(f"Seeded user {id}", fg=typer.colors.GREEN)


@app.command()
def health(json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Model, schema and disk, reported separately.

    Exits non-zero when not ready, so it is usable in a shell script or a
    pre-flight check.
    """
    report = _service().health()
    if json:
        _emit(report.model_dump(), True)
    else:
        typer.echo(f"model     {'ok' if report.llm_reachable else 'UNREACHABLE'}")
        typer.echo(f"database  {'ok' if report.db_reachable else 'UNREACHABLE'}")
        typer.echo(f"schema    {'current' if report.migrations_current else 'PENDING'}")
        typer.echo(f"disk      {report.free_disk_gb:.1f} GB free")
        typer.echo(f"version   {report.app_version}")
        for key, value in report.detail.items():
            typer.secho(f"  {key}: {value}", fg=typer.colors.YELLOW)
    if not report.ok:
        raise typer.Exit(code=1)


# --- positions ---------------------------------------------------------------


@positions_app.command("create")
def create_position(
    reference: Annotated[str, typer.Option("--reference", help="Folder under data/resumes/")],
    title: Annotated[str, typer.Option("--title")],
    jd_file: Annotated[
        Path, typer.Option("--jd-file", help="Job description: .pdf, .docx, or a text file")
    ],
    actor: ActorOption = settings.dev_actor_id,
) -> None:
    """Raise a requisition from a job description on disk.

    A PDF or DOCX goes through the same sandboxed parser the interface uses, so
    the break-glass path and the UI agree on what a job-description file is.
    Anything else is read as text, which is what this option always did and is
    still the right behaviour for the `.txt` and `.md` files it was written for.
    """
    if not jd_file.is_file():
        _fail(f"No such file: {jd_file}")

    service = _service()
    if jd_file.suffix.casefold() in settings.allowed_extensions:
        extraction = service.extract_jd_document(
            jd_file.read_bytes(), filename=jd_file.name, actor=_actor(actor)
        )
        # Printed rather than silently accepted: nobody is watching a browser
        # here, and a rubric drafted from an OCR approximation is the thing this
        # operator most needs to know before approving one.
        if extraction.ocr_used:
            typer.secho(
                f"  read by OCR ({extraction.page_count} pages) — check the text",
                fg=typer.colors.YELLOW,
            )
        jd_text, source, filename, sha, ocr = (
            extraction.text,
            "upload",
            extraction.filename,
            extraction.file_sha256,
            extraction.ocr_used,
        )
    else:
        jd_text, source, filename, sha, ocr = (
            jd_file.read_text(encoding="utf-8"),
            "paste",
            None,
            None,
            None,
        )

    position = service.create_position(
        reference=reference,
        title=title,
        jd_text=jd_text,
        actor=_actor(actor),
        jd_source=source,
        jd_filename=filename,
        jd_file_sha256=sha,
        jd_ocr_used=ocr,
    )
    typer.secho(f"{position.id}  {position.reference}", fg=typer.colors.GREEN)


@positions_app.command("list")
def list_positions() -> None:
    for position in _service().list_positions():
        typer.echo(f"{position.id}  {position.reference:<16} {position.title}")


# --- rubrics -----------------------------------------------------------------


@rubric_app.command("extract")
def extract_rubric(
    position_id: Annotated[str, typer.Argument()],
    actor: ActorOption = settings.dev_actor_id,
) -> None:
    """Draft criteria from the job description. One LLM call, ~5 s.

    The result is unapproved and unusable for a run until a human approves it.
    """
    rubric = _service().extract_rubric(position_id, _actor(actor))
    typer.secho(f"{rubric.id}  version {rubric.version}", fg=typer.colors.GREEN)
    for criterion in rubric.criteria:
        marker = "MUST" if criterion.must_have else "    "
        typer.echo(f"  {criterion.id}  {marker}  w{criterion.weight}  {criterion.text}")
    typer.secho("Review these before approving.", fg=typer.colors.YELLOW)


@rubric_app.command("edit")
def edit_rubric(
    position_id: Annotated[str, typer.Argument()],
    criteria_file: Annotated[Path, typer.Option("--file", help="JSON list of criteria")],
    actor: ActorOption = settings.dev_actor_id,
) -> None:
    """Save an edited rubric as a **new version**. Never edits one in place."""
    if not criteria_file.is_file():
        _fail(f"No such file: {criteria_file}")
    criteria = [Criterion(**c) for c in jsonlib.loads(criteria_file.read_text(encoding="utf-8"))]
    rubric = _service().save_rubric(position_id, criteria, _actor(actor))
    typer.secho(f"{rubric.id}  version {rubric.version}", fg=typer.colors.GREEN)


@rubric_app.command("approve")
def approve_rubric(
    rubric_id: Annotated[str, typer.Argument()],
    actor: ActorOption = settings.dev_actor_id,
) -> None:
    """Record a named human accepting the rubric (9.1)."""
    rubric = _service().approve_rubric(rubric_id, _actor(actor))
    typer.secho(f"Approved by {rubric.approved_by} at {rubric.approved_at}", fg=typer.colors.GREEN)


# --- runs --------------------------------------------------------------------


@run_app.command("create")
def create_run(
    position_id: Annotated[str, typer.Option("--position")],
    rubric_id: Annotated[str, typer.Option("--rubric")],
    actor: ActorOption = settings.dev_actor_id,
) -> None:
    """Snapshot the position's folder into jobs. A run is a fixed set (16.2)."""
    run = _service().create_run(position_id, rubric_id, _actor(actor))
    status = _service().run_status(run.id)
    typer.secho(f"{run.id}  {status.total} file(s) queued", fg=typer.colors.GREEN)


@run_app.command("start")
def start_run(
    run_id: Annotated[str, typer.Argument()],
    actor: ActorOption = settings.dev_actor_id,
) -> None:
    queued = _service().start_run(run_id, _actor(actor))
    typer.echo(f"{queued} file(s) ready. Run `screener work` or start the worker.")


@run_app.command("rescan")
def rescan_run(
    run_id: Annotated[str, typer.Argument()],
    actor: ActorOption = settings.dev_actor_id,
) -> None:
    typer.echo(f"{_service().rescan_run(run_id, _actor(actor))} new file(s) added.")


@run_app.command("abort")
def abort_run(
    run_id: Annotated[str, typer.Argument()],
    actor: ActorOption = settings.dev_actor_id,
) -> None:
    _service().abort_run(run_id, _actor(actor))
    typer.echo("Aborted. Already-screened results are kept and the run can be restarted.")


@run_app.command("status")
def run_status(
    run_id: Annotated[str, typer.Argument()],
    json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    status = _service().run_status(run_id)
    if json:
        _emit(status.model_dump(), True)
        return
    typer.echo(f"status      {status.status}")
    typer.echo(f"screened    {status.done} of {status.total} ({status.failed} failed)")
    typer.echo(f"remaining   {status.pending + status.claimed}")
    typer.echo(f"eta         {status.eta_seconds / 60:.0f} min")
    _report_escalation(status.escalation_rate)


@run_app.command("sign-off")
def sign_off(
    run_id: Annotated[str, typer.Argument()],
    actor: ActorOption = settings.dev_actor_id,
) -> None:
    _service().sign_off_run(run_id, _actor(actor))
    typer.secho(f"Signed off by {actor}.", fg=typer.colors.GREEN)


# --- the break-glass path ----------------------------------------------------


@app.command()
def work(
    limit: Annotated[int, typer.Option("--limit", help="Stop after this many jobs")] = 0,
    worker_id: Annotated[str, typer.Option("--worker-id")] = "",
) -> None:
    """Screen the queue **in the foreground**, without the daemon or the API.

    The gate for this step: a batch can be run with everything else stopped.

    Uses the same `Worker` class the daemon runs, so the emergency path and the
    normal path cannot drift apart — a hand-rolled loop here would be the one
    with different retry, reclaim, or completion semantics, discovered during an
    incident.
    """
    from screener.worker_loop import WorkerIdentityTakenError, build_worker

    worker = build_worker()
    if worker_id:
        worker.worker_id = worker_id
    worker.install_signal_handlers()

    try:
        reclaimed = worker.startup()
    except WorkerIdentityTakenError as err:
        # The common way to hit this: running the break-glass path while the
        # daemon is up. Both would answer to the same name, and this one's
        # startup reclaim would take the running worker's job away mid-inference.
        _fail(
            f"{err}\n\nTry: ./scripts/dev.sh stop worker   (or: screener work --worker-id adhoc-1)"
        )
        return

    if reclaimed:
        typer.secho(f"Reclaimed {reclaimed} job(s) from a previous run.", fg=typer.colors.YELLOW)

    processed = 0
    try:
        while worker.run_once():
            processed += 1
            typer.echo(f"  processed {processed}")
            if limit and processed >= limit:
                break
    finally:
        worker.release()

    # Jobs, not resumes. A run is two passes over the same candidates (17.4),
    # so "6 resumes screened" for a folder of 3 would be wrong in the direction
    # that makes an operator think the queue contained something it did not.
    typer.secho(f"Done. {processed} job(s) processed.", fg=typer.colors.GREEN)


# --- results -----------------------------------------------------------------


@app.command()
def candidates(
    run_id: Annotated[str, typer.Argument()],
    csv_out: Annotated[Path | None, typer.Option("--csv", help="Write a CSV for audit")] = None,
) -> None:
    """Three partitions, never one list (10.6).

    On screen this prints bands. The numeric score goes to the CSV, where its
    provenance travels with it — three verdict levels cannot support a rendered
    precision of `7.8`.
    """
    result = _service().list_candidates(run_id)

    for label, group in (
        ("MEETS REQUIREMENTS", result.meets_must_haves),
        ("MISSING A MUST-HAVE", result.missing_must_have),
        ("NEEDS REVIEW (unranked)", result.needs_review),
    ):
        typer.secho(f"\n{label} ({len(group)})", bold=True)
        for candidate in group:
            flags = ",".join(f.value for f in candidate.flags)
            typer.echo(f"  {candidate.band or '-':<3} {candidate.filename:<32} {flags}")

    _report_escalation(result.escalation_rate)

    if csv_out:
        csv_out.write_text(_to_csv(result), encoding="utf-8")
        typer.secho(f"Wrote {csv_out}", fg=typer.colors.GREEN)


@app.command()
def purge(
    file_sha256: Annotated[str, typer.Argument()],
    actor: ActorOption = settings.dev_actor_id,
) -> None:
    """Erase a candidate everywhere, trace files included (12.6)."""
    erased, removed = _service().purge_candidate(file_sha256, _actor(actor))
    if not erased:
        # Reported as a warning, not a success. "Purged." over a hash that matched
        # nothing reads as a completed erasure, and someone acting on a deletion
        # request would file it as done.
        typer.secho(
            f"No candidate matches {file_sha256[:12]}. Nothing was erased "
            f"({removed} orphaned capture file(s) deleted).",
            fg=typer.colors.YELLOW,
        )
        return
    typer.secho(
        f"Purged {erased} candidate row(s). {removed} capture file(s) deleted.",
        fg=typer.colors.GREEN,
    )


@app.command()
def decide(
    candidate_id: Annotated[int, typer.Argument()],
    decision: Annotated[str, typer.Option("--decision", help="advance | reject | hold")],
    reason: Annotated[str, typer.Option("--reason")],
    actor: ActorOption = settings.dev_actor_id,
) -> None:
    """Record a human decision. The reason is required — it is the record."""
    _service().record_decision(candidate_id, decision, reason, _actor(actor))
    typer.secho("Recorded.", fg=typer.colors.GREEN)


# --- helpers -----------------------------------------------------------------


def _report_escalation(rate: float) -> None:
    """Printed on every result, not buried in a report (18.2)."""
    budget = settings.escalation_budget
    if budget is None:
        # Unset is the default and is not a failure — but it must be visible,
        # or "measure it on the first real run" becomes "never" (19.2).
        typer.secho(
            f"needing review  {rate:.0%}  (no budget set — measure this run and record "
            "escalation_budget_source_run)",
            fg=typer.colors.YELLOW,
        )
        return

    line = f"needing review  {rate:.0%}  (budget {budget:.0%})"
    if rate > budget:
        typer.secho(line, fg=typer.colors.YELLOW)
        typer.secho(
            "  A queue larger than a person will genuinely read is the point at "
            "which review stops being meaningful.",
            fg=typer.colors.YELLOW,
        )
    else:
        typer.echo(line)


def _to_csv(result: Any) -> str:  # noqa: ANN401 — RankedResult, imported lazily
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "partition",
            "filename",
            "file_sha256",
            "band",
            "score",
            "must_haves_met",
            "scoreable",
            "review_required",
            # The decision and who owns it (15.6). An export that carries the
            # ranking but not the outcome cannot answer the only question an
            # adverse-action review actually asks.
            "decision",
            "decided_by",
            "decided_at",
            "verification_status",
            "escalation_reasons",
            "flags",
            "scored_at",
        ]
    )
    for partition, group in (
        ("meets_must_haves", result.meets_must_haves),
        ("missing_must_have", result.missing_must_have),
        ("needs_review", result.needs_review),
    ):
        for c in group:
            writer.writerow(
                [
                    partition,
                    c.filename,
                    c.file_sha256,
                    c.band or "",
                    "" if c.score is None else c.score,
                    c.must_haves_met,
                    c.scoreable,
                    c.review_required,
                    c.decision,
                    c.decided_by or "",
                    c.decided_at or "",
                    c.verification_status,
                    "|".join(r.value for r in c.escalation_reasons),
                    "|".join(f.value for f in c.flags),
                    c.scored_at,
                ]
            )
    return buffer.getvalue()


def main() -> int:
    app()
    return 0


if __name__ == "__main__":
    sys.exit(main())
