"""The only MCP tools the backend may call (architecture section 10.2).

The vendored server has no send tool, but this list is the backend's own guarantee: the
client refuses any other tool name before it starts the server or sends anything to it.
"""

TOOL_FOR_KIND: dict[str, str] = {
    "doc_append": "google_docs_append",
    "email_draft": "gmail_create_draft",
}
ALLOWED_TOOLS = frozenset(TOOL_FOR_KIND.values())


class ToolNotAllowedError(Exception):
    def __init__(self, tool: str) -> None:
        super().__init__(f"tool {tool!r} is not on the allow-list")
        self.tool = tool


def check_tool(tool: str) -> None:
    if tool not in ALLOWED_TOOLS:
        raise ToolNotAllowedError(tool)
