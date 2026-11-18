"""Pending action payload -> MCP tool input, one mapping per action kind.

The approval is checked against the hash of the *payload* (what the approval screen shows),
never of the mapped input. These functions only format that payload for the tool; they add
nothing but layout and the action's dedupe marker.
"""

from typing import Any

from app.db.models import PendingAction
from app.mcp.allowlist import TOOL_FOR_KIND

SEPARATOR = "-" * 60


def dedupe_marker(action: PendingAction) -> str:
    """Written at the end of every doc entry. A retry finds it and doesn't append twice."""
    return f"[support-desk action {action.id}]"


def _lines(items: list[Any], prefix: str = "- ") -> list[str]:
    return [f"{prefix}{item}" for item in items]


def weekly_doc_text(payload: dict[str, Any]) -> list[str]:
    window = payload.get("window") or {}
    lines = [
        f"Weekly Product Pulse — {payload['date']}",
        f"Window: {window.get('start')} to {window.get('end')}",
        "",
        "Top themes:",
    ]
    for t in payload.get("top_themes", []):
        lines.append(
            f"- {t['label']} ({t['count']} reviews): {t.get('description') or ''}".rstrip(": ")
        )
    lines += [
        "",
        "Weekly pulse:",
        payload["weekly_pulse"],
        "",
        f"Identified fee issue: {payload['identified_fee_issue']}",
    ]
    if payload.get("explanation_bullets"):
        lines += ["Explanation:", *_lines(payload["explanation_bullets"])]
    if payload.get("source_links"):
        lines += ["Sources:", *_lines(payload["source_links"])]
    if payload.get("last_checked"):
        lines.append(f"Last checked: {payload['last_checked']}")
    return lines


def queue_doc_text(payload: dict[str, Any]) -> list[str]:
    citation = payload.get("citation") or {}
    routing = payload.get("routing") or {}
    lines = [
        f"Customer question — {payload['date']}",
        f"Question: {payload['question']}",
        "Reply:",
        payload["reply"],
    ]
    if citation:
        lines.append(
            f"Citation: {citation.get('title') or citation.get('source_key')} — "
            f"{citation.get('url')}"
        )
    if routing:
        lines.append(
            f"Routing: {routing.get('team')}, {routing.get('priority')} — {routing.get('reason')}"
        )
        if routing.get("disagreement"):
            lines.append(f"Disagreement: {routing['disagreement']}")
    return lines


def to_tool_input(action: PendingAction) -> tuple[str, dict[str, Any]]:
    """(tool name, arguments) for an action. Raises ValueError for internal actions."""
    if action.kind not in TOOL_FOR_KIND:
        raise ValueError(f"{action.kind} actions don't call a tool")
    tool = TOOL_FOR_KIND[action.kind]
    payload = action.payload
    if action.kind == "doc_append":
        body = (
            weekly_doc_text(payload) if action.origin == "weekly_run" else queue_doc_text(payload)
        )
        marker = dedupe_marker(action)
        content = "\n".join([*body, marker, SEPARATOR, ""])
        return tool, {
            "document_id": action.target,
            "content": content,
            "add_newline": True,
            "dedupe_key": marker,
        }
    args: dict[str, Any] = {
        "subject": payload["subject"],
        "body": payload["body"],
        "body_type": "plain",
    }
    if payload.get("to"):
        args["to"] = list(payload["to"]) if isinstance(payload["to"], list) else [payload["to"]]
    return tool, args


def result_id(kind: str, data: dict[str, Any]) -> str:
    """What the gate stores for an executed action."""
    if kind == "doc_append":
        rid = f"{data.get('document_id')}@{data.get('revision_id') or 'unknown-revision'}"
        return rid + (" (already present)" if data.get("skipped") else "")
    return str(data.get("draft_id"))
