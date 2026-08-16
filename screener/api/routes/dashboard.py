"""The overview screen's numbers (spec 15.4, v7 §5.3).

**One endpoint, one transaction, counts only.** The alternative — the client
calling `/positions`, `/runs`, and `/runs/{id}/candidates` per run and adding up
the results — reads every résumé in the database to produce three integers, and
shows numbers taken at three different instants that visibly fail to add up.

**No role gate, deliberately, and it is a property to keep.** Nothing here names
a person, a file or a reason: the size of a review queue is not a disclosure
about the people in it. The moment a field on this response would be, it belongs
behind the `auditor` check that `GET /audit` and `GET /runs/{id}/story` use.
"""

from fastapi import APIRouter, Depends

from screener.api.deps import get_service, to_dashboard
from screener.schemas import DashboardResponse
from screener.service import ScreenerService

router = APIRouter(tags=["ops"])


@router.get("/dashboard", response_model=DashboardResponse)
def dashboard(service: ScreenerService = Depends(get_service)) -> DashboardResponse:
    return to_dashboard(service.dashboard())
