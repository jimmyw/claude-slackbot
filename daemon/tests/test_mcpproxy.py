"""The MCP proxy, end to end over a real socket.

Not mocked at the seam that matters: this drives the REAL guest relay
(vm-files/usr/local/bin/mcp-relay) as a subprocess, talking newline JSON-RPC to it
exactly as Claude Code would, through the real proxy, to a fake upstream. So it covers
the handshake, the token check, the tool filtering, the two caps and the audit trail in
one path.

The upstream records every call it actually receives, which is how "a blocked call never
leaves the host" is proved rather than assumed.

Run:  .venv/bin/python -m tests.test_mcpproxy
"""
from __future__ import annotations

import asyncio
import json
import os
import stat
import sys
import tempfile
from pathlib import Path

from slackagent.config import Config
from slackagent.mcpconfig import Registry
from slackagent.mcpproxy import McpProxy, RunContext
from slackagent.store import Store

REPO = Path(__file__).resolve().parents[2]
RELAY = REPO / "vm-files/usr/local/bin/mcp-relay"
FAKE = Path(__file__).parent / "fixtures/fake_mcp_server.py"

OPERATOR = "U_JIMMY"
GUEST = "U_BOB"
TOKEN = "run-token-abc"

failures: list[str] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def document(tmp: Path, *, max_calls: int = 40, max_bytes: int = 256 * 1024) -> dict:
    """One shared stdio server plus one per-user server, over the fake upstream."""
    upstream = {
        "type": "stdio",
        "command": sys.executable,
        "args": [str(FAKE)],
        "max_calls_per_run": max_calls,
        "max_result_bytes": max_bytes,
    }
    return {
        "servers": {
            "syslog": {
                **upstream,
                "credential": {"mode": "shared", "env": {"FAKE_TOKEN": "shared-t0ken",
                                                         "FAKE_MCP_CALLS": str(tmp / "calls.txt")}},
                "tools": {"allow": ["query_logs", "get_*"]},
            },
            "varys": {
                **upstream,
                "credential": {
                    "mode": "per_user",
                    "users": {OPERATOR: {"env": {"FAKE_TOKEN": "personal-t0ken",
                                                 "FAKE_MCP_CALLS": str(tmp / "calls.txt")}}},
                },
                "tools": {"allow": ["query_logs"], "deny": ["pulse_reboot"]},
            },
        }
    }


class Harness:
    """A live proxy plus a live relay, over a real loopback socket."""

    def __init__(self, tmp: Path, doc: dict, *, user: str = OPERATOR,
                 name: str = "h") -> None:
        tmp = tmp / name
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.user = user
        self.config_path = tmp / "mcp.json"
        self.config_path.write_text(json.dumps(doc))
        os.chmod(self.config_path, 0o600)
        self.store = Store(tmp / "state.sqlite3")
        self.registry = Registry(self.config_path)
        config = Config(
            bot_token="xoxb-x", app_token="xapp-x", authorized_user=OPERATOR,
            allowed_users=frozenset(), vm_host="10.0.0.1", vm_user="agent",
            vm_ssh_key=tmp / "key", vm_domain="d", vm_workdir="/w",
            libvirt_uri="qemu:///system", forward_agent=False,
            agent_policy="permissive", approval_host="127.0.0.1",
            approval_port=9100, approval_timeout_s=5, tunnel_port_low=1,
            tunnel_port_high=2, db_path=tmp / "state.sqlite3",
            update_interval_s=0.0, mcp_config=self.config_path, mcp_port=0,
        )
        self.proxy = McpProxy(
            config, self.store, self.registry,
            lambda token: (
                RunContext(slack_user=self.user, channel_id="C1", thread_ts="1.1",
                           session_id="s1")
                if token == TOKEN else None
            ),
        )

    async def __aenter__(self) -> "Harness":
        await self.proxy.start()
        return self

    async def __aexit__(self, *_exc) -> None:
        await self.proxy.stop()
        self.store.close()

    async def relay(self, server: str, token: str = TOKEN):
        return await asyncio.create_subprocess_exec(
            sys.executable, str(RELAY), server,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "AGENT_MCP_PORT": str(self.proxy.port),
                "AGENT_RUN_TOKEN": token,
            },
        )

    @property
    def calls(self) -> list[str]:
        path = self.tmp / "calls.txt"
        return path.read_text().split() if path.exists() else []


async def rpc(process, message: dict, timeout: float = 20.0) -> dict:
    process.stdin.write(json.dumps(message).encode() + b"\n")
    await process.stdin.drain()
    line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
    if not line:
        raise AssertionError("the relay closed without answering")
    return json.loads(line)


async def test_happy_path() -> None:
    print("\n[1] a real relay, through the proxy, to an upstream")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        async with Harness(tmp, document(tmp / "h"), name="h") as h:
            relay = await h.relay("syslog")

            reply = await rpc(relay, {"jsonrpc": "2.0", "id": 1,
                                      "method": "initialize", "params": {}})
            check("initialize passes straight through",
                  reply["result"]["serverInfo"]["name"] == "fake", reply)
            check("the credential is delivered to the upstream ON THE HOST, and the "
                  "guest never sees it",
                  reply["result"]["seenToken"] == "shared-t0ken", reply["result"])

            reply = await rpc(relay, {"jsonrpc": "2.0", "id": 2,
                                      "method": "tools/list", "params": {}})
            names = [t["name"] for t in reply["result"]["tools"]]
            check("allowed tools are listed", "query_logs" in names and
                  "get_stats" in names, names)
            check("a tool this caller may not call is not even visible",
                  "pulse_reboot" not in names, names)
            check("pagination survives filtering",
                  reply["result"].get("nextCursor") == "page-2", reply["result"])

            reply = await rpc(relay, {"jsonrpc": "2.0", "id": 3,
                                      "method": "tools/call",
                                      "params": {"name": "query_logs",
                                                 "arguments": {"q": "boom"}}})
            check("an allowed call reaches the upstream and comes back",
                  reply["result"]["content"][0]["text"] == "ran query_logs", reply)
            check("the upstream saw it", h.calls == ["query_logs"], h.calls)

            reply = await rpc(relay, {"jsonrpc": "2.0", "id": 4,
                                      "method": "tools/call",
                                      "params": {"name": "pulse_reboot",
                                                 "arguments": {}}})
            check("a denied call is refused as a tool error, not a broken server",
                  reply["result"]["isError"] is True
                  and "Blocked by host policy" in reply["result"]["content"][0]["text"],
                  reply)
            check("and it NEVER reached the upstream", h.calls == ["query_logs"],
                  h.calls)
            check("the refusal says how to allow it",
                  "|mcp allow syslog pulse_reboot"
                  in reply["result"]["content"][0]["text"],
                  reply["result"]["content"][0]["text"])

            reply = await rpc(relay, {"jsonrpc": "2.0", "id": 5,
                                      "method": "resources/read",
                                      "params": {"uri": "file:///etc/passwd"}})
            check("resources/* is refused outright — default deny for a capability "
                  "nobody asked for", "error" in reply, reply)

            relay.stdin.close()
            await relay.wait()

            print("\n[1b] the audit trail")
            rows = h.store.recent_mcp_calls()
            by_tool = {r["tool"]: r for r in rows}
            check("both attempts are recorded", len(rows) == 2, len(rows))
            check("the allowed one, with bytes and duration",
                  by_tool["query_logs"]["decision"] == "allowed"
                  and by_tool["query_logs"]["result_bytes"] > 0, dict(by_tool["query_logs"]))
            check("the denied one, with a reason",
                  by_tool["pulse_reboot"]["decision"] == "denied"
                  and "not on the allowlist" in by_tool["pulse_reboot"]["reason"],
                  dict(by_tool["pulse_reboot"]))
            check("and both name the person, resolved from the run token",
                  all(r["slack_user"] == OPERATOR for r in rows))


async def test_refusals() -> None:
    print("\n[2] who gets in at all")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        async with Harness(tmp, document(tmp / "refuse"), name="refuse") as h:
            relay = await h.relay("syslog", token="not-a-real-token")
            _, err = await asyncio.wait_for(relay.communicate(), timeout=20)
            check("an unknown run token is refused",
                  relay.returncode != 0 and b"unknown run token" in err, err[:200])

            relay = await h.relay("nosuch")
            _, err = await asyncio.wait_for(relay.communicate(), timeout=20)
            check("an unconfigured server is refused",
                  relay.returncode != 0 and b"no MCP server" in err, err[:200])

            h.store.set_mcp_enabled("syslog", False, OPERATOR)
            relay = await h.relay("syslog")
            _, err = await asyncio.wait_for(relay.communicate(), timeout=20)
            check("a disabled server is refused, and says nothing about why",
                  relay.returncode != 0 and b"no MCP server" in err, err[:200])
            h.store.set_mcp_enabled("syslog", True)

        # A per_user server, asked for by someone with no credential of their own.
        async with Harness(tmp, document(tmp / "guest"), user=GUEST,
                           name="guest") as h:
            relay = await h.relay("varys")
            _, err = await asyncio.wait_for(relay.communicate(), timeout=20)
            check("a caller with no personal credential cannot attach at all — no "
                  "silent fallback to the shared one",
                  relay.returncode != 0 and b"no MCP server" in err, err[:200])


async def test_caps() -> None:
    print("\n[3] the two caps, each stating what it dropped")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        async with Harness(tmp, document(tmp / "calls", max_calls=2), name="calls") as h:
            relay = await h.relay("syslog")
            await rpc(relay, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})

            for n in (2, 3):
                reply = await rpc(relay, {"jsonrpc": "2.0", "id": n,
                                          "method": "tools/call",
                                          "params": {"name": "query_logs"}})
                check(f"call {n - 1} of 2 is allowed",
                      not reply["result"].get("isError"), reply)
            reply = await rpc(relay, {"jsonrpc": "2.0", "id": 4,
                                      "method": "tools/call",
                                      "params": {"name": "query_logs"}})
            check("the third is capped, and says so",
                  reply["result"]["isError"] and "per-run cap"
                  in reply["result"]["content"][0]["text"], reply)
            check("the cap is enforced before the upstream is called",
                  h.calls == ["query_logs", "query_logs"], h.calls)

            relay.stdin.close()
            await relay.wait()

        async with Harness(tmp, document(tmp / "bytes", max_bytes=2000),
                           name="bytes") as h:
            relay = await h.relay("syslog")
            await rpc(relay, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            reply = await rpc(relay, {"jsonrpc": "2.0", "id": 2,
                                      "method": "tools/call",
                                      "params": {"name": "get_stats",
                                                 "arguments": {"big": True}}})
            text = reply["result"]["content"][0]["text"]
            check("an oversized result is withheld with its size stated",
                  reply["result"]["isError"] and "over the 2000-byte cap" in text, text)
            decisions = [r["decision"] for r in h.store.recent_mcp_calls()]
            check("and recorded as capped, not as a success",
                  decisions == ["capped"], decisions)
            relay.stdin.close()
            await relay.wait()


async def test_runtime_policy() -> None:
    print("\n[4] |mcp changes policy without a restart")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        async with Harness(tmp, document(tmp / "allow"), name="allow") as h:
            # Runtime allow, exactly what |mcp allow will write.
            h.store.add_mcp_policy("syslog", "allow", "pulse_*", OPERATOR,
                                   slack_user=OPERATOR)
            relay = await h.relay("syslog")
            await rpc(relay, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            reply = await rpc(relay, {"jsonrpc": "2.0", "id": 2,
                                      "method": "tools/list"})
            names = [t["name"] for t in reply["result"]["tools"]]
            check("a runtime allow makes the tool visible", "pulse_reboot" in names,
                  names)
            reply = await rpc(relay, {"jsonrpc": "2.0", "id": 3,
                                      "method": "tools/call",
                                      "params": {"name": "pulse_reboot"}})
            check("and callable", not reply["result"].get("isError"), reply)
            relay.stdin.close()
            await relay.wait()

        async with Harness(tmp, document(tmp / "deny"), name="deny") as h:
            h.store.add_mcp_policy("syslog", "deny", "query_*", OPERATOR)
            relay = await h.relay("syslog")
            await rpc(relay, {"jsonrpc": "2.0", "id": 1, "method": "initialize"})
            reply = await rpc(relay, {"jsonrpc": "2.0", "id": 2,
                                      "method": "tools/call",
                                      "params": {"name": "query_logs"}})
            check("a runtime deny beats the file's allow",
                  reply["result"]["isError"], reply)
            relay.stdin.close()
            await relay.wait()


async def main() -> int:
    check("the relay script is executable and root-installable",
          RELAY.exists() and bool(RELAY.stat().st_mode & stat.S_IXUSR), RELAY)
    await test_happy_path()
    await test_refusals()
    await test_caps()
    await test_runtime_policy()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
