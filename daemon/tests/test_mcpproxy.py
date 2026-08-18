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
from slackagent.mcpconfig import Credential, Registry
from slackagent.mcpproxy import McpProxy, OAuth, RunContext
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


class FakeTokenEndpoint:
    """An authorisation server that rotates its refresh tokens, as most do."""

    def __init__(self, *, rotate: bool = True, status: int = 200) -> None:
        self.rotate = rotate
        self.status = status
        self.calls: list[dict] = []

    def post(self, url: str, data: dict):  # noqa: ANN201  mimics aiohttp
        endpoint = self

        class _Response:
            status = endpoint.status

            async def json(self, content_type=None):  # noqa: ANN001, ARG002
                endpoint.calls.append(dict(data))
                if endpoint.status >= 400:
                    return {"error": "invalid_grant"}
                body = {"access_token": f"access-{len(endpoint.calls)}",
                        "expires_in": 3600}
                if endpoint.rotate:
                    body["refresh_token"] = f"refresh-{len(endpoint.calls)}"
                return body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_exc):
                return False

        return _Response()

    async def close(self) -> None:
        return None


async def test_oauth() -> None:
    print("\n[5] oauth tokens are minted and rotated on the host")
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        store = Store(tmp / "s.sqlite3")
        endpoint = FakeTokenEndpoint()
        credential = Credential(
            oauth={"token_url": "https://auth.example/token", "client_id": "cid",
                   "refresh_token": "bootstrap-refresh", "scope": "logs"},
            owner=OPERATOR,
        )
        auth = OAuth(store, "esp-crash", credential, session=endpoint)

        header = await auth.header()
        check("the first call refreshes and returns a bearer token",
              header == {"Authorization": "Bearer access-1"}, header)
        check("it used the bootstrap refresh token from the config file",
              endpoint.calls[0]["refresh_token"] == "bootstrap-refresh",
              endpoint.calls[0])
        check("and sent the client id and scope",
              endpoint.calls[0]["client_id"] == "cid"
              and endpoint.calls[0]["scope"] == "logs", endpoint.calls[0])

        row = store.mcp_token("esp-crash", OPERATOR)
        check("the grant is persisted, keyed to the person",
              row["access_token"] == "access-1", dict(row))
        check("the ROTATED refresh token is persisted — keeping the file's copy is "
              "what silently bricks the credential after one refresh",
              row["refresh_token"] == "refresh-1", dict(row))

        await auth.header()
        check("an unexpired token is reused rather than refreshed again",
              len(endpoint.calls) == 1, endpoint.calls)

        await auth.header(force=True)
        check("force refreshes, which is what a 401 mid-session needs",
              len(endpoint.calls) == 2, endpoint.calls)
        check("and the second refresh used the rotated token, not the original",
              endpoint.calls[1]["refresh_token"] == "refresh-1", endpoint.calls[1])

        store.save_mcp_token("esp-crash", OPERATOR, access_token="stale",
                             refresh_token="refresh-2", expires_at=int(__import__("time").time()) + 10)
        await auth.header()
        check("a token inside the expiry skew is refreshed early",
              len(endpoint.calls) == 3, endpoint.calls)

        print("\n[5b] a refusal from the authorisation server")
        broken = OAuth(store, "esp-crash", credential,
                       session=FakeTokenEndpoint(status=400))
        raised = ""
        try:
            await broken.header(force=True)
        except RuntimeError as exc:
            raised = str(exc)
        check("it raises something an operator can act on",
              "invalid_grant" in raised and "esp-crash" in raised, raised)

        nothing = OAuth(store, "other", Credential(oauth={"token_url": "u"}),
                        session=FakeTokenEndpoint())
        raised = ""
        try:
            await nothing.header()
        except RuntimeError as exc:
            raised = str(exc)
        check("with no refresh token at all it says to re-authorise",
              "re-authorise" in raised, raised)
        check("a server with no oauth block does not claim to be configured",
              not OAuth(store, "x", Credential()).configured)
        store.close()


async def test_command() -> None:
    """|mcp against a real registry and a real upstream."""
    print("\n[6] |mcp")
    from slackagent import commands
    from slackagent.commands import mcp as mcp_command

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        async with Harness(tmp, document(tmp / "cmd"), name="cmd") as h:
            said: list[str] = []

            async def say(message: str) -> None:
                said.append(message)

            def context() -> commands.Context:
                return commands.Context(
                    channel="C1", thread_ts="1.1", message_ts="1.2", user=OPERATOR,
                    is_operator=True, config=None, store=h.store, vm=None,
                    bridge=None, approvals=None, say=say, mcp=h.proxy,
                )

            async def run(*argv: str) -> str:
                said.clear()
                parsed = mcp_command.build_parser().parse_args(list(argv))
                await mcp_command.run(context(), parsed)
                return said[-1] if said else ""

            out = await run()
            check("|mcp lists the servers and where the credentials are",
                  "syslog" in out and "varys" in out
                  and "credentials live on the host" in out, out[:160])
            check("it says which tools are allowed", "`query_logs`" in out, out[:400])
            check("and never prints a credential",
                  "shared-t0ken" not in out and "personal-t0ken" not in out, out)

            out = await run("tools", "syslog")
            check("|mcp tools asks the upstream live, and marks what is blocked",
                  "query_logs" in out and "pulse_reboot" in out
                  and "blocked" in out, out[:400])
            check("it suggests the command that would allow one",
                  "|mcp allow syslog pulse_reboot" in out, out[-200:])

            out = await run("allow", "syslog", "pulse_*")
            check("|mcp allow writes a runtime rule", "now *allowed*" in out, out)
            check("which the policy then honours",
                  mcp_command.decide(
                      h.registry.get("syslog"), OPERATOR, "pulse_reboot",
                      extra_allow=("pulse_*",)).allowed)

            out = await run("tools", "syslog")
            check("and the tool moves to allowed", "blocked" not in out, out[:300])

            rule_id = h.store.mcp_policy("syslog")[0]["id"]
            out = await run("forget", str(rule_id))
            check("|mcp forget drops it", "Dropped rule" in out, out)

            out = await run("disable", "syslog")
            check("|mcp disable stops it being offered",
                  "no longer offered" in out and h.store.mcp_disabled() == {"syslog"},
                  out)
            out = await run("enable", "syslog")
            check("|mcp enable brings it back",
                  "offered again" in out and h.store.mcp_disabled() == set(), out)

            h.store.record_mcp_call(
                slack_user=OPERATOR, channel_id="C1", thread_ts="1.1",
                session_id="s", server="syslog", tool="query_logs",
                decision="allowed",
            )
            out = await run("calls")
            check("|mcp calls shows the audit trail, naming the person",
                  "syslog.query_logs" in out and OPERATOR in out, out)

            errors: list[str] = []
            for argv in (("tools", "nosuch"), ("allow", "syslog"), ("forget", "x")):
                try:
                    await run(*argv)
                except commands.CommandError as exc:
                    errors.append(str(exc))
            check("bad input is reported, not raised at the daemon",
                  len(errors) == 3, errors)
            check("an unknown server lists the real ones",
                  "syslog" in errors[0], errors[0])


async def main() -> int:
    check("the relay script is executable and root-installable",
          RELAY.exists() and bool(RELAY.stat().st_mode & stat.S_IXUSR), RELAY)
    await test_happy_path()
    await test_refusals()
    await test_caps()
    await test_runtime_policy()
    await test_oauth()
    await test_command()
    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
