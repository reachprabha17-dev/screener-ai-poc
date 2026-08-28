"""FastAPI application factory (spec 15).

The control plane. It creates positions, drafts and approves rubrics, snapshots
folders into jobs, and reports progress. **It never screens** — that is the
worker, in another process, so that a 78-minute batch cannot make this
unresponsive (2).

**Handlers are `def`, not `async def`.** The database driver is synchronous, so
FastAPI runs them in its threadpool — which is correct, and is what the
connection pool exists to serve. Declaring `async def` and then making blocking
calls inside would block the event loop and produce something slower than the
sync version while looking more sophisticated (15.3).

**Migrations are a startup gate.** The app refuses to construct against a stale
schema rather than serving requests that write rows half-matching the schema the
code expects (12.1).
"""

import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from config.settings import settings
from screener.api.routes import (
    audit,
    candidates,
    config,
    dashboard,
    health,
    positions,
    rubrics,
    runs,
)
from screener.service import (
    BusyError,
    ConflictError,
    NotFoundError,
    RubricNotApprovedError,
    ServiceError,
    require_current_schema,
)

# Paths whose request body is a file upload, and the header value that decides
# whether we will read it. Everything else in this API is small JSON.
_UPLOAD_PATHS = frozenset({"/jd-documents"})

# Multipart framing — the boundary lines, the Content-Disposition header, the
# trailer — around a file of exactly `jd_max_file_bytes`. Generous, because
# rejecting a legitimate 2 MB document for the sake of its envelope would be a
# bug nobody could diagnose from the error.
_MULTIPART_OVERHEAD_BYTES = 8192


def _install_error_handlers(app: FastAPI) -> None:
    """Service rules become status codes in one place.

    Without this, every route grows a `try/except` and the four-line handler
    15.4 asks for becomes fifteen. The mapping is deliberate: a rule violation
    is the caller's problem (4xx), never a 500 that pages somebody.
    """

    @app.exception_handler(BusyError)
    def _busy(request: Request, exc: BusyError) -> JSONResponse:
        # 503 with Retry-After, not 400 or 500: the request was fine and
        # repeating it verbatim is the correct response. A 400 would tell
        # somebody their document is wrong when it is not.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": str(exc)},
            headers={"Retry-After": "5"},
        )

    @app.exception_handler(NotFoundError)
    def _not_found(request: Request, exc: NotFoundError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    @app.exception_handler(RubricNotApprovedError)
    def _not_approved(request: Request, exc: RubricNotApprovedError) -> JSONResponse:
        # 409, not 400: the request is well-formed, the *state* forbids it.
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": f"rubric {exc} has not been approved"},
        )

    @app.exception_handler(ConflictError)
    def _conflict(request: Request, exc: ConflictError) -> JSONResponse:
        # 409 for the same reason as above: the body is fine, the world moved.
        # A 400 here would read as "you sent something wrong" to someone whose
        # only mistake was having the page open while a colleague saved.
        return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"detail": str(exc)})

    @app.exception_handler(ServiceError)
    def _bad_request(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


def _mount_reviewer_ui(app: FastAPI) -> None:
    """Serve the built React bundle at `/ui/`, when there is one.

    **This is the whole of the UI's presence in this process.** Decision #11 is
    unchanged and arguably stronger than it was under Streamlit: what is served
    here is static files, and the reviewer interface is a browser tab that can
    reach the data only through the same HTTP endpoints anything else uses. There
    is no UI process holding a database handle, because there is no UI process.

    **Mounted under `/ui/`, not `/`.** The SPA does client-side routing, so a
    deep link like `/ui/audit/search` has to return `index.html` — and `/audit`
    is an API route. A catch-all at the root would make which one answers depend
    on registration order, which is the kind of thing that works until somebody
    adds an endpoint whose path a reviewer had bookmarked.

    **Absent in a source checkout that has not built the UI**, and that is not an
    error: `npm run dev` serves the bundle itself and proxies the API here.
    """
    dist = Path(settings.web_dist_dir).resolve()
    index = dist / "index.html"
    if not index.is_file():
        return

    @app.get("/ui", include_in_schema=False)
    @app.get("/ui/{asset_path:path}", include_in_schema=False)
    def reviewer_ui(asset_path: str = "") -> FileResponse:
        """A built asset if the path names one, `index.html` otherwise.

        `StaticFiles(html=True)` is not enough on its own: it serves `index.html`
        for a *directory*, and answers 404 for `/ui/runs/run-1/review`, which is
        a route in the browser rather than a file on disk. The symptom is
        specific and easy to miss — the app works until somebody reloads the page
        or shares a link.

        `asset_path` comes from the URL, so the resolved path is checked against
        the bundle directory before anything is opened. It should be impossible
        to escape a directory whose contents this process built, and that is
        precisely the assumption worth not making.
        """
        candidate = (dist / asset_path).resolve()
        if asset_path and candidate.is_file() and candidate.is_relative_to(dist):
            # `FileResponse` picks the media type from the suffix, which is all
            # a bundle of `.js`, `.css` and `.map` files needs.
            return FileResponse(candidate)
        return FileResponse(index, media_type="text/html")


class UploadSizeLimit:
    """Reject an oversized upload **before** its body is read (7, 8.2).

    This has to be ASGI middleware rather than a check in the handler, and the
    reason is not style. Starlette parses the multipart body during FastAPI's
    dependency resolution, before the sync handler is dispatched at all — and
    each file part goes into a `SpooledTemporaryFile` that rolls to disk past
    1 MB with **no size ceiling of its own** (`max_part_size` is enforced only
    for non-file parts). By the time anything in `routes/` or `service.py` runs,
    an unbounded upload is already on the filesystem. So the only place early
    enough is here, ahead of the app.

    **Two checks, and the second is not redundant.** `Content-Length` covers
    every well-behaved client. It is absent under `Transfer-Encoding: chunked`,
    which uvicorn accepts — so a counting wrapper around `receive` covers the
    client that simply omits the header, which is the one actually trying.

    Scoped to the upload paths rather than applied globally: a body cap sized
    for a job description, silently applied to every future endpoint, is a limit
    nobody would remember was here.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") not in _UPLOAD_PATHS:
            await self.app(scope, receive, send)
            return

        limit = settings.jd_max_file_bytes + _MULTIPART_OVERHEAD_BYTES
        declared = _content_length(scope)
        if declared is not None and declared > limit:
            await _too_large(send, limit)
            return

        seen = 0

        async def counting_receive() -> Message:
            nonlocal seen
            message = await receive()
            if message["type"] == "http.request":
                seen += len(message.get("body", b""))
                if seen > limit:
                    # Truncate the stream rather than raise: the exception would
                    # surface as a 500 from inside Starlette's form parser, which
                    # is not what happened.
                    raise _UploadTooLarge
            return message

        try:
            await self.app(scope, counting_receive, send)
        except _UploadTooLarge:
            await _too_large(send, limit)


class _UploadTooLarge(Exception):
    """Raised out of the receive wrapper. Never reaches a handler."""


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _too_large(send: Send, limit: int) -> None:
    body = json.dumps(
        {"detail": f"That file is larger than the {limit // (1024 * 1024)} MB limit."}
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status.HTTP_413_CONTENT_TOO_LARGE,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def create_app(*, check_migrations: bool = True) -> FastAPI:
    """Build the app.

    **A factory, with no module-level `app`.** Constructing one at import time
    would run the migration gate on import, which makes this module unimportable
    anywhere the database is not already migrated — including every test that
    only wants to inspect the routes. Import-time side effects also mean the gate
    fires before an operator has had a chance to read the error.

    Deployed with `uvicorn screener.api.app:create_app --factory`, so the gate
    still runs exactly once, at startup, where it belongs.

    `check_migrations` is for tests that manage their own database. It is never
    disabled in a deployment.
    """
    if check_migrations:
        require_current_schema()

    app = FastAPI(
        title="Screener",
        version=settings.app_version,
        description=(
            "On-prem resume screening control plane. Enqueues work; the worker "
            "executes it. No candidate data leaves this host."
        ),
    )

    _install_error_handlers(app)

    for module in (positions, rubrics, runs, candidates, health, audit, dashboard, config):
        app.include_router(module.router)

    app.add_middleware(UploadSizeLimit)

    @app.middleware("http")
    async def _no_store(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Responses carry candidate names and verdicts. Nothing caches them.

        Cheap, and it removes a class of accident: a proxy or browser holding a
        ranked list of applicants is a copy of candidate data outside `data/`,
        which is where the erasure path can reach.
        """
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    # After the routers and after the middleware, so the API paths are matched
    # first and the bundle is covered by `no-store` like everything else.
    _mount_reviewer_ui(app)

    return app
