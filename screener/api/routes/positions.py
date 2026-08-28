"""Requisitions and rubric drafting (spec 15.1).

Handlers are `def`, not `async def`. The database driver is synchronous, so FastAPI runs
these in its threadpool, which is correct. `async def` with a blocking database
call inside blocks the event loop — the most common FastAPI mistake, producing
something slower than the sync version while looking more sophisticated (15.3).
"""

from fastapi import APIRouter, Depends, File, UploadFile

from screener.api.deps import (
    get_actor,
    get_service,
    to_folder,
    to_jd_extraction,
    to_position,
    to_rubric,
)
from screener.models import Actor
from screener.schemas import (
    CreatePositionRequest,
    FolderPageResponse,
    JdDocumentResponse,
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
        reference=request.reference,
        title=request.title,
        jd_text=request.jd_text,
        actor=actor,
        jd_source=request.jd_source,
        jd_filename=request.jd_filename,
        jd_file_sha256=request.jd_file_sha256,
        jd_ocr_used=request.jd_ocr_used,
    )
    return to_position(position)


@router.post("/jd-documents", response_model=JdDocumentResponse)
def extract_jd_document(
    file: UploadFile = File(...),
    actor: Actor = Depends(get_actor),
    service: ScreenerService = Depends(get_service),
) -> JdDocumentResponse:
    """A job-description PDF/DOCX → the text inside it. **Creates nothing.**

    200, not 201: no resource comes into existence here. The reviewer reads the
    text, corrects whatever the parser got wrong, and submits it to
    `POST /positions` like any other description — so upload and paste converge
    on one path, and the text stored against a requisition is always text a
    person accepted.

    Showing them the extraction is the point rather than a nicety. A two-column
    layout that interleaves, or a scan whose OCR dropped a "not", produces a
    perfectly plausible rubric, and the approval gate in front of that rubric
    cannot catch it — the reviewer has nothing to compare it against. This is
    that same control, one step earlier.

    Synchronous, ~2 s. The parse happens in a separate locked-down process; the
    size and page caps that bound it are `jd_max_*` (7), and the request body is
    capped before it is buffered by `JdUploadSizeLimit` in `app.py`.
    """
    return to_jd_extraction(
        service.extract_jd_document(file.file.read(), filename=file.filename or "", actor=actor)
    )


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
