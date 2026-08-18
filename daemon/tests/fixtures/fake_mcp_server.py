#!/usr/bin/env python3
"""A minimal stdio MCP server, for testing the proxy without a real upstream.

Speaks just enough of the protocol to be relayed: initialize, a paginated tools/list,
tools/call, and ping. Every call it actually receives is appended to $FAKE_MCP_CALLS,
which is how a test proves a blocked call never reached the upstream at all.
"""
from __future__ import annotations

import json
import os
import sys

TOOLS = [
    {"name": "query_logs", "description": "search logs"},
    {"name": "get_stats", "description": "counters"},
    {"name": "pulse_reboot", "description": "reboot a device"},
]


def note(tool: str) -> None:
    path = os.environ.get("FAKE_MCP_CALLS")
    if path:
        with open(path, "a") as handle:
            handle.write(tool + "\n")


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        message = json.loads(line)
        method = message.get("method")
        request_id = message.get("id")
        if request_id is None:
            continue  # a notification: nothing to answer

        if method == "initialize":
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "fake", "version": "1"},
                # Echoed back so a test can prove the env reached the upstream, which
                # is where a stdio credential is delivered.
                "seenToken": os.environ.get("FAKE_TOKEN", ""),
            }
        elif method == "tools/list":
            cursor = (message.get("params") or {}).get("cursor")
            result = {"tools": TOOLS}
            if cursor is None:
                result["nextCursor"] = "page-2"
        elif method == "tools/call":
            params = message.get("params") or {}
            tool = params.get("name") or ""
            note(tool)
            arguments = params.get("arguments") or {}
            text = "x" * 400_000 if arguments.get("big") else f"ran {tool}"
            result = {"content": [{"type": "text", "text": text}]}
        elif method == "ping":
            result = {}
        else:
            sys.stdout.write(json.dumps({
                "jsonrpc": "2.0", "id": request_id,
                "error": {"code": -32601, "message": f"no {method}"},
            }) + "\n")
            sys.stdout.flush()
            continue

        sys.stdout.write(
            json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result}) + "\n"
        )
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
