#!/usr/bin/env python3
"""Dump the bundled CLI's live built-in tool catalog, one name per line.

The CLI announces its full tool list in the `init` SystemMessage of every
session — that IS the programmatic catalog (the sdk-bump-checklist used to
claim none exists). This probe spins up a throwaway 1-turn session in /tmp,
captures that list, filters MCP tools (muselab-injected, not CLI-bundled),
and prints it sorted — ready to diff against docs/tool-catalog.txt:

    .venv/bin/python scripts/dump-tool-catalog.py | diff docs/tool-catalog.txt -

Run on every SDK bump (checklist item 1). A new line on the right that is
harness-only (plan-mode / cron / worktree / notification primitives with no
muselab UI) belongs in `disallowed_tools` in backend/chat.py; a useful tool
(new editor/search verb) should be left exposed and noted in the bump PR.
"""
import sys

import anyio
from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient, SystemMessage


async def main() -> None:
    opts = ClaudeAgentOptions(cwd="/tmp", max_turns=1)
    async with ClaudeSDKClient(options=opts) as client:
        await client.query("hi")
        async for msg in client.receive_response():
            if (isinstance(msg, SystemMessage)
                    and getattr(msg, "subtype", "") == "init"):
                tools = (getattr(msg, "data", None) or {}).get("tools") or []
                for name in sorted(t for t in tools
                                   if not t.startswith("mcp__")):
                    print(name)
                return
        sys.exit("no init SystemMessage received — CLI broken?")


anyio.run(main)
