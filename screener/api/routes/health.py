"""Liveness and readiness (spec 15.1, 17).

Two endpoints because they answer different questions. `/health` says the process
is up and can serve. `/ready` says it can do useful work: the model is reachable,
migrations are current, and there is disk headroom for the stored resume text.

Conflating them means a full disk either takes the process out of rotation
entirely, or goes unnoticed until a batch fills it.
"""

from fastapi import APIRouter, Depends, Response, status

from screener.api.deps import get_service, to_health
from screener.schemas import HealthResponse
from screener.service import ScreenerService

router = APIRouter(tags=["ops"])


@router.get("/health", response_model=HealthResponse)
def health(service: ScreenerService = Depends(get_service)) -> HealthResponse:
    return to_health(service.health())


@router.get("/ready", response_model=HealthResponse)
def ready(response: Response, service: ScreenerService = Depends(get_service)) -> HealthResponse:
    """503 when not ready, so a load balancer or `systemctl` can act on it."""
    report = service.health()
    if not report.ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return to_health(report)
