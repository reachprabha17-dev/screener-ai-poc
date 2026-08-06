"""Request dependencies and domain→response mapping (spec §15.2, §15.4).

**Auth is stubbed but seamed.** `get_actor` is one function, and every mutating
route already takes `actor: Actor = Depends(get_actor)`. Real LDAP or argon2
lands inside that single function; nothing else in the codebase changes. Threading
`actor` through every signature is what is expensive to retrofit — the
authentication itself is a day's work.

**The stub trusts a header.** Anything that can reach this port is therefore an
admin, which is why the API binds to loopback and why `auth_mode` must be moved
off `stub` before an external listener exists.

The mappers are here rather than on the models because they enforce §15.4's
outward boundary in one place: `model_verdict` is dropped when a `ScoredCriterion`
becomes a `CriterionResponse`, and there is no other path from a domain object to
an HTTP body.
"""

from functools import lru_cache

from fastapi import Header, HTTPException, status

from config.settings import settings
from screener.clients.ollama_client import OllamaClient
from screener.models import (
    Actor,
    Candidate,
    HealthReport,
    Position,
    RankedResult,
    Rubric,
    Run,
    RunStatus,
)
from screener.schemas import (
    CandidateResponse,
    CriterionResponse,
    HealthResponse,
    PositionResponse,
    RankedResponse,
    RubricResponse,
    RunResponse,
    RunStatusResponse,
)
from screener.service import ScreenerService


def get_actor(x_actor: str | None = Header(default=None)) -> Actor:
    """Who is making this request.

    Stubbed today. The `X-Actor` header is honoured so a PoC can demonstrate two
    different people approving and signing off — separation of duties is the
    thing an audit trail exists to record, and a hardcoded single identity would
    make it untestable.
    """
    if settings.auth_mode == "stub":
        actor_id = x_actor or settings.dev_actor_id
        return Actor(id=actor_id, display_name=actor_id, roles=frozenset({"admin"}))
    # LDAP / argon2 lands here — one function, no call-site changes.
    raise HTTPException(
        status_code=status.HTTP_501_NOT_IMPLEMENTED,
        detail=f"auth_mode={settings.auth_mode} is not implemented",
    )


@lru_cache(maxsize=1)
def _shared_service() -> ScreenerService:
    """One service per process.

    The LLM client caches the model digest and is safe to share; database
    connections are *not* shared — `get_connection()` creates one per thread,
    which is why FastAPI's threadpool works at all (§12.3).
    """
    return ScreenerService(llm=OllamaClient())


def get_service() -> ScreenerService:
    return _shared_service()


# --- domain → response -------------------------------------------------------


def to_position(position: Position) -> PositionResponse:
    return PositionResponse(
        id=position.id,
        reference=position.reference,
        title=position.title,
        created_by=position.created_by,
        created_at=position.created_at,
    )


def to_rubric(rubric: Rubric) -> RubricResponse:
    return RubricResponse(
        id=rubric.id,
        position_id=rubric.position_id,
        version=rubric.version,
        criteria=rubric.criteria,
        rubric_hash=rubric.content_hash,
        created_by=rubric.created_by,
        approved_by=rubric.approved_by,
        approved_at=rubric.approved_at,
    )


def to_run(run: Run) -> RunResponse:
    return RunResponse(
        id=run.id,
        position_id=run.position_id,
        rubric_id=run.rubric_id,
        folder=run.folder,
        status=run.status,
        model_digest=run.model_digest,
        prompt_hash=run.prompt_hash,
        app_version=run.app_version,
    )


def to_run_status(status_: RunStatus) -> RunStatusResponse:
    return RunStatusResponse(**status_.model_dump())


def to_candidate(candidate: Candidate) -> CandidateResponse:
    """The §15.4 boundary. `model_verdict` is dropped here and nowhere else."""
    return CandidateResponse(
        filename=candidate.filename,
        file_sha256=candidate.file_sha256,
        score=candidate.score,
        band=candidate.band,
        must_haves_met=candidate.must_haves_met,
        criteria=[
            CriterionResponse(
                id=c.id,
                verdict=c.verdict,
                evidence=c.evidence,
                verified=c.verified,
                match_ratio=c.match_ratio,
                longest_span=c.longest_span,
                weight=c.weight,
                must_have=c.must_have,
            )
            for c in candidate.criteria
        ],
        notable_strengths=candidate.notable_strengths,
        red_flags=candidate.red_flags,
        summary=candidate.summary,
        flags=[f.value for f in candidate.flags],
        scoreable=candidate.scoreable,
        review_required=candidate.review_required,
        scored_at=candidate.scored_at,
    )


def to_ranked(result: RankedResult) -> RankedResponse:
    return RankedResponse(
        meets_must_haves=[to_candidate(c) for c in result.meets_must_haves],
        missing_must_have=[to_candidate(c) for c in result.missing_must_have],
        needs_review=[to_candidate(c) for c in result.needs_review],
        escalation_rate=result.escalation_rate,
    )


def to_health(report: HealthReport) -> HealthResponse:
    return HealthResponse(**report.model_dump())
