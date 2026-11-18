import asyncio
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api import actions, explainers, health, queue, weekly
from app.config import Settings, get_settings
from app.guard.log_filter import install_log_redaction
from app.observability import configure_logging, get_logger

log = get_logger(__name__)

_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{8,64}")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)
    install_log_redaction()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resume: asyncio.Task[None] | None = None
        if settings.weekly_resume_on_startup:
            resume = asyncio.create_task(_resume_weekly_runs())
        yield
        if resume is not None and not resume.done():
            resume.cancel()

    app = FastAPI(title="The Support Desk", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def request_id(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming = request.headers.get("x-request-id", "")
        # Accept a caller's id only if it looks like an id, so logs never hold free text.
        rid = incoming if _REQUEST_ID.fullmatch(incoming) else uuid.uuid4().hex
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=rid)
        response = await call_next(request)
        response.headers["x-request-id"] = rid
        # Path and status only. Request bodies are never logged.
        log.info(
            "request", method=request.method, path=request.url.path, status=response.status_code
        )
        return response

    app.include_router(health.router)
    app.include_router(weekly.router)
    app.include_router(queue.router)
    app.include_router(actions.router)
    app.include_router(explainers.router)
    return app


async def _resume_weekly_runs() -> None:
    """Resume weekly runs a restart interrupted. Never stops the API from starting."""
    from app.weekly.runner import get_weekly_runner

    try:
        resumed = await get_weekly_runner().resume_interrupted()
        if resumed:
            log.info("weekly_runs_resumed", count=len(resumed))
    except Exception:  # noqa: BLE001 - e.g. database not reachable yet
        log.exception("weekly_resume_failed")


app = create_app()
