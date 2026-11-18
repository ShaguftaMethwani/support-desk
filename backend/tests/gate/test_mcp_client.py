"""MCP client, allow-list and payload mapping. The contract test runs the real vendored server
(skipped when Node or its build is missing)."""

import json
import shutil
import sys
import uuid
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.db.models import PendingAction
from app.gate.actions import canonical_json, payload_hash
from app.mcp.allowlist import ALLOWED_TOOLS, ToolNotAllowedError, check_tool
from app.mcp.client import McpToolError, StdioMcpClient, parse_result, server_env
from app.mcp.mapping import dedupe_marker, result_id, to_tool_input


def settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[arg-type]


def action(
    kind: str, origin: str, payload: dict[str, object], target: str = "doc-123"
) -> PendingAction:
    stored = json.loads(canonical_json(payload))
    return PendingAction(
        id=uuid.uuid4(),
        kind=kind,
        origin=origin,
        origin_id=uuid.uuid4(),
        target=target,
        payload=stored,
        payload_hash=payload_hash(stored),
        status="approved",
    )


WEEKLY_DOC = {
    "date": "2026-11-18",
    "window": {"start": "2026-08-27", "end": "2026-11-18"},
    "top_themes": [{"label": "App crashes", "description": "Users report crashes.", "count": 97}],
    "weekly_pulse": "Weekly Product Pulse: ...",
    "identified_fee_issue": "Expense ratio and Direct vs Regular plans",
    "explanation_bullets": ["Expense ratio is the annual cost."],
    "source_links": ["https://www.sebi.gov.in/faq.pdf"],
    "last_checked": "2026-11-17",
}


# --- allow-list -----------------------------------------------------------------------------


def test_allow_list_names_only_the_two_tools() -> None:
    assert ALLOWED_TOOLS == {"google_docs_append", "gmail_create_draft"}
    for name in ("gmail_send_email", "send_email", "gmail_delete_draft", ""):
        with pytest.raises(ToolNotAllowedError):
            check_tool(name)


async def test_client_refuses_other_tools_before_starting_the_server() -> None:
    # If the client tried to start a server, this command would fail with a different error.
    client = StdioMcpClient(settings(mcp_server_command="/nonexistent/server"))
    with pytest.raises(ToolNotAllowedError):
        await client.call(
            "gmail_send_email", {"to": ["a@example.com"], "subject": "s", "body": "b"}
        )


# --- results ------------------------------------------------------------------------------


def _result(text: str, is_error: bool = False) -> SimpleNamespace:
    return SimpleNamespace(content=[SimpleNamespace(text=text)], is_error=is_error)


def test_parse_result() -> None:
    ok = parse_result(_result(json.dumps({"success": True, "draft_id": "d1"})))
    assert ok["draft_id"] == "d1"
    failed = _result(
        json.dumps(
            {"success": False, "error": {"code": "TOKEN_EXPIRED", "message": "re-authorize"}}
        )
    )
    with pytest.raises(McpToolError, match="TOKEN_EXPIRED"):
        parse_result(failed)
    with pytest.raises(McpToolError, match="TOOL_ERROR"):
        parse_result(_result("Tool gmail_send_email not found", is_error=True))
    with pytest.raises(McpToolError, match="BAD_RESPONSE"):
        parse_result(_result("not json"))


async def test_a_server_that_never_answers_times_out_with_outcome_unknown() -> None:
    hang = f"{sys.executable} -c 'import time; time.sleep(30)'"
    client = StdioMcpClient(settings(mcp_server_command=hang, mcp_timeout_seconds=1.0))
    with pytest.raises(McpToolError) as info:
        await client.call("gmail_create_draft", {"subject": "s", "body": "b"})
    assert info.value.code == "TIMEOUT" and info.value.outcome_unknown


def test_credentials_go_to_the_server_environment_only() -> None:
    env = server_env(
        settings(
            google_client_id="cid", google_client_secret="secret", google_refresh_token="1//refresh"
        ),
        "/tmp/tok",
    )
    assert env["GOOGLE_CLIENT_ID"] == "cid" and env["GOOGLE_CLIENT_SECRET"] == "secret"
    assert json.loads(env["GOOGLE_TOKENS_JSON"]) == {"refresh_token": "1//refresh"}
    assert env["GOOGLE_TOKEN_PATH"] == "/tmp/tok/tokens.json"


# --- mapping --------------------------------------------------------------------------------


def test_weekly_doc_append_mapping_carries_the_dedupe_marker() -> None:
    a = action("doc_append", "weekly_run", WEEKLY_DOC)
    tool, args = to_tool_input(a)
    assert tool == "google_docs_append" and args["document_id"] == "doc-123"
    assert args["dedupe_key"] == dedupe_marker(a) == f"[support-desk action {a.id}]"
    assert args["dedupe_key"] in args["content"]
    for text in (
        "Weekly Product Pulse — 2026-11-18",
        "App crashes (97 reviews)",
        "Identified fee issue: Expense ratio",
        "https://www.sebi.gov.in/faq.pdf",
        "Last checked: 2026-11-17",
    ):
        assert text in args["content"]


def test_queue_doc_append_mapping() -> None:
    payload = {
        "date": "2026-11-18",
        "question": "What is the exit load?",
        "reply": "It is 1%.",
        "citation": {"title": "KIM", "url": "https://files.hdfcfund.com/kim.pdf"},
        "routing": {"team": "support_l1", "priority": "P3", "reason": "Answered"},
        "draft_version": 1,
    }
    _, args = to_tool_input(action("doc_append", "queue_item", payload))
    assert "Question: What is the exit load?" in args["content"]
    assert "Citation: KIM — https://files.hdfcfund.com/kim.pdf" in args["content"]
    assert "Routing: support_l1, P3 — Answered" in args["content"]


def test_email_draft_mapping_has_no_recipient() -> None:
    a = action(
        "email_draft",
        "weekly_run",
        {"subject": "Weekly", "body": "Body", "to": None},
        target="gmail_drafts",
    )
    tool, args = to_tool_input(a)
    assert tool == "gmail_create_draft"
    assert args == {"subject": "Weekly", "body": "Body", "body_type": "plain"}


def test_internal_actions_never_map_to_a_tool() -> None:
    with pytest.raises(ValueError):
        to_tool_input(action("promote_canonical", "weekly_run", {"fee_type": "x"}, "internal"))


def test_result_ids() -> None:
    assert result_id("doc_append", {"document_id": "d", "revision_id": "r9"}) == "d@r9"
    skipped = result_id("doc_append", {"document_id": "d", "revision_id": "r9", "skipped": True})
    assert skipped == "d@r9 (already present)"
    assert result_id("email_draft", {"draft_id": "r-123"}) == "r-123"


# --- contract test against the real server --------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
async def test_contract_with_the_vendored_server() -> None:
    s = settings()
    if not (s.mcp_server_dir / "dist" / "index.js").exists():
        pytest.skip("mcp_server is not built (cd mcp_server && npm ci && npm run build)")
    tools = {t["name"]: t["input_schema"] for t in await StdioMcpClient(s).list_tools()}
    assert set(tools) == {"google_docs_append", "gmail_create_draft"}  # no send tool
    append = tools["google_docs_append"]
    assert set(append["required"]) == {"document_id", "content"}
    assert {"content", "document_id", "add_newline", "dedupe_key"} <= set(append["properties"])
    draft = tools["gmail_create_draft"]
    assert set(draft["required"]) == {"subject", "body"}  # "to" is optional
    assert {"to", "subject", "body", "body_type"} <= set(draft["properties"])


async def test_doc_actions_created_without_a_doc_id_fail_without_calling_the_tool() -> None:
    from unittest.mock import AsyncMock

    from app.mcp.execute import UNSET_DOC_TARGET, execute

    a = action("doc_append", "weekly_run", WEEKLY_DOC, target=UNSET_DOC_TARGET)
    approval = SimpleNamespace(action_id=a.id, payload_hash=a.payload_hash)
    session = SimpleNamespace(get=AsyncMock(side_effect=[a, approval]))
    client = SimpleNamespace(call=AsyncMock())
    with pytest.raises(McpToolError, match="NO_TARGET"):
        await execute(session, a.id, uuid.uuid4(), client)  # type: ignore[arg-type]
    client.call.assert_not_called()
