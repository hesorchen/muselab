#!/usr/bin/env python3
"""Stdio MCP server exposing MuseLab long-term memory to external Agent CLIs.

MuseLab chat sessions already get these tools in-process. This bridge is for
other clients (Claude Code, Ducc, Codex, ...) that should share the same
memory. It proxies every call to the running MuseLab backend, so ownership,
auditing and indexing stay in one place.

Environment:
  MUSELAB_URL            Backend base URL (default http://127.0.0.1:8765)
  MUSELAB_TOKEN          MuseLab access token (sent as X-Auth-Token)
  MUSELAB_MEMORY_CLIENT  Client label recorded on writes (default "external")
  MUSELAB_SESSION_ID     Optional MuseLab session id to attribute writes to

Example (Claude Code):
  claude mcp add muselab-memory -e MUSELAB_TOKEN=... -- \\
      python3 /path/to/muselab/scripts/muselab_memory_mcp.py
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import mcp.types as types  # noqa: E402
from mcp.server.lowlevel import Server  # noqa: E402
from mcp.server.stdio import stdio_server  # noqa: E402

from backend.memory_tool_specs import SERVER_NAME, TOOL_SPECS  # noqa: E402

_TIMEOUT_S = 30.0


def _base_url() -> str:
    return os.environ.get("MUSELAB_URL", "http://127.0.0.1:8765").rstrip("/")


def _post(tool: str, args: dict) -> tuple[str, bool]:
    body = json.dumps({
        "args": args,
        "session_id": os.environ.get("MUSELAB_SESSION_ID", ""),
        "client": os.environ.get("MUSELAB_MEMORY_CLIENT", "external")[:64],
    }).encode("utf-8")
    request = urllib.request.Request(
        f"{_base_url()}/api/memory/agent/tools/{tool}", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "X-Auth-Token": os.environ.get("MUSELAB_TOKEN", "")})
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as response:
            payload = json.loads(response.read())
        return str(payload.get("text", "")), bool(payload.get("is_error"))
    except urllib.error.HTTPError as exc:
        hint = " (check MUSELAB_TOKEN)" if exc.code in (401, 403) else ""
        return json.dumps({"error": f"MuseLab returned HTTP {exc.code}{hint}"}), True
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return json.dumps({"error": f"MuseLab unreachable at {_base_url()}: "
                                    f"{type(exc).__name__}"}), True


def build_server() -> Server:
    server = Server(SERVER_NAME)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [types.Tool(name=name, description=spec["description"],
                           inputSchema=spec["schema"])
                for name, spec in TOOL_SPECS.items()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict) -> types.CallToolResult:
        if name not in TOOL_SPECS:
            text, is_error = json.dumps({"error": f"unknown tool {name}"}), True
        else:
            text, is_error = await asyncio.to_thread(_post, name, arguments or {})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=text)], isError=is_error)

    return server


async def main() -> None:
    server = build_server()
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream,
                         server.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
