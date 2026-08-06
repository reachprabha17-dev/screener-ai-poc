"""Candidate reads, overrides, and erasure (spec §15.1)."""

from fastapi import APIRouter, Depends

from screener.api.deps import get_actor, get_service, to_candidate
from screener.models import Actor
from screener.schemas import CandidateResponse, CountResponse, OverrideRequest
from screener.service import ScreenerService

router = APIRouter(tags=["candidates"])


@router.get("/candidates/{candidate_id}", response_model=CandidateResponse)
def get_candidate(
    candidate_id: int,
    service: ScreenerService = Depends(get_service),
) -> CandidateResponse:
    return to_candidate(service.get_candidate(candidate_id))


@router.post("/candidates/{candidate_id}/override", status_code=204)
def record_override(
    candidate_id: int,
    request: OverrideRequest,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> None:
    """A human overruling the system. Never reversible silently — it is appended."""
    service.record_override(candidate_id, request.decision, request.reason, actor)


@router.delete("/candidates/{file_sha256}", response_model=CountResponse)
def purge_candidate(
    file_sha256: str,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> CountResponse:
    """Erase a candidate everywhere, trace files included (§12.6).

    Kept unauthenticated-but-present in the PoC deliberately: it is what makes
    the data disposable, and it is cheap now and expensive to retrofit (§21).
    """
    return CountResponse(count=service.purge_candidate(file_sha256, actor))
