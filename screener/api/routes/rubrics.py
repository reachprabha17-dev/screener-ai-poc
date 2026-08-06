"""Rubric approval — the human gate in front of an LLM (spec 9.1, 15.1)."""

from fastapi import APIRouter, Depends

from screener.api.deps import get_actor, get_service, to_rubric
from screener.models import Actor
from screener.schemas import RubricResponse
from screener.service import ScreenerService

router = APIRouter(tags=["rubrics"])


@router.post("/rubrics/{rubric_id}/approve", response_model=RubricResponse)
def approve_rubric(
    rubric_id: str,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> RubricResponse:
    """Recorded with who and when, because it is a decision a person owns.

    Without it a hallucinated requirement would reject every applicant lacking
    something the job never asked for — across a whole run, invisibly.
    """
    return to_rubric(service.approve_rubric(rubric_id, actor))
