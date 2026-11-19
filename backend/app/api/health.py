"""GET /health: database and Groq connectivity.

Returns 503 only when the database is down, since nothing works without it.
A Groq problem gives status "degraded": the app still serves stored data.

GET /health/live: the process is up. No database or Groq call, so the host's frequent health
checks (Render) cost nothing and never touch the Groq quota.
"""

import asyncio
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Response
from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy import text

from app.config import Settings, get_settings
from app.db.session import get_engine

CheckStatus = Literal["ok", "error", "not_configured"]


class CheckResult(BaseModel):
    status: CheckStatus
    detail: str | None = None


class HealthResponse(BaseModel):
    status: Literal["ok", "degraded", "down"]
    database: CheckResult
    groq: CheckResult


@dataclass
class HealthChecks:
    settings: Settings

    async def database(self) -> CheckResult:
        try:
            async with asyncio.timeout(5):
                async with get_engine().connect() as conn:
                    await conn.execute(text("SELECT 1"))
            return CheckResult(status="ok")
        except Exception as exc:  # noqa: BLE001 - health reports any failure
            return CheckResult(status="error", detail=type(exc).__name__)

    async def groq(self) -> CheckResult:
        if self.settings.groq_api_key is None:
            return CheckResult(status="not_configured", detail="GROQ_API_KEY is not set")
        client = AsyncOpenAI(
            api_key=self.settings.groq_api_key.get_secret_value(),
            base_url=self.settings.groq_base_url,
            timeout=5,
            max_retries=0,
        )
        try:
            # Listing models uses no tokens.
            await client.models.list()
            return CheckResult(status="ok")
        except Exception as exc:  # noqa: BLE001
            return CheckResult(status="error", detail=type(exc).__name__)
        finally:
            await client.close()


def get_health_checks() -> HealthChecks:
    return HealthChecks(get_settings())


router = APIRouter()


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health", response_model=HealthResponse)
async def health(
    response: Response, checks: Annotated[HealthChecks, Depends(get_health_checks)]
) -> HealthResponse:
    database, groq = await asyncio.gather(checks.database(), checks.groq())
    if database.status != "ok":
        overall: Literal["ok", "degraded", "down"] = "down"
        response.status_code = 503
    elif groq.status == "error":
        overall = "degraded"
    else:
        overall = "ok"
    return HealthResponse(status=overall, database=database, groq=groq)
