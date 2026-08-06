"""Transactional command/query surface (spec §14).

Called by the API, the CLI, and the worker — three callers, one implementation,
so every business rule is exercisable without an HTTP client. That is the test
§15.4 sets for the route handlers, and it only holds if the rules live here.

**Three rules shape every method below.**

*Every mutating method takes an `actor` and writes its audit row inside the same
transaction as its effect.* Not afterwards, not in a wrapper. A crash between the
two leaves an override with no record of who made it — a decision affecting a
person that nobody can be held to.

*No method contains inference.* `extract_rubric` is the single LLM call here, and
it is one ~5 s request made while a human waits, not a batch. **This module never
imports `pipeline.py`.** It enqueues; the worker executes. If the service layer
ever starts screening, the process boundaries in §2 have collapsed and the API
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
from dataclasses import dataclass
from pathlib import Path

from config.settings import settings
from screener.core.rank import rank
from screener.llm.extract_rubric import extract_rubric as run_extraction
from screener.llm.judge_resume import judge_prompt_hash
from screener.models import (
    Actor,
    Candidate,
    Criterion,
    HealthReport,
    Position,
    RankedResult,
    Rubric,
    Run,
    RunStatus,
    now,
)
from screener.ports import CacheKey, Job, LLMClient
from screener.storage import (
    audit_store,
    jobs_store,
    positions_store,
    results_store,
    rubrics_store,
    runs_store,
    traces_store,
)
from screener.storage.connection import pending_migrations
from screener.storage.uow import Tx, UnitOfWork, unit_of_work


class ServiceError(RuntimeError):
    """A rule was violated. Distinct from an infrastructure failure."""


class NotFoundError(ServiceError):
    pass


class RubricNotApprovedError(ServiceError):
    """A run may not be created against an unapproved rubric.

    The approval is the human gate in front of an LLM-generated rubric (§9.1).
    Without it, a hallucinated requirement silently rejects every applicant who
    lacks something the job never asked for — across the whole run, leaving no
    trace in any individual result.
    """


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

        Auth is stubbed today (§15.2), so actors appear here on first use. When
        LDAP or local auth lands this becomes a lookup against the real
        directory rather than an insert, and no call site changes — which is the
        retrofit this plumbing exists to make cheap.
        """
        positions_store.seed_user(tx, actor.id, actor.display_name or actor.id)

    # --- positions -----------------------------------------------------------

    def create_position(
        self, *, reference: str, title: str, jd_text: str, actor: Actor
    ) -> Position:
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
            positions_store.create(tx, position)
            audit_store.append_for(
                tx, actor, "create_position", "position", position.id, {"reference": reference}
            )
        return position

    def list_positions(self) -> list[Position]:
        with self.uow_factory() as tx:
            return positions_store.list_open(tx)

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
                    "model_digest": self.llm.model_digest,
                },
            )
        return rubric

    def save_rubric(self, position_id: str, criteria: list[Criterion], actor: Actor) -> Rubric:
        """Store a reviewer's edited rubric as a **new version**.

        Never an in-place edit. A run records the `rubric_id` it used and
        candidates carry the `rubric_hash`; rewriting a rubric already scored
        against would make those stored decisions unexplainable, showing a
        reviewer verdicts against criteria that no longer exist.
        """
        with self.uow_factory() as tx:
            self._ensure_actor(tx, actor)
            if positions_store.get(tx, position_id) is None:
                raise NotFoundError(f"position {position_id}")
            rubric = Rubric(
                id=f"rub-{uuid.uuid4().hex[:12]}",
                position_id=position_id,
                version=rubrics_store.next_version(tx, position_id),
                criteria=criteria,
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
        """The human gate. Recorded with who and when, because it is a decision."""
        with self.uow_factory() as tx:
            self._ensure_actor(tx, actor)
            rubric = rubrics_store.get(tx, rubric_id)
            if rubric is None:
                raise NotFoundError(f"rubric {rubric_id}")
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
        model_digest = self.llm.model_digest
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

            folder = Path(settings.resumes_dir) / position.reference
            runs_store.create(
                tx,
                run_id=run_id,
                position_id=position_id,
                rubric_id=rubric_id,
                folder=str(folder),
                created_by=actor.id,
                model_name=settings.chat_model,
                model_digest=model_digest,
                prompt_hash=prompt_hash,
                redaction_on=settings.redact_pii,
                num_ctx=settings.num_ctx,
                num_predict=settings.num_predict,
                seed=settings.seed,
                app_version=settings.app_version,
            )
            queued = jobs_store.snapshot_folder(tx, run_id, folder)
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
            audit_store.append_for(tx, actor, "rescan_run", "run", run_id, {"added": added})
        return added

    def start_run(self, run_id: str, actor: Actor) -> int:
        """Mark a run ready. **Enqueues only** — the worker executes (§15.3).

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
        """Stop a run. Resumable — in-flight jobs go back to pending (§16.4)."""
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
        lost 78 minutes, and §18.2 exists because that is when the control
        silently stops working.
        """
        with self.uow_factory() as tx:
            run = runs_store.get(tx, run_id)
            if run is None:
                raise NotFoundError(f"run {run_id}")
            progress = jobs_store.progress(tx, run_id)
            ahead = jobs_store.queue_depth_ahead(tx, run_id)
            scored = results_store.list_for_run(tx, run_id)

        escalated = sum(1 for c in scored if c.review_required or not c.scoreable)
        return RunStatus(
            run_id=run_id,
            status=run.status,
            total=progress.total,
            pending=progress.pending,
            claimed=progress.claimed,
            done=progress.done,
            failed=progress.failed,
            queue_depth_ahead=ahead,
            eta_seconds=(progress.pending + progress.claimed + ahead) * settings.seconds_per_resume,
            escalation_rate=round(escalated / len(scored), 4) if scored else 0.0,
        )

    def sign_off_run(self, run_id: str, actor: Actor) -> None:
        """A named human accepting the results. The end of the §23 flow."""
        with self.uow_factory() as tx:
            self._ensure_actor(tx, actor)
            if runs_store.get(tx, run_id) is None:
                raise NotFoundError(f"run {run_id}")
            runs_store.sign_off(tx, run_id, actor.id)
            audit_store.append_for(tx, actor, "sign_off_run", "run", run_id)

    # --- candidates ----------------------------------------------------------

    def list_candidates(self, run_id: str) -> RankedResult:
        """Three partitions plus the escalation rate (§10.6).

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

    def record_override(self, candidate_id: int, decision: str, reason: str, actor: Actor) -> None:
        """A human overruling the system. Override and audit land together.

        The atomicity is the point: without it you eventually hold an override
        with no audit trail — a decision about a person with no record of who
        made it. `reason` is required by the schema for the same reason.
        """
        if decision not in ("advance", "reject", "hold"):
            raise ServiceError(f"unknown decision {decision!r}")
        if not reason.strip():
            raise ServiceError("an override requires a reason")

        with self.uow_factory() as tx:
            self._ensure_actor(tx, actor)
            candidate = results_store.get(tx, candidate_id)
            if candidate is None:
                raise NotFoundError(f"candidate {candidate_id}")
            tx.execute(
                "INSERT INTO overrides (candidate_id, actor_id, old_score, old_band, "
                "new_decision, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    candidate_id,
                    actor.id,
                    candidate.score,
                    candidate.band,
                    decision,
                    reason,
                    now().isoformat(),
                ),
            )
            audit_store.append_for(
                tx,
                actor,
                "override",
                "candidate",
                str(candidate_id),
                {"decision": decision, "old_score": candidate.score, "reason": reason},
            )

    def purge_candidate(self, file_sha256: str, actor: Actor) -> int:
        """Erase a candidate everywhere. Returns how many trace files were removed.

        Database first, in one transaction; **files afterwards**. There is no
        rollback for `unlink`, so deleting inside the transaction would destroy
        data that a later abort was supposed to keep.

        The audit stub is deliberately non-identifying: recording *that* an
        erasure happened, without re-recording the person it was about.
        """
        with self.uow_factory() as tx:
            trace_paths = results_store.purge_candidate(tx, file_sha256)
            audit_store.append_for(
                tx,
                actor,
                "purge_candidate",
                "candidate",
                file_sha256[:12],
                {"traces": len(trace_paths)},
            )

        removed = 0
        for path in trace_paths:
            try:
                path.unlink()
                removed += 1
            except FileNotFoundError:
                # Already gone. The index is the record; the file is the copy.
                continue
        return removed

    # --- worker-facing surface -----------------------------------------------
    #
    # The worker owns no SQL. The transaction boundary is this layer (§12.2), and
    # a daemon that opened its own would be a second place where a save and its
    # job-completion could drift apart — the exact divergence that makes a batch
    # either lose results or redo them.
    #
    # None of these write to `audit_log`. Per-job events are "what the system
    # did", which is the JSONL stream; `audit_log` is "who did what" and stays
    # readable by a human (§17). A thousand claim rows per run would bury the
    # overrides and sign-offs that an auditor actually needs.

    def reclaim_orphaned(self, worker_id: str) -> int:
        """Recover jobs this worker held in a previous life (§16.5).

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

    def claim_next_job(self, worker_id: str) -> Job | None:
        """Take one job and mark its run running, in a single transaction.

        The status move belongs here rather than in the worker: a claim that
        committed while the run stayed `pending` would leave a batch that is
        demonstrably executing but reports as not started.
        """
        with self.uow_factory() as tx:
            job = jobs_store.claim_next(tx, worker_id)
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

    def fail_job(self, job: Job, error: str, *, retryable: bool) -> None:
        with self.uow_factory() as tx:
            jobs_store.fail(tx, job.id, error, retryable)

    def release_job(self, job: Job) -> None:
        """Hand a job back on clean shutdown, without counting an attempt."""
        with self.uow_factory() as tx:
            jobs_store.release(tx, job.id)

    def record_trace(self, run_id: str, file_sha256: str, path: Path) -> None:
        """Index a trace file so `purge_candidate` can find it.

        The write and this call must both happen. A trace on disk with no row
        is resume text nothing knows about — erasure would report success while
        leaving a full copy behind (§17).
        """
        with self.uow_factory() as tx:
            traces_store.record(tx, run_id, file_sha256, path)

    def cached_candidate(self, key: CacheKey) -> Candidate | None:
        """A prior judgment made under identical conditions (§12.5).

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
        """All eight fields that change the output (§6)."""
        return CacheKey(
            file_sha256=file_sha256,
            position_id=run.position_id,
            rubric_hash=rubric.content_hash,
            model_digest=run.model_digest,
            prompt_hash=run.prompt_hash,
            redaction_on=run.redaction_on,
            num_ctx=run.num_ctx,
            app_version=run.app_version,
        )

    def finish_run_if_complete(self, run_id: str) -> bool:
        """Close a run once nothing is pending or in flight (§16.4).

        The escalation rate is written at the same moment, so the finished run
        carries the number §18.2 says nobody should have to discover candidate by
        candidate.
        """
        with self.uow_factory() as tx:
            progress = jobs_store.progress(tx, run_id)
            if not progress.is_complete:
                return False
            run = runs_store.get(tx, run_id)
            if run is None or run.status in ("completed", "aborted", "failed"):
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

        digest_ok = True
        if llm_reachable and settings.model_digest_pin:
            digest_ok = self.llm.model_digest == settings.model_digest_pin
            if not digest_ok:
                # Not fatal, but every decision stored under the old weights has
                # stopped being reproducible and the operator must know.
                detail["model_digest"] = "loaded weights differ from the configured pin"

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
            # Traces grow fast; the worker stops claiming below this (§17).
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

    def trace_count(self) -> int:
        with self.uow_factory() as tx:
            return traces_store.count(tx)


def _free_disk_gb(path: Path) -> float:
    import shutil

    try:
        return shutil.disk_usage(path if path.exists() else Path.cwd()).free / 1024**3
    except OSError:
        return 0.0
