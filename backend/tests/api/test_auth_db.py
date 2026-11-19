"""Login, the session cookie and the frontend-only endpoints, against real Postgres.
Skipped when no database is reachable (REQUIRE_DB=1 makes that a failure)."""

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.api.auth import COOKIE, LoginLimiter, get_login_limiter
from app.config import Settings, get_settings
from app.corpus.embed import HashEmbedder
from app.db.models import AppUser
from app.db.session import get_session
from app.llm import LLMClient
from app.main import create_app
from app.orchestrator.deps import get_desk_deps
from app.orchestrator.desk import DeskDeps
from app.security import hash_password

pytestmark = pytest.mark.db

PASSWORD = "a long test password"
DESK = {"X-Requested-With": "desk"}


async def _run(statements: Any) -> None:
    engine = create_async_engine(Settings().database_url, poolclass=NullPool)
    try:
        async with async_sessionmaker(engine, expire_on_commit=False)() as s:
            await statements(s)
            await s.commit()
    finally:
        await engine.dispose()


@pytest.fixture
def username() -> Iterator[str]:
    name = f"test-{uuid.uuid4().hex[:12]}"

    async def create(s: AsyncSession) -> None:
        await s.execute(text("SELECT 1"))
        s.add(AppUser(username=name, password_hash=hash_password(PASSWORD), role="agent"))

    try:
        asyncio.run(_run(create))
    except Exception as exc:  # noqa: BLE001
        if os.environ.get("REQUIRE_DB") == "1":
            pytest.fail(f"Database required but unreachable: {type(exc).__name__}")
        pytest.skip("No database reachable")
    yield name

    async def remove(s: AsyncSession) -> None:
        await s.execute(delete(AppUser).where(AppUser.username == name))

    asyncio.run(_run(remove))


def make_client(**overrides: Any) -> TestClient:
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        weekly_resume_on_startup=False,
        weekly_trigger_token="secret-token",
        **overrides,
    )

    async def session_override() -> AsyncIterator[AsyncSession]:
        engine = create_async_engine(Settings().database_url, poolclass=NullPool)
        try:
            async with async_sessionmaker(engine, expire_on_commit=False)() as s:
                yield s
        finally:
            await engine.dispose()

    app = create_app(settings)
    limiter = LoginLimiter()  # each client starts with no failed sign-ins
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_session] = session_override
    app.dependency_overrides[get_login_limiter] = lambda: limiter
    # The desk's own dependencies, never the production ones: those read GROQ_API_KEY from the
    # environment (absent in CI) and load the embedding model. No test here calls a model.
    deps = DeskDeps(
        sessions=async_sessionmaker(
            create_async_engine(Settings().database_url, poolclass=NullPool),
            expire_on_commit=False,
        ),
        llm=LLMClient(settings.model_copy(update={"groq_api_key": SecretStr("unused")})),
        embedder=HashEmbedder(),
        retriever_for=lambda _session: None,  # type: ignore[arg-type,return-value]
        settings=settings,
    )
    app.dependency_overrides[get_desk_deps] = lambda: deps
    # https: the cookie is Secure outside local, and httpx only sends it over https.
    return TestClient(app, base_url="https://testserver")


def test_login_me_logout(username: str) -> None:
    client = make_client()
    assert client.get("/auth/me").status_code == 401  # no local stand-in for /auth/me
    wrong = client.post("/auth/login", json={"username": username, "password": "nope"})
    assert wrong.status_code == 401
    unknown = client.post("/auth/login", json={"username": "no-such-user", "password": PASSWORD})
    assert unknown.status_code == 401 and unknown.json() == wrong.json()

    ok = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert ok.status_code == 200 and ok.json()["username"] == username
    set_cookie = ok.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie
    assert client.get("/auth/me").json()["username"] == username

    client.post("/auth/logout")
    assert client.get("/auth/me").status_code == 401


def test_the_local_developer_user_cannot_log_in() -> None:
    client = make_client()
    response = client.post("/auth/login", json={"username": "local-dev", "password": "!"})
    assert response.status_code == 401


def test_cookie_requests_need_the_header_to_change_state(username: str) -> None:
    client = make_client()
    client.post("/auth/login", json={"username": username, "password": PASSWORD})
    missing = uuid.uuid4()
    assert client.post(f"/queue/{missing}/reject").status_code == 403
    assert client.post(f"/queue/{missing}/reject", headers=DESK).status_code == 404
    assert client.get("/queue?limit=1").status_code == 200  # reads need no header


def test_a_bad_cookie_is_refused_even_locally() -> None:
    client = make_client()
    forged = {"Cookie": f"{COOKIE}={uuid.uuid4()}.1.deadbeef"}
    assert client.get("/queue?limit=1", headers=forged).status_code == 401


def test_outside_local_every_view_needs_a_login(username: str) -> None:
    client = make_client(app_env="production", session_secret="x" * 32)
    assert client.get("/queue?limit=1").status_code == 401
    assert client.get("/sources").status_code == 401
    ok = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert "secure" in ok.headers["set-cookie"].lower()
    assert client.get("/queue?limit=1").status_code == 200


def test_login_needs_a_secret_outside_local(username: str) -> None:
    client = make_client(app_env="production")
    response = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert response.status_code == 503


def test_weekly_runs_take_the_token_or_a_signed_in_user(username: str) -> None:
    client = make_client()
    # The local stand-in user is not enough for the weekly endpoints.
    assert client.get("/weekly/runs").status_code == 401
    token = client.get("/weekly/runs", headers={"X-Trigger-Token": "secret-token"})
    assert token.status_code == 200 and isinstance(token.json()["runs"], list)

    client.post("/auth/login", json={"username": username, "password": PASSWORD})
    runs = client.get("/weekly/runs?limit=2")
    assert runs.status_code == 200 and len(runs.json()["runs"]) <= 2
    # Starting a run with the cookie needs the header like any other state change.
    assert client.post("/weekly/run").status_code == 403
    if runs.json()["runs"]:
        run_id = runs.json()["runs"][0]["run_id"]
        assert client.get(f"/weekly/{run_id}").json()["run_id"] == run_id


def test_sources_and_reviews() -> None:
    client = make_client()
    sources = client.get("/sources").json()["sources"]
    for s in sources:
        assert {"key", "url", "publisher", "last_checked", "citing", "citing_count"} <= set(s)
        assert len(s["citing"]) <= s["citing_count"]
        for c in s["citing"]:
            assert {"item_id", "version", "question"} <= set(c)
    body = client.get("/reviews", params={"ids": ["no-such-review"]}).json()
    assert body == {"reviews": [], "missing": ["no-such-review"]}
    assert client.get("/reviews").status_code == 422


def test_repeated_failed_sign_ins_are_refused_for_a_while(username: str) -> None:
    client = make_client(login_max_failures=3)
    for _ in range(3):
        bad = client.post("/auth/login", json={"username": username, "password": "nope"})
        assert bad.status_code == 401
    locked = client.post("/auth/login", json={"username": username, "password": PASSWORD})
    assert locked.status_code == 429 and "try again" in locked.json()["detail"]
    assert int(locked.headers["retry-after"]) > 0
    # Other usernames are not affected.
    other = client.post("/auth/login", json={"username": "no-such-user", "password": "x"})
    assert other.status_code == 401


def test_api_docs_are_only_served_locally() -> None:
    assert make_client().get("/docs").status_code == 200
    deployed = make_client(app_env="production", session_secret="x" * 32)
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert deployed.get(path).status_code == 404
