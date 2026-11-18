"""Who is calling. Login (signed session cookie, hashed passwords) arrives in Phase 10.

Until then every request in a *local* environment acts as one local developer user, so chat
sessions and approvals have an owner. In any other environment these endpoints refuse with
401: nothing can be deployed with the desk open to the internet before login exists.
"""

from typing import Annotated

from fastapi import Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db.models import AppUser
from app.db.session import get_session

LOCAL_USERNAME = "local-dev"
LOCAL_ENVS = frozenset({"local", "test"})


async def current_user(
    session: Annotated[AsyncSession, Depends(get_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> AppUser:
    if settings.app_env not in LOCAL_ENVS:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "login required")
    user = await session.scalar(select(AppUser).where(AppUser.username == LOCAL_USERNAME))
    if user is None:
        # "!" is not a valid password hash, so this user can never log in with a password.
        user = AppUser(username=LOCAL_USERNAME, password_hash="!", role="agent")
        session.add(user)
        await session.commit()
    return user
