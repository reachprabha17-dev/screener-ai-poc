"""Runs — created, started, watched, aborted, signed off (spec §15.1).

**Every route here enqueues or reports. None of them screen.** Screening never
runs in a request lifecycle and never in `BackgroundTasks`: a 78-minute batch
tied to a request loses orphan reclaim and resumption, and dies with the process
(§15.3). `POST /runs/{id}/start` marks a run ready and returns immediately; the
daemon picks it up.

Progress is **polling**, not WebSockets. A job updating every few seconds over
78 minutes does not justify the complexity (§22.2).
"""

from fastapi import APIRouter, Depends

from screener.api.deps import get_actor, get_service, to_ranked, to_run, to_run_status
from screener.models import Actor
from screener.schemas import (
    CountResponse,
    CreateRunRequest,
    RankedResponse,
    RunResponse,
    RunStatusResponse,
)
from screener.service import ScreenerService

router = APIRouter(tags=["runs"])


@router.post("/runs", response_model=RunResponse, status_code=201)
def create_run(
    request: CreateRunRequest,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> RunResponse:
    """Snapshots the position's folder into jobs. A run is a fixed set (§16.2)."""
    return to_run(service.create_run(request.position_id, request.rubric_id, actor))


@router.post("/runs/{run_id}/start", response_model=CountResponse)
def start_run(
    run_id: str,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> CountResponse:
    return CountResponse(count=service.start_run(run_id, actor))


@router.post("/runs/{run_id}/rescan", response_model=CountResponse)
def rescan_run(
    run_id: str,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> CountResponse:
    """Explicit. Nothing is ever silently added to a run in progress."""
    return CountResponse(count=service.rescan_run(run_id, actor))


@router.post("/runs/{run_id}/abort", status_code=204)
def abort_run(
    run_id: str,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> None:
    service.abort_run(run_id, actor)


@router.get("/runs/{run_id}/status", response_model=RunStatusResponse)
def run_status(
    run_id: str,
    service: ScreenerService = Depends(get_service),
) -> RunStatusResponse:
    return to_run_status(service.run_status(run_id))


@router.get("/runs/{run_id}/candidates", response_model=RankedResponse)
def list_candidates(
    run_id: str,
    service: ScreenerService = Depends(get_service),
) -> RankedResponse:
    return to_ranked(service.list_candidates(run_id))


@router.post("/runs/{run_id}/sign-off", status_code=204)
def sign_off_run(
    run_id: str,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> None:
    """A named human accepting the results."""
    service.sign_off_run(run_id, actor)
