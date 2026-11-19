"""Who is calling: login with a username and password, then a signed session cookie.

POST /auth/login    username and password -> sets the session cookie
POST /auth/logout   clears it
GET  /auth/me       the signed-in user (401 without a valid session)

Every desk endpoint depends on `current_user`:
- A request with a session cookie is that user. An invalid or expired cookie is a 401.
  State-changing requests made with the cookie must also send `X-Requested-With: desk`, which
  a cross-site form cannot set and a cross-site script cannot send without passing CORS.
- A request without a cookie is refused, except in a *local* or *test* environment, where it
  acts as one local developer user so curl and the test suite work without logging in. The
  frontend always asks for a login: it uses /auth/me, which needs a real session.

Users are created with `uv run python -m scripts.create_user <username>`.
"""

import secrets
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import AppUser
from app.db.session import get_session
from app.security import make_session_token, read_session_token, verify_password

LOCAL_USERNAME = "local-dev"
LOCAL_ENVS = frozenset({"local", "test"})
COOKIE = "desk_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "desk"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

router = APIRouter(prefix="/auth", tags=["auth"])


@lru_cache
def _local_secret() -> bytes:
    return secrets.token_bytes(32)


def session_secret(settings: Settings) -> bytes:
    if settings.session_secret is not None:
        return settings.session_secret.get_secret_value().encode()
    if settings.app_env in LOCAL_ENVS:
        return _local_secret()
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "login not configured")


async def _cookie_user(
    request: Request, session: AsyncSession, settings: Settings
) -> AppUser | None:
    """The user of a valid session cookie; None when there is no cookie; 401 when it is bad."""
    token = request.cookies.get(COOKIE)
    if not token:
        return None
    max_age = settings.session_max_age_hours * 3600
    user_id = read_session_token(token, session_secret(settings), max_age)
    user = await session.get(AppUser, user_id) if user_id else None
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "session expired, sign in again")
    if request.method not in SAFE_METHODS and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "missing X-Requested-With header")
    return user


async def session_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AppUser | None:
    """The signed-in user, or None without a cookie. Never the local developer user."""
    return await _cookie_user(request, session, settings)


async def current_user(
    request: Request,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AppUser:
    user = await _cookie_user(request, session, settings)
    if user is not None:
        return user
    if settings.app_env not in LOCAL_ENVS:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
    user = await session.scalar(select(AppUser).where(AppUser.username == LOCAL_USERNAME))
    if user is None:
        # "!" is not a valid password hash, so this user can never log in with a password.
        user = AppUser(username=LOCAL_USERNAME, password_hash="!", role="agent")
        session.add(user)
        await session.commit()
    return user


def user_view(user: AppUser) -> dict[str, Any]:
    return {"id": str(user.id), "username": user.username, "role": user.role}


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@router.post("/login")
async def login(
    body: LoginRequest,
    response: Response,
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    secret = session_secret(settings)
    user = await session.scalar(select(AppUser).where(AppUser.username == body.username))
    # verify_password runs scrypt even for an unknown user, so timing does not reveal names.
    if not verify_password(body.password, user.password_hash if user else None) or user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "wrong username or password")
    response.set_cookie(
        COOKIE,
        make_session_token(user.id, secret),
        max_age=settings.session_max_age_hours * 3600,
        httponly=True,
        secure=settings.app_env not in LOCAL_ENVS,
        samesite=settings.session_cookie_samesite,  # type: ignore[arg-type]
        path="/",
    )
    return user_view(user)


@router.post("/logout")
async def logout(response: Response, settings: Annotated[Settings, Depends(get_settings)]) -> Any:
    response.delete_cookie(
        COOKIE,
        httponly=True,
        secure=settings.app_env not in LOCAL_ENVS,
        samesite=settings.session_cookie_samesite,  # type: ignore[arg-type]
        path="/",
    )
    return {"status": "signed_out"}


@router.get("/me")
async def me(user: Annotated[AppUser | None, Depends(session_user)]) -> dict[str, Any]:
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
    return user_view(user)
