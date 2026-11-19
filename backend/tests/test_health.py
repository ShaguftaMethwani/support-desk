from fastapi.testclient import TestClient

from app.api.health import CheckResult, get_health_checks
from app.config import Settings
from app.main import create_app


class StubChecks:
    def __init__(self, db: str, groq: str) -> None:
        self._db, self._groq = db, groq

    async def database(self) -> CheckResult:
        return CheckResult(status=self._db)  # type: ignore[arg-type]

    async def groq(self) -> CheckResult:
        return CheckResult(status=self._groq)  # type: ignore[arg-type]


def client_with(settings: Settings, db: str, groq: str) -> TestClient:
    app = create_app(settings)
    app.dependency_overrides[get_health_checks] = lambda: StubChecks(db, groq)
    return TestClient(app)


def test_all_ok(settings: Settings) -> None:
    r = client_with(settings, "ok", "ok").get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_groq_not_configured_is_still_ok(settings: Settings) -> None:
    r = client_with(settings, "ok", "not_configured").get("/health")
    assert r.status_code == 200 and r.json()["status"] == "ok"


def test_groq_down_is_degraded(settings: Settings) -> None:
    r = client_with(settings, "ok", "error").get("/health")
    assert r.status_code == 200 and r.json()["status"] == "degraded"


def test_database_down_returns_503(settings: Settings) -> None:
    r = client_with(settings, "error", "ok").get("/health")
    assert r.status_code == 503 and r.json()["status"] == "down"


def test_request_id_is_echoed_when_well_formed(settings: Settings) -> None:
    r = client_with(settings, "ok", "ok").get("/health", headers={"x-request-id": "abc-12345"})
    assert r.headers["x-request-id"] == "abc-12345"


def test_free_text_request_id_is_replaced(settings: Settings) -> None:
    bad = "my PAN is ABCDE1234F"
    r = client_with(settings, "ok", "ok").get("/health", headers={"x-request-id": bad})
    assert r.headers["x-request-id"] != bad
    assert len(r.headers["x-request-id"]) == 32


def test_live_check_needs_neither_database_nor_groq(settings: Settings) -> None:
    # Both checks would fail; the liveness endpoint never runs them.
    r = client_with(settings, "error", "error").get("/health/live")
    assert r.status_code == 200 and r.json() == {"status": "ok"}
