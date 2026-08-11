"""Request dependencies and domain→response mapping (spec 15.2, 15.4).

**Auth is stubbed but seamed.** `get_actor` is one function, and every mutating
route already takes `actor: Actor = Depends(get_actor)`. Real LDAP or argon2
lands inside that single function; nothing else in the codebase changes. Threading
`actor` through every signature is what is expensive to retrofit — the
authentication itself is a day's work.

**The stub trusts a header.** Anything that can reach this port is therefore an
admin, which is why the API binds to loopback and why `auth_mode` must be moved
off `stub` before an external listener exists.

The mappers are here rather than on the models because they enforce 15.2's
outward boundary in one place: a `Candidate` becomes a response only by passing
through `to_candidate`, which picks the recruiter or auditor view from the
actor's roles. There is no other path from a domain object to an HTTP body.
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
    HealthResponse,
    PositionResponse,
    RankedResponse,
    RubricResponse,
    RunResponse,
    RunStatusResponse,
    candidate_response,
)
from screener.service import ScreenerService


def get_actor(
    x_actor: str | None = Header(default=None),
    x_actor_roles: str | None = Header(default=None),
) -> Actor:
    """Who is making this request.

    Stubbed today. The `X-Actor` header is honoured so a PoC can demonstrate two
    different people approving and signing off — separation of duties is the
    thing an audit trail exists to record, and a hardcoded single identity would
    make it untestable.

    `X-Actor-Roles` exists for the same reason applied to 15.2: role-scoped field
    exposure that cannot be exercised is a control nobody can show works. It is a
    trusted header, which is acceptable only while the port is on loopback and
    `auth_mode` is `stub` — the same assumption the identity header already makes.
    """
    if settings.auth_mode == "stub":
        actor_id = x_actor or settings.dev_actor_id
        roles = (
            frozenset(r.strip() for r in x_actor_roles.split(",") if r.strip())
            if x_actor_roles
            else frozenset({"admin"})
        )
        return Actor(id=actor_id, display_name=actor_id, roles=roles)
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
    which is why FastAPI's threadpool works at all (12.3).
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
        judge_digest=run.judge_digest,
        prompt_hash=run.prompt_hash,
        app_version=run.app_version,
        created_at=run.created_at,
        created_by=run.created_by,
        file_count=run.file_count,
        escalation_rate=run.escalation_rate,
    )




def to_run_status(status_: RunStatus) -> RunStatusResponse:
    return RunStatusResponse(**status_.model_dump())


def to_candidate(candidate: Candidate, actor: Actor) -> CandidateResponse:
    """The 15.2 boundary. Which fields leave depends on who is asking.

    A thin forward to `schemas.candidate_response`, kept so every route reaches
    the boundary through the same name it always has. The role check itself lives
    beside the view types, next to the table it implements.
    """
    return candidate_response(candidate, actor)


def to_ranked(result: RankedResult, actor: Actor) -> RankedResponse:
    return RankedResponse(
        meets_must_haves=[to_candidate(c, actor) for c in result.meets_must_haves],
        missing_must_have=[to_candidate(c, actor) for c in result.missing_must_have],
        needs_review=[to_candidate(c, actor) for c in result.needs_review],
        escalation_rate=result.escalation_rate,
    )


def to_health(report: HealthReport) -> HealthResponse:
    return HealthResponse(**report.model_dump())
