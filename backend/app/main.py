import re
import uuid
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from app.api import health
from app.config import Settings, get_settings
from app.observability import configure_logging, get_logger

log = get_logger(__name__)

_REQUEST_ID = re.compile(r"[A-Za-z0-9-]{8,64}")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(title="The Support Desk", version="0.1.0")
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
    return app


app = create_app()
