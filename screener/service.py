"""Transactional command/query surface (spec 14).

Called by the API, the CLI, and the worker — three callers, one implementation,
so every business rule is exercisable without an HTTP client. That is the test
15.4 sets for the route handlers, and it only holds if the rules live here.

**Three rules shape every method below.**

*Every mutating method takes an `actor` and writes its audit row inside the same
transaction as its effect.* Not afterwards, not in a wrapper. A crash between the
two leaves an override with no record of who made it — a decision affecting a
person that nobody can be held to.

*No method contains inference.* `extract_rubric` is the single LLM call here, and
it is one ~5 s request made while a human waits, not a batch. **This module never
imports `pipeline.py`.** It enqueues; the worker executes. If the service layer
ever starts screening, the process boundaries in 2 have collapsed and the API
stops being responsive during a 78-minute run.

*No pass-through methods.* Every method below adds a transaction boundary, an
audit row, orchestration across stores, or a rule. One that only forwarded to a
store would be pure cost — a layer to maintain that changes nothing.

**The verdict/rubric join.** `verdicts` rows do not store `weight` or `must_have`:
those are rubric facts, and duplicating them would create a second source of
truth for the scoring arithmetic. This layer re-attaches them from the run's
rubric when reading a candidate back, which is real orchestration across two
stores rather than a forwarding call.
"""

import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

from config.settings import settings
from screener.core.rank import rank
from screener.core.resume_paths import folder_for, is_safe_reference
from screener.llm.extract_rubric import extract_rubric as run_extraction
from screener.llm.judge_resume import judge_prompt_hash
from screener.logging import failure_paths_for
from screener.models import (
    Actor,
    AdverseActionRecord,
    AuditEntry,
    Candidate,
    Criterion,
    DashboardSummary,
    Decision,
    DecisionRecord,
    EscalationReason,
    FailedFile,
    FolderInfo,
    HealthReport,
    Position,
    RankedResult,
    ReviewQueue,
    Rubric,
    Run,
    RunStatus,
    RunStory,
    now,
)
from screener.ports import CacheKey, Job, LLMClient
from screener.storage import (
    audit_store,
    jobs_store,
    positions_store,
    results_store,
    resumes_store,
    rubrics_store,
    runs_store,
)
from screener.storage.connection import pending_migrations
from screener.storage.connection import (
    require_current_schema as _require_current_schema,
)
from screener.storage.uow import Tx, UnitOfWork, unit_of_work


class ServiceError(RuntimeError):
    """A rule was violated. Distinct from an infrastructure failure."""


class NotFoundError(ServiceError):
    pass


class ConflictError(ServiceError):
    """Someone else changed the thing you were editing. Maps to HTTP 409."""


class RubricNotApprovedError(ServiceError):
    """A run may not be created against an unapproved rubric.

    The approval is the human gate in front of an LLM-generated rubric (9.1).
    Without it, a hallucinated requirement silently rejects every applicant who
    lacks something the job never asked for — across the whole run, leaving no
    trace in any individual result.
    """


def require_current_schema() -> None:
    """Refuse to run against a schema the code does not match (12.1).

    Raises rather than warns, and does not auto-migrate: applying a schema
    change as a side effect of starting a process means it runs at an unplanned
    time, on a database nobody has backed up, possibly from two processes at
    once.

    Exposed here so `create_app` can gate on it without reaching past the
    service layer — 4 is `api/ → service.py, schemas.py, models.py`, and a
    bootstrap check is not a reason to make an exception to that. Importing this
    module constructs nothing, so the gate still runs before anything is wired.
    """
    _require_current_schema()


def _mark_stale_claims(criteria: list[Criterion], previous: Rubric | None) -> list[Criterion]:
    """Flag every criterion whose text moved while its claim stood still (23.1.8).

    Computed here rather than trusted from the request. The client that forgets
    to set the flag is precisely the client this protects against, and the
    failure is silent: phase 2 goes on verifying a hypothesis the rubric no
    longer makes, agreeing confidently with the wrong question.

    An **absent** claim is not stale. Rubrics written before v6 have none, and
    `verify_support` falls back to the criterion text for them; treating empty as
    stale would block approval of every one of those on a claim that was never
    written rather than one that went out of date.
    """
    if previous is None:
        return criteria
    before = {c.id: c for c in previous.criteria}
    return [
        c.model_copy(update={"claim_stale": True})
        if (old := before.get(c.id)) is not None
        and c.claim != ""
        and c.text != old.text
        and c.claim == old.claim
        else c
        for c in criteria
    ]


@dataclass(frozen=True)
class BulkDecisionResult:
    """What a bulk action actually did, not what it was asked to do."""

    decided: list[int] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)


@dataclass
class ScreenerService:
    llm: LLMClient
    uow_factory: Callable[[], UnitOfWork] = unit_of_work

    # --- actor plumbing ------------------------------------------------------

    @staticmethod
    def _ensure_actor(tx: Tx, actor: Actor) -> None:
        """Guarantee a `users` row before anything references this actor.

        `positions.created_by`, `rubrics.approved_by`, `runs.reviewed_by` and
        `overrides.actor_id` are all foreign keys into `users`. Seeding only on
        position creation meant **a second person could not sign off or
        override** — the referential integrity check rejected them — which is
        exactly backwards: separation of duties between whoever raises a
        requisition and whoever approves its results is the point of having an
        audit trail at all.

        Auth is stubbed today (15.2), so actors appear here on first use. When
        LDAP or local auth lands this becomes a lookup against the real
        directory rather than an insert, and no call site changes — which is the
        retrofit this plumbing exists to make cheap.
        """
        positions_store.seed_user(tx, actor.id, actor.display_name or actor.id)

    # --- positions -----------------------------------------------------------

    def create_position(
        self, *, reference: str, title: str, jd_text: str, actor: Actor
    ) -> Position:
        if not is_safe_reference(reference):
            raise ServiceError(
                f"Invalid reference '{reference}': "
                "letters, numbers, spaces, hyphens, and underscores only"
            )

        position = Position(
            id=f"pos-{uuid.uuid4().hex[:12]}",
            reference=reference,
            title=title,
            jd_text=jd_text,
            created_by=actor.id,
            created_at=now(),
        )
        with self.uow_factory() as tx:
            self._ensure_actor(tx, actor)
            # `reference` is unique only while a requisition using it is open
            # (0004) — checked here rather than left to the index so a clash
            # reads as a 409 a reviewer can act on, not an unhandled 500 from a
            # raw `IntegrityError`.
            if positions_store.get_open_by_reference(tx, reference) is not None:
                raise ConflictError(f"a requisition with reference '{reference}' is already open")
            positions_store.create(tx, position)
            audit_store.append_for(
                tx, actor, "create_position", "position", position.id, {"reference": reference}
            )
        return position

    def list_positions(self, *, include_closed: bool = False) -> list[Position]:
        """Open requisitions by default; everything when asked.

        The default is the working list — what somebody is recruiting for now.
        `include_closed` exists for the screens that resolve a run's requisition:
        a run outlives the requisition it belongs to, and a closed one must not
        become an unresolvable id on a screen showing candidate results.
        """
        with self.uow_factory() as tx:
            if include_closed:
                return positions_store.list_all(tx)
            return positions_store.list_open(tx)

    def close_position(self, position_id: str, actor: Actor) -> Position:
        """Close a requisition: the post is filled, or it is not being filled.

        **Nothing is deleted and no run is touched.** The requisition leaves the
        working list, and every run it produced stays exactly where it is —
        readable, exportable, and answerable to the audit log. A closed
        requisition whose candidate records vanished would make the record of an
        adverse decision depend on whether somebody later tidied up.

        **Runs already under way continue.** Closing is an administrative fact
        about the requisition, not a stop signal. The worker's queue is keyed on
        the run, so nothing here reaches it — and that is the behaviour to want:
        a batch 400 CVs into 1,000 has already spent the GPU time, and halting it
        would discard that while leaving 400 applicants assessed and unanswered.
        The reviewer works and signs off the run exactly as before; the
        requisition is simply no longer recruiting. Stopping a run remains its
        own deliberate act, on the run, called Abort.

        This is why `list_positions` takes `include_closed`: a run outliving its
        requisition still has to be able to name the requisition it belongs to.

        Idempotent, deliberately: closing an already-closed requisition is not an
        error, it is someone arriving at the state they wanted. It records no
        second audit row, because nothing changed.
        """
        with self.uow_factory() as tx:
            self._ensure_actor(tx, actor)
            position = positions_store.get(tx, position_id)
            if position is None:
                raise NotFoundError(f"position {position_id}")
            if position.status == "closed":
                return position

            positions_store.close(tx, position_id)
            audit_store.append_for(
                tx,
                actor,
                "close_position",
                "position",
                position_id,
                {"reference": position.reference},
            )
            closed = positions_store.get(tx, position_id)
        assert closed is not None  # noqa: S101 — just written in this transaction
        return closed

    def list_resume_folders(
        self, subpath: str = "", query: str = "", offset: int = 0, limit: int = 15
    ) -> tuple[list[FolderInfo], int]:
        """One page of subfolders of the resume share, and the total matching.

        Touches no database, so no transaction. `subpath` is validated inside
        the store; an unsafe or missing path lists as empty rather than raising,
        because an unmounted share is an ordinary state of this screen.

        Paged because counting a folder's resumes is a recursive walk: a large
        share on a network mount would otherwise spend seconds per render, and
        Streamlit renders on every click.
        """
        return resumes_store.list_folders(subpath, query, offset, limit)

    # --- rubrics -------------------------------------------------------------

    def extract_rubric(self, position_id: str, actor: Actor) -> Rubric:
        """JD → draft rubric. The one LLM call in this layer.

        **The LLM call happens outside the transaction.** It takes ~5 s, and
        SQLite's write lock held for that long blocks the worker in another
        process. The same discipline as the worker's claim → screen → save.

        The result is a *draft*: unapproved, and unusable for a run until a human
        approves it.
        """
        with self.uow_factory() as tx:
            position = positions_store.get(tx, position_id)
        if position is None:
            raise NotFoundError(f"position {position_id}")

        extraction = run_extraction(self.llm, position.jd_text)

        with self.uow_factory() as tx:
            self._ensure_actor(tx, actor)
            version = rubrics_store.next_version(tx, position_id)
            rubric = Rubric(
                id=f"rub-{uuid.uuid4().hex[:12]}",
                position_id=position_id,
                version=version,
                criteria=extraction.criteria,
                created_by=actor.id,
            )
            rubrics_store.create(tx, rubric)
            audit_store.append_for(
                tx,
                actor,
                "extract_rubric",
                "rubric",
                rubric.id,
                {
                    "position_id": position_id,
                    "criteria": len(rubric.criteria),
                    "prompt_hash": extraction.prompt_hash,
                    "judge_digest": self.llm.digest(settings.judge_model),
                },
            )
        return rubric

    def save_rubric(
        self,
        position_id: str,
        criteria: list[Criterion],
        actor: Actor,
        base_version: int | None = None,
    ) -> Rubric:
        """Store a reviewer's edited rubric as a **new version**.

        Never an in-place edit. A run records the `rubric_id` it used and
        candidates carry the `rubric_hash`; rewriting a rubric already scored
        against would make those stored decisions unexplainable, showing a
        reviewer verdicts against criteria that no longer exist.

        `base_version` is the version the editor was looking at (23.1.7). Two
        recruiters tuning the same rubric in adjacent tabs is the ordinary case,
        not the exotic one, and without this the second save silently supersedes
        the first — no conflict, no error, just one person's edits gone and a
        higher version number to suggest everything worked. `None` skips the
        check, for callers with no prior version to be stale about.
        """
        with self.uow_factory() as tx:
            self._ensure_actor(tx, actor)
            if positions_store.get(tx, position_id) is None:
                raise NotFoundError(f"position {position_id}")
            latest = rubrics_store.latest_version(tx, position_id)
            if base_version is not None and base_version != latest:
                raise ConflictError(
                    f"rubric was edited by someone else: you started from version "
                    f"{base_version}, current is {latest}"
                )
            previous = rubrics_store.latest_for_position(tx, position_id)
            rubric = Rubric(
                id=f"rub-{uuid.uuid4().hex[:12]}",
                position_id=position_id,
                version=rubrics_store.next_version(tx, position_id),
                criteria=_mark_stale_claims(criteria, previous),
                created_by=actor.id,
            )
            rubrics_store.create(tx, rubric)
            audit_store.append_for(
                tx,
                actor,
                "save_rubric",
                "rubric",
                rubric.id,
                {"version": rubric.version, "rubric_hash": rubric.content_hash},
            )
        return rubric

    def approve_rubric(self, rubric_id: str, actor: Actor) -> Rubric:
        """The human gate. Recorded with who and when, because it is a decision.

        **Blocked while any claim is stale, but only when something reads the
        claim.** The claim is what `verify_support` (Stage D) verifies against
        (10.6 A); editing a criterion's text without regenerating it leaves
        that check checking a hypothesis the rubric no longer makes — silently,
        and in the direction that produces confident agreement with the wrong
        question. Under the shipped `verify_scope="none"` default that stage
        never runs at all, so blocking approval over it would be enforcing
        upkeep of a field nothing reads — worse, with no way to fix it, since
        the rubric editor no longer shows a claim field to fix it in. Revisit
        if `verify_scope` is ever turned back on.
        """
        with self.uow_factory() as tx:
            self._ensure_actor(tx, actor)
            rubric = rubrics_store.get(tx, rubric_id)
            if rubric is None:
                raise NotFoundError(f"rubric {rubric_id}")
            stale = [c.id for c in rubric.criteria if c.claim_stale]
            if stale and settings.verify_scope != "none":
                raise ServiceError(
                    f"criteria {', '.join(stale)} were edited after their claim was written; "
                    "regenerate the claims before approving"
                )
            rubrics_store.approve(tx, rubric_id, actor)
            audit_store.append_for(
                tx,
                actor,
                "approve_rubric",
                "rubric",
                rubric_id,
                {"rubric_hash": rubric.content_hash},
            )
            approved = rubrics_store.get(tx, rubric_id)
        assert approved is not None  # noqa: S101 — just written in this transaction
        return approved

    def get_latest_rubric(self, position_id: str) -> Rubric | None:
        """The latest rubric for a position, or None when no rubric exists yet."""
        with self.uow_factory() as tx:
            return rubrics_store.latest_for_position(tx, position_id)

    def get_approved_rubric(self, position_id: str) -> Rubric | None:
        """The active approved rubric for a position, or None when none is approved yet."""
        with self.uow_factory() as tx:
            return rubrics_store.approved_for_position(tx, position_id)

    # --- runs ----------------------------------------------------------------

    def create_run(self, position_id: str, rubric_id: str, actor: Actor) -> Run:
        """Create a run and **snapshot** the position's folder into jobs.

        The snapshot is what makes a run a defined set of candidates at a point
        in time — which is what makes the ranking meaningful and the result
        reproducible. Files added afterwards need an explicit `rescan_run`.

        Every reproducibility input is frozen here: digest, prompt hash,
        decoding parameters, `app_version`. They cannot be recovered later.
        """
        run_id = f"run-{uuid.uuid4().hex[:12]}"

        # Outside the transaction: reaching Ollama for the digest is a network
        # call, and the write lock is not held across one.
        judge_digest = self.llm.digest(settings.judge_model)
        # Recorded even when verification is off for this run, so a later
        # question — "which verifier would this run have used?" — is answerable
        # from the row rather than from whatever the config says today.
        verifier_digest = self.llm.digest(settings.verifier_model)
        prompt_hash = judge_prompt_hash()

        with self.uow_factory() as tx:
            self._ensure_actor(tx, actor)
            position = positions_store.get(tx, position_id)
            if position is None:
                raise NotFoundError(f"position {position_id}")
            rubric = rubrics_store.get(tx, rubric_id)
            if rubric is None:
                raise NotFoundError(f"rubric {rubric_id}")
            if rubric.position_id != position_id:
                raise ServiceError(f"rubric {rubric_id} does not belong to {position_id}")
            if not rubrics_store.is_approved(tx, rubric_id):
                raise RubricNotApprovedError(rubric_id)

            try:
                folder = folder_for(position.reference)
            except ValueError as err:
                raise ServiceError(str(err)) from err

            runs_store.create(
                tx,
                run_id=run_id,
                position_id=position_id,
                rubric_id=rubric_id,
                folder=str(folder),
                created_by=actor.id,
                judge_model=settings.judge_model,
                judge_digest=judge_digest,
                verifier_model=settings.verifier_model,
                verifier_digest=verifier_digest,
                verification_enabled=settings.verification_enabled,
                prompt_hash=prompt_hash,
                redaction_on=settings.redact_pii,
                num_ctx=settings.num_ctx,
                num_predict=settings.num_predict,
                seed=settings.seed,
                app_version=settings.app_version,
            )
            queued = jobs_store.snapshot_folder(tx, run_id, folder)
            runs_store.set_file_count(tx, run_id, queued)
            if queued == 0:
                # Distinct from `completed` (17.6). A run over an empty folder
                # otherwise reaches sign-off as a blank results screen that is
                # indistinguishable from "we screened everyone and nobody
                # qualified" — the one outcome a reviewer must not confuse with a
                # mistyped path.
                runs_store.set_status(tx, run_id, "empty")
            audit_store.append_for(
                tx,
                actor,
                "create_run",
                "run",
                run_id,
                {"folder": str(folder), "queued": queued, "rubric_id": rubric_id},
            )
            created = runs_store.get(tx, run_id)

        assert created is not None  # noqa: S101 — just written in this transaction
        return created

    def rescan_run(self, run_id: str, actor: Actor) -> int:
        """Pick up files added since the snapshot. Returns how many are new.

        Explicit, never automatic. Nothing is silently added mid-run, because a
        ranking computed over a set that changed underneath it describes nothing.
        """
        with self.uow_factory() as tx:
            run = runs_store.get(tx, run_id)
            if run is None:
                raise NotFoundError(f"run {run_id}")
            added = jobs_store.snapshot_folder(tx, run_id, Path(run.folder))
            # Recomputed from the jobs actually snapshotted, not `run.file_count
            # + added`: a run created before this accounting existed still reads
            # 0, and adding to a wrong number keeps it wrong.
            runs_store.set_file_count(tx, run_id, jobs_store.progress(tx, run_id, "judge").total)
            if added and run.phase == "done":
                # New files are phase-1 work, so a finished run re-enters phase 1.
                # Without this the jobs sit in a queue nothing is draining: the
                # worker picks its phase from the run, and a run left at `done`
                # is invisible to it. The symptom is a rescan that reports "1
                # added" and then never screens anybody.
                runs_store.set_phase(tx, run_id, "judge")
            audit_store.append_for(tx, actor, "rescan_run", "run", run_id, {"added": added})
        return added

    def list_runs(self) -> list[Run]:
        """All screening runs ordered by creation date descending."""
        with self.uow_factory() as tx:
            return runs_store.list_all(tx)

    def start_run(self, run_id: str, actor: Actor) -> int:
        """Mark a run ready. **Enqueues only** — the worker executes (15.3).

        Screening never runs in a request lifecycle: a 78-minute batch tied to
        one loses orphan reclaim, resumption, and dies with the process.
        """
        with self.uow_factory() as tx:
            run = runs_store.get(tx, run_id)
            if run is None:
                raise NotFoundError(f"run {run_id}")
            progress = jobs_store.progress(tx, run_id)
            if progress.total == 0:
                raise ServiceError(f"run {run_id} has no queued files")
            runs_store.set_status(tx, run_id, "pending")
            audit_store.append_for(
                tx, actor, "start_run", "run", run_id, {"queued": progress.pending}
            )
        return progress.pending

    def abort_run(self, run_id: str, actor: Actor) -> None:
        """Stop a run. Resumable — in-flight jobs go back to pending (16.4)."""
        with self.uow_factory() as tx:
            if runs_store.get(tx, run_id) is None:
                raise NotFoundError(f"run {run_id}")
            released = jobs_store.abort_run_jobs(tx, run_id)
            runs_store.set_status(tx, run_id, "aborted")
            audit_store.append_for(tx, actor, "abort_run", "run", run_id, {"released": released})

    def run_status(self, run_id: str) -> RunStatus:
        """Counts, ETA and live escalation rate.

        The escalation rate is computed here rather than at the end: a reviewer
        who discovers a 200-item queue only when the run completes has already
        lost 78 minutes, and 18.2 exists because that is when the control
        silently stops working.

        **Read-only transaction.** The UI polls this every 5 s while a run is
        live (`POLL_MS`, web/src/api/queries.ts) — the same window the worker is
        claiming and completing jobs in. `uow_factory()`'s default `BEGIN
        IMMEDIATE` takes the single write-reservation lock even for a pure read,
        so a poll and a worker write compete for it; under any pile-up that is
        enough to exceed `busy_timeout` and crash the worker with `database is
        locked` — observed in practice. Nothing below writes, so `.read_only()`
        is safe here.
        """
        with self.uow_factory().read_only() as tx:
            run = runs_store.get(tx, run_id)
            if run is None:
                raise NotFoundError(f"run {run_id}")
            progress = jobs_store.progress(tx, run_id)
            active = run.phase if run.phase in ("judge", "verify") else "judge"
            current = progress if active == "judge" else jobs_store.progress(tx, run_id, active)
            ahead = jobs_store.queue_depth_ahead(tx, run_id)
            scored = results_store.list_for_run(tx, run_id)
            failed = jobs_store.failed_jobs(tx, run_id)

        escalated = [c for c in scored if c.review_required or not c.scoreable]
        breakdown: dict[EscalationReason, int] = {}
        for candidate in escalated:
            for reason in candidate.escalation_reasons:
                breakdown[reason] = breakdown.get(reason, 0) + 1

        # Both phases still to run for anything not yet judged, one for anything
        # judged but unverified. A single-phase ETA understates a two-phase run
        # by half, and an ETA people plan around is worse wrong than absent.
        remaining = progress.pending + progress.claimed + ahead
        per_resume = settings.seconds_per_resume * (2 if run.verification_enabled else 1)

        return RunStatus(
            run_id=run_id,
            status=run.status,
            total=progress.total,
            pending=progress.pending,
            claimed=progress.claimed,
            done=progress.done,
            failed=progress.failed,
            phase=run.phase,
            phase_done=current.finished,
            phase_total=current.total,
            escalation_breakdown=breakdown,
            undecided_count=sum(1 for c in scored if c.decision == "undecided"),
            queue_depth_ahead=ahead,
            eta_seconds=remaining * per_resume,
            escalation_rate=round(len(escalated) / len(scored), 4) if scored else 0.0,
            failed_files=[
                FailedFile(
                    filename=f.filename,
                    phase=f.phase,
                    attempts=f.attempts,
                    last_error=f.last_error,
                )
                for f in failed
            ],
        )

    def sign_off_run(self, run_id: str, actor: Actor) -> None:
        """A named human accepting the results. The end of the flow.

        **Refuses while any escalated candidate is still undecided** (23.1.6).
        Sign-off is the artefact that says a human reviewed this run; signing one
        with 23 untouched escalations would make that artefact false at exactly
        the moment it starts being relied on. The system escalated because it
        could not decide — accepting the run without answering those is the
        oversight becoming theatre.

        A candidate still `pending` verification counts as outstanding for the
        same reason (17.6): partial verification must never look like completed
        verification, and phase 2 may yet raise an escalation nobody has seen.

        **A permanently-failed job blocks it too, and for a stronger reason.**
        Those files produced no candidate at all — `judge_one` turns document
        problems into a flagged candidate, so a job reaching `failed` means the
        model, the database or a bug stopped it being screened. Checking only
        candidates missed them entirely: the run reached `completed`, sign-off
        succeeded, and applicants who were never assessed were absent from the
        result with nothing on screen naming them. That is precisely the outcome
        this gate exists to prevent, arriving by the one path it did not inspect.
        """
        with self.uow_factory() as tx:
            self._ensure_actor(tx, actor)
            if runs_store.get(tx, run_id) is None:
                raise NotFoundError(f"run {run_id}")
            outstanding = [
                c
                for c in results_store.list_for_run(tx, run_id)
                if c.decision == "undecided"
                and (c.review_required or c.verification_status == "pending")
            ]
            if outstanding:
                raise ServiceError(
                    f"{len(outstanding)} candidate(s) need review and have no decision; "
                    "sign-off is blocked until each one is advanced, held, or rejected"
                )
            unscreened = jobs_store.failed_jobs(tx, run_id)
            if unscreened:
                names = ", ".join(f.filename for f in unscreened[:5])
                more = f" and {len(unscreened) - 5} more" if len(unscreened) > 5 else ""
                raise ServiceError(
                    f"{len(unscreened)} file(s) were never screened and are missing from "
                    f"these results: {names}{more}. Fix the cause and rescan, or abort "
                    "the run — signing off would accept a result those applicants are "
                    "absent from."
                )
            runs_store.sign_off(tx, run_id, actor.id)
            audit_store.append_for(tx, actor, "sign_off_run", "run", run_id)

    # --- candidates ----------------------------------------------------------

    def list_candidates(self, run_id: str) -> RankedResult:
        """Three partitions plus the escalation rate (10.6).

        `needs_review` is returned as its own list, never appended to the bottom
        of a ranking. At 1,000 applicants a reviewer only ever reads the top of
        Band A, so an unscoreable candidate parked at the end of one long list is
        invisible in practice.
        """
        with self.uow_factory() as tx:
            run = runs_store.get(tx, run_id)
            if run is None:
                raise NotFoundError(f"run {run_id}")
            rubric = rubrics_store.get(tx, run.rubric_id)
            candidates = results_store.list_for_run(tx, run_id)

        return rank([self._attach_rubric(c, rubric) for c in candidates])

    def get_candidate(self, candidate_id: int) -> Candidate:
        with self.uow_factory() as tx:
            candidate = results_store.get(tx, candidate_id)
            if candidate is None:
                raise NotFoundError(f"candidate {candidate_id}")
            run = runs_store.get(tx, candidate.run_id)
            rubric = rubrics_store.get(tx, run.rubric_id) if run else None
        return self._attach_rubric(candidate, rubric)

    @staticmethod
    def _attach_rubric(candidate: Candidate, rubric: Rubric | None) -> Candidate:
        """Re-attach `weight` and `must_have` from the rubric.

        They are not stored per verdict — they are rubric facts, and a second
        copy would be a second source of truth for the arithmetic. A criterion
        the rubric no longer has keeps its stored placeholder rather than being
        dropped, so a reviewer still sees what the model said.
        """
        if rubric is None:
            return candidate
        by_id = {c.id: c for c in rubric.criteria}
        return candidate.model_copy(
            update={
                "criteria": [
                    v.model_copy(
                        update={
                            "text": by_id[v.id].text,
                            "weight": by_id[v.id].weight,
                            "must_have": by_id[v.id].must_have,
                        }
                    )
                    if v.id in by_id
                    else v
                    for v in candidate.criteria
                ]
            }
        )

    def candidate_file_path(self, candidate_id: int) -> Path | None:
        """The original document on disk, or `None` if it is not under the run.

        Resolved and confined to the run's folder. The stored filename came from
        a directory scan rather than from a request, so this is not defending
        against today's input — it is making the confinement a property of the
        read path, so it stays true when someone later adds a way to write one.
        """
        with self.uow_factory() as tx:
            candidate = results_store.get(tx, candidate_id)
            if candidate is None:
                raise NotFoundError(f"candidate {candidate_id}")
            run = runs_store.get(tx, candidate.run_id)
        if run is None:
            return None

        folder = Path(run.folder).resolve()
        path = (folder / candidate.filename).resolve()
        return path if path.is_relative_to(folder) else None

    def record_decision(self, candidate_id: int, decision: str, reason: str, actor: Actor) -> None:
        """A named human deciding about a named person (12.8, 15.5).

        Three writes, one transaction: the decision on the candidate, the history
        row on `overrides`, the audit entry. Splitting them eventually leaves a
        decision nobody can attribute — and this is the record that has to answer
        an adverse-action question months later, when the only thing left is what
        was written down.

        `reason` is required by the schema rather than by convention. A rejection
        with no stated ground is not reviewable by anyone, including the reviewer
        who made it.
        """
        if decision not in ("advance", "reject", "hold"):
            raise ServiceError(f"unknown decision {decision!r}")
        if not reason.strip():
            raise ServiceError("a decision requires a reason")

        with self.uow_factory() as tx:
            self._ensure_actor(tx, actor)
            candidate = results_store.get(tx, candidate_id)
            if candidate is None:
                raise NotFoundError(f"candidate {candidate_id}")
            decided_at = now()
            tx.execute(
                "INSERT INTO overrides (candidate_id, actor_id, old_score, old_band, "
                "old_decision, new_decision, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    actor.id,
                    candidate.score,
                    candidate.band,
                    candidate.decision,
                    decision,
                    reason,
                    decided_at.isoformat(),
                ),
            )
            results_store.save_decision(
                tx, candidate_id, cast(Decision, decision), actor.id, decided_at
            )
            audit_store.append_for(
                tx,
                actor,
                "decision",
                "candidate",
                str(candidate_id),
                {
                    "decision": decision,
                    "from": candidate.decision,
                    "old_score": candidate.score,
                    "reason": reason,
                },
            )

    def record_bulk_decision(
        self, candidate_ids: list[int], decision: str, reason: str, actor: Actor
    ) -> BulkDecisionResult:
        """The same decision across many candidates, **skipping `review_required`**.

        Bulk actions exist because a reviewer working 400 clear rejections one
        modal at a time will stop reading them. The exclusion exists because the
        escalated ones are precisely those where the system said *a human has to
        look at this* — sweeping them into a shared reason would answer that
        request with a rubber stamp, and the escalation would have bought nothing.

        Skipped ids are returned rather than silently dropped: a caller that
        asked for 400 and got 377 needs to know which 23 still need opening.
        """
        skipped: list[int] = []
        applied: list[int] = []
        for candidate_id in candidate_ids:
            with self.uow_factory() as tx:
                candidate = results_store.get(tx, candidate_id)
            if candidate is None:
                raise NotFoundError(f"candidate {candidate_id}")
            if candidate.review_required or candidate.verification_status == "pending":
                # `pending` is excluded for the same reason: verification has not
                # finished, so the escalation that would have flagged this one may
                # simply not have happened yet (17.6).
                skipped.append(candidate_id)
                continue
            applied.append(candidate_id)

        for candidate_id in applied:
            self.record_decision(candidate_id, decision, reason, actor)
        return BulkDecisionResult(decided=applied, skipped=skipped)

    def purge_candidate(self, file_sha256: str, actor: Actor) -> int:
        """Erase a candidate everywhere. Returns how many files were removed.

        Database first, in one transaction; **files afterwards**. There is no
        rollback for `unlink`, so deleting inside the transaction would destroy
        data that a later abort was supposed to keep.

        The files are the failure captures of 18 — raw model output written when
        schema validation failed, which is derived from the resume and is
        therefore inside the erasure path. They are found by hash rather than
        through an index table: needing an index is what made continuous tracing
        expensive to keep correct, and a glob cannot fall out of step with the
        thing it is indexing.

        The audit stub is deliberately non-identifying: recording *that* an
        erasure happened, without re-recording the person it was about.
        """
        paths = failure_paths_for(file_sha256)
        with self.uow_factory() as tx:
            results_store.purge_candidate(tx, file_sha256)
            audit_store.append_for(
                tx,
                actor,
                "purge_candidate",
                "candidate",
                file_sha256[:12],
                {"failure_captures": len(paths)},
            )

        removed = 0
        for path in paths:
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                # Already gone. The database row is the record; the file is a copy.
                continue
        return removed

    # --- worker-facing surface -----------------------------------------------
    #
    # The worker owns no SQL. The transaction boundary is this layer (12.2), and
    # a daemon that opened its own would be a second place where a save and its
    # job-completion could drift apart — the exact divergence that makes a batch
    # either lose results or redo them.
    #
    # None of these write to `audit_log`. Per-job events are "what the system
    # did", which is the JSONL stream; `audit_log` is "who did what" and stays
    # readable by a human (17). A thousand claim rows per run would bury the
    # overrides and sign-offs that an auditor actually needs.

    def reclaim_orphaned(self, worker_id: str) -> int:
        """Recover jobs this worker held in a previous life (16.5).

        Audited *without* an actor, because no human did this. Inventing a
        synthetic user would make a crash recovery indistinguishable from a
        person's decision.
        """
        with self.uow_factory() as tx:
            recovered = jobs_store.reclaim_orphaned(tx, worker_id)
            if recovered:
                audit_store.append(
                    tx, None, "reclaim_orphaned", "worker", worker_id, {"jobs": recovered}
                )
        return recovered

    def claim_next_job(self, worker_id: str, phase: str = "judge") -> Job | None:
        """Take one job and mark its run running, in a single transaction.

        The status move belongs here rather than in the worker: a claim that
        committed while the run stayed `pending` would leave a batch that is
        demonstrably executing but reports as not started.
        """
        with self.uow_factory() as tx:
            job = jobs_store.claim_next(tx, worker_id, phase)
            if job is not None:
                run = runs_store.get(tx, job.run_id)
                if run is not None and run.status == "pending":
                    runs_store.set_status(tx, job.run_id, "running")
        return job

    def heartbeat(self, job_id: int, worker_id: str) -> None:
        with self.uow_factory() as tx:
            jobs_store.heartbeat(tx, job_id, worker_id)

    def complete_job(self, job: Job, candidate: Candidate, key: CacheKey) -> None:
        """Save the result and close the job **atomically**.

        Two writes that must not diverge. A saved candidate with an open job
        gets screened again on resume and violates `UNIQUE(file_sha256, run_id)`;
        a closed job with no candidate silently drops a real applicant from the
        run.
        """
        with self.uow_factory() as tx:
            results_store.save(tx, job.run_id, candidate, key)
            jobs_store.complete(tx, job.id, key.file_sha256)

    def complete_verify_job(self, job: Job) -> None:
        """Close a phase-2 job. The candidate was written by `save_verification`.

        Separate from `complete_job`, which also saves a candidate and a cache
        key — neither of which exists here, because phase 2 updates a row rather
        than creating one.
        """
        with self.uow_factory() as tx:
            jobs_store.complete(tx, job.id)

    def fail_job(self, job: Job, error: str, *, retryable: bool) -> None:
        with self.uow_factory() as tx:
            jobs_store.fail(tx, job.id, error, retryable)

    def release_job(self, job: Job) -> None:
        """Hand a job back on clean shutdown, without counting an attempt."""
        with self.uow_factory() as tx:
            jobs_store.release(tx, job.id)

    def cached_candidate(self, key: CacheKey) -> Candidate | None:
        """A prior judgment made under identical conditions (12.5).

        This is what makes a re-run over an unchanged folder nearly free, and
        what keeps a transient failure from being served back as a verdict —
        non-cacheable rows are invisible to this lookup by index construction.
        """
        with self.uow_factory() as tx:
            return results_store.get_cached(tx, key)

    def rubric_for_run(self, run_id: str) -> tuple[Rubric, Run]:
        with self.uow_factory() as tx:
            run = runs_store.get(tx, run_id)
            if run is None:
                raise NotFoundError(f"run {run_id}")
            rubric = rubrics_store.get(tx, run.rubric_id)
            if rubric is None:
                raise NotFoundError(f"rubric {run.rubric_id}")
        return rubric, run

    def cache_key_for(self, run: Run, rubric: Rubric, file_sha256: str) -> CacheKey:
        """All nine fields that change the output (6)."""
        return CacheKey(
            file_sha256=file_sha256,
            position_id=run.position_id,
            rubric_hash=rubric.content_hash,
            judge_digest=run.judge_digest,
            verifier_digest=run.verifier_digest or "",
            prompt_hash=run.prompt_hash,
            redaction_on=run.redaction_on,
            num_ctx=run.num_ctx,
            app_version=run.app_version,
        )

    def next_active_phase(self) -> tuple[Run, str] | None:
        """The run the worker should be working on, and which pass (17.4).

        FIFO across runs, and a run's own phase decides the model. Returning the
        phase alongside the run is what lets the worker load a model **once** and
        drain a whole queue against it, rather than discovering per job which
        weights it needs.
        """
        with self.uow_factory() as tx:
            for run in runs_store.list_active(tx):
                if run.phase in ("judge", "verify"):
                    return run, run.phase
        return None

    def save_verification(self, candidate_id: int, candidate: Candidate) -> None:
        """Persist what phase 2 concluded. Never score, band or verdict."""
        with self.uow_factory() as tx:
            results_store.save_verification(tx, candidate_id, candidate)

    def advance_phase_if_complete(self, run_id: str) -> str | None:
        """Move a run to its next phase once the current one has drained.

        Returns the new phase, or None if there was nothing to do.

        **Enqueueing phase 2 and setting the phase happen in one transaction.**
        Split, a crash between them leaves a run in `verify` with an empty queue,
        which drains instantly and completes having verified nobody — a failure
        that looks exactly like success.

        A run with verification disabled, or with no scoreable candidates to
        verify, goes straight to `done`. There is no empty middle phase.
        """
        with self.uow_factory() as tx:
            run = runs_store.get(tx, run_id)
            if run is None:
                return None

            # Closes out verify jobs a cache hit or a crash left permanently
            # unclaimable — see `close_verified_jobs`. Unconditional and cheap:
            # the UPDATE matches nothing on a run that never had any.
            jobs_store.close_verified_jobs(tx, run_id)

            if run.phase == "judge":
                if not jobs_store.no_pending(tx, run_id, "judge"):
                    return None
                enqueued = (
                    jobs_store.enqueue_verify_jobs(tx, run_id) if run.verification_enabled else 0
                )
                # A judge-phase cache hit can enqueue verify jobs that are
                # already verified before a worker ever sees them — close
                # those immediately rather than waiting for the next call.
                jobs_store.close_verified_jobs(tx, run_id)
                phase = "verify" if not jobs_store.no_pending(tx, run_id, "verify") else "done"
                runs_store.set_phase(tx, run_id, phase)
                if phase == "done":
                    # Nothing will ever verify these, so they must not be left
                    # reading as "not verified yet" — that blocks sign-off on
                    # work that is never going to happen.
                    results_store.mark_unverified_as_skipped(tx, run_id)
                audit_store.append(
                    tx, None, "advance_phase", "run", run_id, {"phase": phase, "queued": enqueued}
                )
                return phase

            if run.phase == "verify" and jobs_store.no_pending(tx, run_id, "verify"):
                runs_store.set_phase(tx, run_id, "done")
                # Unscoreable candidates never get a verify job queued at all
                # (`enqueue_verify_jobs` is `scoreable = 1` only), so `no_pending`
                # on `verify` can be true while they still sit at the initial
                # `verification_status='pending'`. Left alone that reads
                # "not checked yet" forever, the same failure this call already
                # prevents when verification never starts for the whole run.
                results_store.mark_unverified_as_skipped(tx, run_id)
                audit_store.append(tx, None, "advance_phase", "run", run_id, {"phase": "done"})
                return "done"

        return None

    def finish_run_if_complete(self, run_id: str) -> bool:
        """Close a run once nothing is pending or in flight (16.4).

        The escalation rate is written at the same moment, so the finished run
        carries the number 18.2 says nobody should have to discover candidate by
        candidate.
        """
        with self.uow_factory() as tx:
            progress = jobs_store.progress(tx, run_id)
            if not progress.is_complete:
                return False
            run = runs_store.get(tx, run_id)
            if run is None or run.status in ("completed", "aborted", "failed"):
                return False
            if run.phase != "done":
                # Phase 1 draining is not the run finishing. Completing here
                # would mark a run reviewable before a single candidate had been
                # verified, and every one of them would still read `pending`.
                return False

            scored = results_store.list_for_run(tx, run_id)
            escalated = sum(1 for c in scored if c.review_required or not c.scoreable)
            rate = round(escalated / len(scored), 4) if scored else 0.0

            runs_store.set_status(tx, run_id, "completed")
            runs_store.set_rates(tx, run_id, escalation_rate=rate, reproducibility_rate=None)
            audit_store.append(
                tx,
                None,
                "complete_run",
                "run",
                run_id,
                {"done": progress.done, "failed": progress.failed, "escalation_rate": rate},
            )
        return True

    def dashboard(self) -> DashboardSummary:
        """The three numbers, counted in one transaction.

        **One transaction, not one per number.** A dashboard assembled from six
        separate reads can show a candidate that has been decided since the count
        above it was taken, and the arithmetic stops adding up on screen for no
        reason a reader can see. Everything here is a snapshot of the same
        instant.

        **Counted in SQL, not by loading rows.** The obvious implementation —
        `list_runs()` then `list_candidates()` per run — reads every résumé in the
        database to produce three integers, because `Candidate` carries
        `resume_text` and `sent_text` (~40 KB each). At a thousand candidates that
        is tens of megabytes per page load.

        **Writes no audit row.** Reading counts is not a mutation, and a log
        entry per dashboard visit would bury the decisions an auditor is looking
        for under refreshes — the same reasoning that keeps `search_audit`
        silent.
        """
        with self.uow_factory() as tx:
            queues = []
            for run_id, count in results_store.awaiting_review_by_run(tx):
                run = runs_store.get(tx, run_id)
                position = positions_store.get(tx, run.position_id) if run else None
                queues.append(
                    ReviewQueue(
                        run_id=run_id,
                        # A run whose requisition has been deleted still holds a
                        # real queue. Naming the run is more useful than dropping
                        # the row, which would silently lose part of the total.
                        position_reference=position.reference if position else "unknown",
                        position_title=position.title if position else "",
                        awaiting_review=count,
                    )
                )
            return DashboardSummary(
                open_positions=positions_store.count_open(tx),
                applications=results_store.count_applications(tx),
                awaiting_review=results_store.count_awaiting_review(tx),
                runs_in_progress=runs_store.count_active(tx),
                unscreened_files=jobs_store.count_unscreened(tx),
                queues=queues,
            )

    # --- operations ----------------------------------------------------------

    def health(self) -> HealthReport:
        """Granular by design — "unhealthy" alone tells an operator nothing at 2am.

        Never raises. A health check that throws is not a health check: it takes
        down the thing that was supposed to report the problem.
        """
        detail: dict[str, str] = {}

        try:
            llm_reachable = self.llm.health()
        except Exception as exc:  # noqa: BLE001 — reporting, not control flow
            llm_reachable = False
            detail["llm"] = str(exc)[:200]

        # Both models are checked. A drifted verifier changes who lands in the
        # review queue rather than who scores what, but a run whose escalations
        # came from weights nobody can name is not one an auditor can defend.
        digest_ok = True
        if llm_reachable:
            for label, model, pin in (
                ("judge_digest", settings.judge_model, settings.judge_digest_pin),
                ("verifier_digest", settings.verifier_model, settings.verifier_digest_pin),
            ):
                if pin and self.llm.digest(model) != pin:
                    # Not fatal, but every decision stored under the old weights
                    # has stopped being reproducible and the operator must know.
                    digest_ok = False
                    detail[label] = f"{model}: loaded weights differ from the configured pin"

        try:
            outstanding = pending_migrations()
            migrations_current = not outstanding
            if outstanding:
                detail["migrations"] = f"{len(outstanding)} pending"
        except Exception as exc:  # noqa: BLE001 — reporting, not control flow
            migrations_current = False
            detail["migrations"] = str(exc)[:200]

        free_gb = _free_disk_gb(Path(settings.db_path).parent)
        disk_ok = free_gb >= settings.min_free_disk_gb
        if not disk_ok:
            # Stored resume text grows the database fast; the worker stops
            # claiming below this (18).
            detail["disk"] = f"{free_gb:.1f} GB free, below {settings.min_free_disk_gb} GB"

        return HealthReport(
            ok=llm_reachable and migrations_current and disk_ok,
            llm_reachable=llm_reachable,
            model_digest_matches_pin=digest_ok,
            migrations_current=migrations_current,
            free_disk_gb=round(free_gb, 2),
            disk_ok=disk_ok,
            app_version=settings.app_version,
            detail=detail,
        )

    def search_audit(
        self,
        *,
        actor_id: str = "",
        action: str = "",
        entity: str = "",
        entity_id: str = "",
        since: str = "",
        until: str = "",
        offset: int = 0,
        limit: int = 50,
    ) -> tuple[list[AuditEntry], int]:
        with self.uow_factory() as tx:
            rows, total = audit_store.search(
                tx,
                actor_id=actor_id,
                action=action,
                entity=entity,
                entity_id=entity_id,
                since=since,
                until=until,
                offset=offset,
                limit=limit,
            )
            entries = [AuditEntry(**row) for row in rows]
            return entries, total

    # Candidate events are one query each. A thousand of them against a local
    # SQLite file is survivable but pointless; past this many the story is a
    # summary of the run rather than a per-applicant record, and the reader is
    # told so. Exactness here wants one `entity_id IN (…)` query, not a bigger cap.
    CANDIDATE_EVENT_CAP = 200

    def adverse_action_record(self, candidate_id: int) -> AdverseActionRecord:
        """Everything that determined one person's outcome, in one object.

        Orchestration across four stores, and the reason it belongs here: the
        grounds for the decision live in `overrides`, the criteria and verdicts
        in `candidates`/`verdicts`, the criterion *text* in the rubric, and the
        two human gates on the rubric and run rows. Answering "why was this
        person rejected" from the UI would mean four round trips and a rejoin
        that has to agree with the one `list_candidates` already does.

        **Not restricted to rejections.** A record that exists only for adverse
        outcomes cannot be checked against a favourable one, and the asymmetry is
        exactly what an auditor would want to test.
        """
        with self.uow_factory() as tx:
            candidate = results_store.get(tx, candidate_id)
            if candidate is None:
                raise NotFoundError(f"candidate {candidate_id}")
            run = runs_store.get(tx, candidate.run_id)
            rubric = rubrics_store.get(tx, run.rubric_id) if run else None
            position = positions_store.get(tx, run.position_id) if run else None
            history = results_store.decision_history(tx, candidate_id)

        # The same rejoin `list_candidates` performs, for the same reason: weight
        # and must_have are rubric facts and are not duplicated onto verdicts.
        attached = self._attach_rubric(candidate, rubric)

        return AdverseActionRecord(
            candidate_id=candidate_id,
            filename=attached.filename,
            run_id=attached.run_id,
            position_reference=position.reference if position else "",
            decision=attached.decision,
            decided_by=attached.decided_by,
            decided_at=attached.decided_at,
            history=[DecisionRecord(**row) for row in history],
            score=attached.score,
            band=attached.band,
            must_haves_met=attached.must_haves_met,
            scoreable=attached.scoreable,
            verification_status=attached.verification_status,
            criteria=attached.criteria,
            flags=attached.flags,
            escalation_reasons=attached.escalation_reasons,
            summary=attached.summary,
            rubric_version=rubric.version if rubric else None,
            rubric_hash=rubric.content_hash if rubric else "",
            judge_digest=run.judge_digest if run else "",
            verifier_digest=run.verifier_digest if run else None,
            prompt_hash=run.prompt_hash if run else "",
            app_version=run.app_version if run else "",
            redaction_on=run.redaction_on if run else True,
            scored_at=attached.scored_at,
            rubric_approved_by=rubric.approved_by if rubric else None,
            run_signed_off_by=run.reviewed_by if run else None,
        )

    def run_story(self, run_id: str) -> RunStory:
        """Everything the audit log records about one run, in order.

        Orchestration across four stores: the run gives the rubric and position
        it descends from, `audit_store.list_for_entity` supplies each link's
        events, and the candidates supply the decisions. Assembling it here
        rather than in the UI is what keeps `separation_of_duties` a single fact
        about the record instead of a rule re-derived in a template.

        **Reads only.** Looking at the audit log is not a mutation, and a read
        that logged itself would bury the mutations an auditor came to find under
        the traffic of the page they used to find them.
        """
        with self.uow_factory() as tx:
            run = runs_store.get(tx, run_id)
            if run is None:
                raise NotFoundError(f"run {run_id}")

            position = positions_store.get(tx, run.position_id)
            rubric = rubrics_store.get(tx, run.rubric_id)

            rows = [
                *audit_store.list_for_entity(tx, "position", run.position_id),
                *audit_store.list_for_entity(tx, "rubric", run.rubric_id),
                *audit_store.list_for_entity(tx, "run", run_id),
            ]

            candidates = results_store.list_for_run(tx, run_id)
            capped = [c for c in candidates if c.id is not None][: self.CANDIDATE_EVENT_CAP]
            for candidate in capped:
                # `entity_id` is the integer id stored as text; the str() is
                # required rather than cosmetic.
                rows.extend(audit_store.list_for_entity(tx, "candidate", str(candidate.id)))

        events = sorted((AuditEntry(**row) for row in rows), key=lambda e: e.ts)
        approved_by = rubric.approved_by if rubric else None
        signed_off_by = run.reviewed_by

        return RunStory(
            run_id=run_id,
            position_reference=position.reference if position else run.position_id,
            rubric_version=rubric.version if rubric else None,
            approved_by=approved_by,
            signed_off_by=signed_off_by,
            events=events,
            separation_of_duties=bool(approved_by and signed_off_by)
            and approved_by != signed_off_by,
            candidate_events_truncated=len(candidates) > self.CANDIDATE_EVENT_CAP,
        )


def _free_disk_gb(path: Path) -> float:
    import shutil

    try:
        return shutil.disk_usage(path if path.exists() else Path.cwd()).free / 1024**3
    except OSError:
        return 0.0
