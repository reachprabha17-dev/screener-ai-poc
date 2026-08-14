"""Candidate reads, decisions, the original file, and erasure (spec 15.1, 16)."""

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse

from screener.api.deps import get_actor, get_service, to_adverse_action, to_candidate
from screener.models import Actor
from screener.schemas import (
    AUDITOR_ROLE,
    AdverseActionResponse,
    BulkDecisionRequest,
    BulkDecisionResponse,
    CandidateAuditResponse,
    CandidateResponse,
    CountResponse,
    DecisionRequest,
)
from screener.service import ScreenerService

router = APIRouter(tags=["candidates"])


@router.get("/candidates/{candidate_id}", response_model=None)
def get_candidate(
    candidate_id: int,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> CandidateResponse | CandidateAuditResponse:
    """`response_model=None` because the shape depends on who is asking (15.2).

    Declaring the base model here would re-validate the auditor's response into
    it and drop every audit field — the failure being that role scoping still
    *looks* implemented while returning the recruiter view to everyone. The
    boundary is not weakened by removing it: `to_candidate` returns one of two
    explicit models and there is no path around it.
    """
    return to_candidate(service.get_candidate(candidate_id), actor)


@router.get("/candidates/{candidate_id}/file")
def get_candidate_file(
    candidate_id: int,
    service: ScreenerService = Depends(get_service),
) -> FileResponse:
    """The original PDF/DOCX, for a reviewer who wants to see the real document.

    `FileResponse` streams through the ASGI server rather than reading the file
    into the handler. Four reviewers opening 5 MB PDFs is an ordinary Tuesday,
    and `read()` would put all of it in the API's heap at once for no benefit.

    The path is resolved and checked against the run's folder before it is
    served. It comes from a database row that was written from a directory scan,
    not from the request — but a path traversal that only becomes exploitable
    after someone adds a route that writes one is still a path traversal, and the
    check costs a syscall.
    """
    path = service.candidate_file_path(candidate_id)
    if path is None or not path.is_file():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="file not found")
    return FileResponse(
        path,
        filename=Path(path).name,
        media_type="application/octet-stream",
    )


@router.post("/candidates/{candidate_id}/decision", status_code=204)
def record_decision(
    candidate_id: int,
    request: DecisionRequest,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> None:
    """A human deciding. Appended to history, never silently reversible."""
    service.record_decision(candidate_id, request.decision, request.reason, actor)


@router.post("/candidates/decisions", response_model=BulkDecisionResponse)
def record_bulk_decision(
    request: BulkDecisionRequest,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> BulkDecisionResponse:
    """One decision across many candidates, **excluding those needing review**.

    The response names the skipped ids rather than reporting a count, because
    "23 of your 400 still need opening" is only actionable if you know which 23.
    """
    result = service.record_bulk_decision(
        request.candidate_ids, request.decision, request.reason, actor
    )
    return BulkDecisionResponse(decided=result.decided, skipped=result.skipped)


@router.delete("/candidates/{file_sha256}", response_model=CountResponse)
def purge_candidate(
    file_sha256: str,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> CountResponse:
    """Erase a candidate everywhere, failure captures included (12.6).

    Kept unauthenticated-but-present in the PoC deliberately: it is what makes
    the data disposable, and it is cheap now and expensive to retrofit (21).
    """
    return CountResponse(count=service.purge_candidate(file_sha256, actor))


@router.get("/candidates/{candidate_id}/record", response_model=AdverseActionResponse)
def adverse_action_record(
    candidate_id: int,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> AdverseActionResponse:
    """Why this person got this outcome — the record handed to a regulator.

    **Auditor role required**, like the other two audit reads. It carries
    `model_verdict` (what the judge said before the consistency gate forced it
    down) and the stated grounds for the decision, which 15.4 keeps off the
    reviewer's screen for a reason: showing both the forced verdict and the
    original invites the second-guessing the gate exists to remove.

    Available for every decision, not only rejections. A record produced solely
    for adverse outcomes cannot be checked against a favourable one, and that
    comparison is the first thing an auditor would want to make.
    """
    if AUDITOR_ROLE not in actor.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires the auditor role",
        )
    record = service.adverse_action_record(candidate_id)
    return to_adverse_action(record, service.get_candidate(candidate_id))
