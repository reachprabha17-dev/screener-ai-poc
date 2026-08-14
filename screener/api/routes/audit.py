from fastapi import APIRouter, Depends, HTTPException, status

from screener.api.deps import get_actor, get_service, to_audit_entry
from screener.models import Actor
from screener.schemas import AUDITOR_ROLE, AuditPageResponse
from screener.service import ScreenerService

router = APIRouter()


@router.get("/audit", response_model=AuditPageResponse)
def search_audit(
    actor_id: str = "",
    action: str = "",
    entity: str = "",
    entity_id: str = "",
    since: str = "",
    until: str = "",
    offset: int = 0,
    limit: int = 50,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> AuditPageResponse:
    """The audit log names who did what. Requires the auditor role."""
    if AUDITOR_ROLE not in actor.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Requires the auditor role",
        )

    limit = min(limit, 200)
    entries, total = service.search_audit(
        actor_id=actor_id,
        action=action,
        entity=entity,
        entity_id=entity_id,
        since=since,
        until=until,
        offset=offset,
        limit=limit,
    )
    return AuditPageResponse(
        entries=[to_audit_entry(e) for e in entries],
        total=total,
    )
