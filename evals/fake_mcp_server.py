"""A stand-in for the Google Docs and Gmail MCP server, used only by the evals.

It offers the same two tools the backend may call, answers like the real server, and appends
each call to a JSONL file (the first argument) instead of writing to Google. The backend starts
it over stdio exactly as it starts the real one (MCP_SERVER_COMMAND).
"""

import json
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer

LOG = Path(sys.argv[1])
server = MCPServer("fake-google")


def record(tool: str, arguments: dict[str, object]) -> None:
    with LOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": datetime.now(UTC).isoformat(), "tool": tool,
                            "arguments": arguments}) + "\n")  # fmt: skip


@server.tool()
def google_docs_append(
    document_id: str, content: str, add_newline: bool = True, dedupe_key: str | None = None
) -> str:
    record("google_docs_append", {"document_id": document_id, "content": content,
                                  "add_newline": add_newline, "dedupe_key": dedupe_key})  # fmt: skip
    return json.dumps({"success": True, "document_id": document_id,
                       "revision_id": f"rev-{uuid.uuid4().hex[:8]}"})  # fmt: skip


@server.tool()
def gmail_create_draft(
    subject: str, body: str, body_type: str = "plain", to: list[str] | None = None
) -> str:
    record("gmail_create_draft", {"subject": subject, "body": body, "body_type": body_type,
                                  "to": to})  # fmt: skip
    return json.dumps({"success": True, "draft_id": f"draft-{uuid.uuid4().hex[:8]}"})


if __name__ == "__main__":
    server.run()
