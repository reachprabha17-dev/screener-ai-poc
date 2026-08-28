"""Deployment policy the interface has to agree with (spec 7, 15.1).

**The first server→browser configuration channel in this app, and it exists
because there was none.** `bulk_decision_enabled` has sat in `config/settings.py`
since it was written, read by no code at all, while `BulkDecisionForm.tsx`
reimplements the rule it describes client-side. A setting the interface cannot
see is a setting that gets reimplemented, drifts, and eventually disagrees with
itself. `jd_intake_mode` needed a real one.

**Not part of `/health`.** That endpoint answers "can this process do work" and
is polled every thirty seconds; this one answers "what is this deployment
configured to allow" and changes only on restart. Different question, different
cache lifetime, different consumers.

**This endpoint is served to every browser that can reach the port**, so nothing
derived from a secret, a filesystem path, or a hostname may be added to it. The
test for whether a value belongs here is whether you would put it in the page
source, because that is where it ends up.

Answering the config does not require an actor, but the route takes one anyway:
every route in this app does, and a single exception is how the next one gets
argued for.
"""

from fastapi import APIRouter, Depends

from config.settings import settings
from screener.api.deps import get_actor
from screener.models import Actor
from screener.schemas import ConfigResponse

router = APIRouter(tags=["ops"])


@router.get("/config", response_model=ConfigResponse)
def get_config(actor: Actor = Depends(get_actor)) -> ConfigResponse:
    """What this deployment allows. Read-only, and cheap enough to cache hard.

    Reads `settings` directly rather than through a service method. 14 forbids
    pass-through methods, and one that only copied four config values into a
    response would be exactly that — a layer to maintain that adds no
    transaction, no audit row and no rule. `deps.py` already reads `settings`
    for `auth_mode`, which is the same call.
    """
    return ConfigResponse(
        jd_intake_mode=settings.jd_intake_mode,
        jd_max_file_bytes=settings.jd_max_file_bytes,
        jd_max_pages=settings.jd_max_pages,
        allowed_extensions=list(settings.allowed_extensions),
    )
