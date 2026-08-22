"""Requisitions and rubric drafting (spec 15.1).

Handlers are `def`, not `async def`. `sqlite3` is synchronous, so FastAPI runs
these in its threadpool, which is correct. `async def` with a blocking database
call inside blocks the event loop — the most common FastAPI mistake, producing
something slower than the sync version while looking more sophisticated (15.3).
"""

from fastapi import APIRouter, Depends

from screener.api.deps import (
    get_actor,
    get_service,
    to_folder,
    to_position,
    to_rubric,
)
from screener.models import Actor
from screener.schemas import (
    CreatePositionRequest,
    FolderPageResponse,
    PositionResponse,
    RubricResponse,
    SaveRubricRequest,
)
from screener.service import ScreenerService

router = APIRouter(tags=["positions"])


@router.post("/positions", response_model=PositionResponse, status_code=201)
def create_position(
    request: CreatePositionRequest,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> PositionResponse:
    position = service.create_position(
        reference=request.reference, title=request.title, jd_text=request.jd_text, actor=actor
    )
    return to_position(position)


@router.post("/positions/{position_id}/close", response_model=PositionResponse)
def close_position(
    position_id: str,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> PositionResponse:
    """Take a filled requisition off the working list. Deletes nothing.

    Declared above `/positions/{position_id}/rubric` for no reason other than
    grouping; the literal `folders` route is the one whose order matters.
    """
    return to_position(service.close_position(position_id, actor))


@router.get("/positions", response_model=list[PositionResponse])
def list_positions(
    include_closed: bool = False,
    service: ScreenerService = Depends(get_service),
) -> list[PositionResponse]:
    """Open requisitions, or all of them for screens that name a run's own."""
    return [to_position(p) for p in service.list_positions(include_closed=include_closed)]


@router.get("/positions/folders", response_model=FolderPageResponse)
def list_resume_folders(
    path: str = "",
    q: str = "",
    offset: int = 0,
    limit: int = 15,
    service: ScreenerService = Depends(get_service),
) -> FolderPageResponse:
    """One page of subfolders of `path` on the resume share, for the picker.

    Paged and filtered server-side: counting a folder's resumes is a recursive
    walk, so an unbounded listing costs thousands of filesystem operations on a
    large share — seconds per render over a network mount, repeated on every
    Streamlit interaction.

    Declared above the `/positions/{position_id}` routes so the literal segment
    is matched first — a parameterised route added later would otherwise read
    "folders" as an id and shadow this silently.
    """
    folders, total = service.list_resume_folders(path, q, offset, min(limit, 100))
    return FolderPageResponse(folders=[to_folder(f) for f in folders], total=total)


@router.post("/positions/{position_id}/rubric/extract", response_model=RubricResponse)
def extract_rubric(
    position_id: str,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> RubricResponse:
    """Synchronous, ~5 s. One request while a human waits — not a batch (14).

    The result is a **draft**. It cannot be used for a run until approved.
    """
    return to_rubric(service.extract_rubric(position_id, actor))


@router.put("/positions/{position_id}/rubric", response_model=RubricResponse, status_code=201)
def save_rubric(
    position_id: str,
    request: SaveRubricRequest,
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> RubricResponse:
    """201, not 200: this creates a new version rather than replacing one.

    A stale `base_version` raises `ConflictError` → 409, so the second of two
    people editing the same rubric is told rather than silently winning.
    """
    return to_rubric(
        service.save_rubric(position_id, request.criteria, actor, request.base_version)
    )


@router.get("/positions/{position_id}/rubric", response_model=RubricResponse | None)
def get_latest_rubric(
    position_id: str,
    service: ScreenerService = Depends(get_service),
) -> RubricResponse | None:
    rubric = service.get_latest_rubric(position_id)
    return to_rubric(rubric) if rubric else None


@router.get("/positions/{position_id}/rubric/approved", response_model=RubricResponse | None)
def get_approved_rubric(
    position_id: str,
    service: ScreenerService = Depends(get_service),
) -> RubricResponse | None:
    rubric = service.get_approved_rubric(position_id)
    return to_rubric(rubric) if rubric else None
