"""FastAPI application factory (spec §15).

The control plane. It creates positions, drafts and approves rubrics, snapshots
folders into jobs, and reports progress. **It never screens** — that is the
worker, in another process, so that a 78-minute batch cannot make this
unresponsive (§2).

**Handlers are `def`, not `async def`.** `sqlite3` is synchronous, so FastAPI
runs them in its threadpool, which is correct and is why `connection.py` creates
one connection per thread. Declaring `async def` and then making blocking calls
inside would block the event loop and produce something slower than the sync
version while looking more sophisticated (§15.3).

**Migrations are a startup gate.** The app refuses to construct against a stale
schema rather than serving requests that write rows half-matching the schema the
code expects (§12.1).
"""

from collections.abc import Awaitable, Callable

from fastapi import FastAPI, Request, Response, status
from fastapi.responses import JSONResponse

from config.settings import settings
from screener.api.routes import candidates, health, positions, rubrics, runs
from screener.service import NotFoundError, RubricNotApprovedError, ServiceError
from screener.storage.connection import require_current_schema


def _install_error_handlers(app: FastAPI) -> None:
    """Service rules become status codes in one place.

    Without this, every route grows a `try/except` and the four-line handler
    §15.4 asks for becomes fifteen. The mapping is deliberate: a rule violation
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

    @app.exception_handler(ServiceError)
    def _bad_request(request: Request, exc: ServiceError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_400_BAD_REQUEST, content={"detail": str(exc)})


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

    for module in (positions, rubrics, runs, candidates, health):
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

    return app
