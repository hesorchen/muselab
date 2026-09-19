#!/usr/bin/env python3
"""Exercise the installed SDK/CLI checkpoint against a localhost fake model.

Only temporary files and loopback HTTP are used. No provider credentials, real
prompts or protocol bodies are printed. This is a protocol test, not an LLM test.
"""

from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading

from claude_agent_sdk import (
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    UserMessage,
)


async def probe(*, all_tools: bool = False, instructions: str | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="muselab-checkpoint-offline-") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        config = root / "config"
        config.mkdir()
        instruction_marker = "SYNTHETIC_PROJECT_INSTRUCTION_7b98a2"
        if instructions:
            name = "CLAUDE.md" if instructions == "claude" else "AGENTS.md"
            (workspace / name).write_text(instruction_marker + "\n")
            if instructions == "import":
                (workspace / "CLAUDE.md").write_text("@AGENTS.md\n")
        instruction_observed = []
        target = workspace / "sample.txt"
        target.write_text("before\n")
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                request = json.loads(self.rfile.read(length) or b"{}")
                requests.append(self.path.split("?", 1)[0])
                if instructions and "count_tokens" not in self.path:
                    instruction_observed.append(instruction_marker in json.dumps(request))
                if "count_tokens" in self.path:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"input_tokens":20}')
                    return
                completed_ids = {
                    b.get("tool_use_id")
                    for m in request.get("messages", [])
                    if isinstance(m, dict) and isinstance(m.get("content"), list)
                    for b in m["content"]
                    if isinstance(b, dict) and b.get("type") == "tool_result"
                }
                tool_done = "offline_write" in completed_ids
                if tool_done:
                    block = {"type": "text", "text": "Done."}
                elif "offline_read" in completed_ids:
                    block = {
                        "type": "tool_use",
                        "id": "offline_write",
                        "name": "Write",
                        "input": {"file_path": str(target), "content": "after\n"},
                    }
                else:
                    block = {
                        "type": "tool_use",
                        "id": "offline_read",
                        "name": "Read",
                        "input": {"file_path": str(target)},
                    }
                stop = "end_turn" if tool_done else "tool_use"
                message = {
                    "id": f"offline_message_{len(requests)}",
                    "type": "message",
                    "role": "assistant",
                    "model": request.get("model", "claude-sonnet-4-6"),
                    "content": [block],
                    "stop_reason": stop,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 20, "output_tokens": 10},
                }
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/event-stream" if request.get("stream") else "application/json",
                )
                self.end_headers()
                if not request.get("stream"):
                    self.wfile.write(json.dumps(message).encode())
                    return

                def emit(kind, value):
                    self.wfile.write(
                        (
                            "event: "
                            + kind
                            + "\ndata: "
                            + json.dumps({"type": kind, **value})
                            + "\n\n"
                        ).encode()
                    )

                emit(
                    "message_start",
                    {
                        "message": {
                            **message,
                            "content": [],
                            "stop_reason": None,
                            "usage": {"input_tokens": 20, "output_tokens": 0},
                        }
                    },
                )
                start = {"type": "text", "text": ""} if tool_done else {**block, "input": {}}
                emit("content_block_start", {"index": 0, "content_block": start})
                delta = (
                    {"type": "text_delta", "text": "Done."}
                    if tool_done
                    else {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}
                )
                emit("content_block_delta", {"index": 0, "delta": delta})
                emit("content_block_stop", {"index": 0})
                emit(
                    "message_delta",
                    {
                        "delta": {"stop_reason": stop, "stop_sequence": None},
                        "usage": {"output_tokens": 10},
                    },
                )
                emit("message_stop", {})
                self.wfile.flush()

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        environment = {
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{server.server_port}",
            "ANTHROPIC_API_KEY": "offline-test-not-a-real-key",
            "ANTHROPIC_AUTH_TOKEN": "",
            "CLAUDE_CONFIG_DIR": str(config),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "HTTP_PROXY": "",
            "HTTPS_PROXY": "",
            "ALL_PROXY": "",
        }
        options = ClaudeAgentOptions(
            cwd=str(workspace),
            model="claude-sonnet-4-6",
            env=environment,
            tools=None if all_tools else ["Read", "Write"],
            setting_sources=["project"] if instructions else [],
            plugins=[],
            permission_mode="bypassPermissions",
            enable_file_checkpointing=True,
            extra_args={"replay-user-messages": None},
            max_turns=4,
            max_thinking_tokens=0,
        )
        checkpoint_id = None
        result_ok = False
        builtin_tools = []
        try:
            async with asyncio.timeout(60):
                async with ClaudeSDKClient(options=options) as client:
                    await client.query("Write the requested content to the sample file.")
                    async for message in client.receive_response():
                        if isinstance(message, SystemMessage) and message.subtype == "init":
                            builtin_tools = sorted(
                                str(name) for name in message.data.get("tools", [])
                            )
                        if (
                            isinstance(message, UserMessage)
                            and message.uuid
                            and isinstance(message.content, str)
                        ):
                            checkpoint_id = message.uuid
                        if isinstance(message, ResultMessage):
                            result_ok = not message.is_error
                    wrote = target.read_text() == "after\n"
                    if checkpoint_id:
                        await client.rewind_files(checkpoint_id)
                    restored = target.read_text() == "before\n"
        finally:
            server.shutdown()
            server.server_close()
        return {
            **({
                "instructions": instructions,
                "instruction_loaded": any(instruction_observed),
            } if instructions else {}),
            "localhost_only_model": True,
            "model_request_count": len(requests),
            "sdk_result_ok": result_ok,
            "builtin_tools": builtin_tools,
            "checkpoint_received": bool(checkpoint_id),
            "actual_file_written": wrote,
            "actual_file_restored": restored,
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--all-tools",
        action="store_true",
        help="Capture the full default CLI tool catalog; the local model still only uses Read/Write",
    )
    parser.add_argument(
        "--instructions",
        choices=("claude", "agents", "import"),
        help="Probe project instructions; native AGENTS.md can be unavailable with feature flags off",
    )
    args = parser.parse_args()
    result = asyncio.run(probe(all_tools=args.all_tools, instructions=args.instructions))
    print(json.dumps(result))
    raise SystemExit(
        0
        if result["checkpoint_received"]
        and result["actual_file_written"]
        and result["actual_file_restored"]
        and result.get("instruction_loaded", True)
        else 1
    )
