"""FastAPI application factory (spec 15).

The control plane. It creates positions, drafts and approves rubrics, snapshots
folders into jobs, and reports progress. **It never screens** — that is the
worker, in another process, so that a 78-minute batch cannot make this
unresponsive (2).

**Handlers are `def`, not `async def`.** `sqlite3` is synchronous, so FastAPI
runs them in its threadpool, which is correct and is why `connection.py` creates
one connection per thread. Declaring `async def` and then making blocking calls
inside would block the event loop and produce something slower than the sync
version while looking more sophisticated (15.3).

**Migrations are a startup gate.** The app refuses to construct against a stale
schema rather than serving requests that write rows half-matching the schema the
code expects (12.1).
"""

from collections.abc import Awaitable, Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import FileResponse, JSONResponse

from config.settings import settings
from screener.api.routes import (
    audit,
    candidates,
    dashboard,
    health,
    positions,
    rubrics,
    runs,
)
from screener.service import (
    ConflictError,
    NotFoundError,
    RubricNotApprovedError,
    ServiceError,
)
from screener.storage.connection import require_current_schema


def _install_error_handlers(app: FastAPI) -> None:
    """Service rules become status codes in one place.

    Without this, every route grows a `try/except` and the four-line handler
    15.4 asks for becomes fifteen. The mapping is deliberate: a rule violation
    is the caller's problem (4xx), never a 500 that pages somebody.
    """

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

    for module in (positions, rubrics, runs, candidates, health, audit, dashboard):
        app.include_router(module.router)

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
