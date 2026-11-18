"""MCP client for the vendored Google Docs and Gmail server (mcp_server/), over stdio.

Each call starts the server as a subprocess, runs one tool, and stops it. Approvals are rare,
so a short-lived process is simpler and more robust than keeping one alive. The Google
credentials are passed from this backend's environment; the server's own token file is put in
a fresh temporary directory, so nothing from a previous run is reused.

The server reports Google errors as a normal result with `"success": false`, not as an MCP
error, so the reply is parsed and such a result raises McpToolError like any other failure.
"""

import asyncio
import json
import os
import shlex
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from app.config import Settings
from app.mcp.allowlist import check_tool


class McpToolError(Exception):
    """A tool call that did not complete.

    `outcome_unknown` is True when the call may still have reached Google (a timeout or a
    lost connection after sending). A doc append is safe to retry either way, because it
    carries a dedupe key; an email draft retried after an unknown outcome could exist twice.
    """

    def __init__(self, code: str, message: str, *, outcome_unknown: bool = False) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.outcome_unknown = outcome_unknown


class McpClient(Protocol):
    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]: ...

    async def list_tools(self) -> list[dict[str, Any]]: ...


@dataclass(frozen=True)
class ServerCommand:
    command: str
    args: list[str]
    cwd: Path


def server_command(settings: Settings) -> ServerCommand:
    if settings.mcp_server_command:
        parts = shlex.split(settings.mcp_server_command)
        return ServerCommand(parts[0], parts[1:], settings.mcp_server_dir)
    return ServerCommand("node", ["dist/index.js"], settings.mcp_server_dir)


def server_env(settings: Settings, token_dir: str) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", ""),
        "LOG_LEVEL": "warn",
        "GOOGLE_TOKEN_PATH": str(Path(token_dir) / "tokens.json"),
    }
    if settings.google_client_id:
        env["GOOGLE_CLIENT_ID"] = settings.google_client_id
    if settings.google_client_secret:
        env["GOOGLE_CLIENT_SECRET"] = settings.google_client_secret.get_secret_value()
    if settings.google_refresh_token:
        token = settings.google_refresh_token.get_secret_value()
        env["GOOGLE_TOKENS_JSON"] = json.dumps({"refresh_token": token})
    return env


def parse_result(result: Any) -> dict[str, Any]:
    text = "".join(getattr(c, "text", "") for c in (result.content or []))
    if getattr(result, "is_error", False):
        raise McpToolError("TOOL_ERROR", text[:500] or "the tool reported an error")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise McpToolError("BAD_RESPONSE", "the tool did not return JSON") from exc
    if not isinstance(data, dict):
        raise McpToolError("BAD_RESPONSE", "the tool did not return an object")
    if data.get("success") is not True:
        error = data.get("error") or {}
        raise McpToolError(
            str(error.get("code", "TOOL_FAILED")),
            str(error.get("message", "the tool reported a failure"))[:500],
        )
    return data


class StdioMcpClient:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def _session_call(self, tool: str | None, arguments: dict[str, Any]) -> Any:
        cmd = server_command(self._settings)
        with tempfile.TemporaryDirectory(prefix="mcp-tokens-") as token_dir:
            params = StdioServerParameters(
                command=cmd.command,
                args=cmd.args,
                cwd=cmd.cwd,
                env=server_env(self._settings, token_dir),
            )
            async with stdio_client(params) as (read, write), ClientSession(read, write) as s:
                await s.initialize()
                if tool is None:
                    return await s.list_tools()
                return await s.call_tool(tool, arguments)

    async def call(self, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        check_tool(tool)  # before anything reaches the server
        try:
            result = await asyncio.wait_for(
                self._session_call(tool, arguments), timeout=self._settings.mcp_timeout_seconds
            )
        except TimeoutError as exc:
            raise McpToolError(
                "TIMEOUT", "the tool did not answer in time", outcome_unknown=True
            ) from exc
        except McpToolError:
            raise
        except Exception as exc:  # noqa: BLE001 - server crash, broken pipe, bad startup
            raise McpToolError(
                "SERVER_ERROR", f"{type(exc).__name__}: {exc}"[:300], outcome_unknown=True
            ) from exc
        return parse_result(result)

    async def list_tools(self) -> list[dict[str, Any]]:
        result = await asyncio.wait_for(
            self._session_call(None, {}), timeout=self._settings.mcp_timeout_seconds
        )
        return [{"name": t.name, "input_schema": t.input_schema} for t in result.tools]
