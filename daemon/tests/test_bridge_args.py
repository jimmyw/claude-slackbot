"""Bridge argv tests — offline, no VM needed.

Regression guard for a failure that only appeared once the daemon ran as a real
systemd --user service. Mount-namespace sandboxing there (ProtectSystem,
ProtectHome, PrivateTmp) puts the unit in a user namespace where root-owned files
appear as nobody:nobody. ssh validates the ownership of its config files and dies
with "Bad owner or permissions on /etc/ssh/ssh_config.d/..." and exit 255 before
opening any connection. Passing -F makes ssh skip both the user and the
system-wide config, which avoids the check entirely.

Every bridge test before this one ran ssh from an ordinary shell, so none of them
could have caught it.

Run:  .venv/bin/python -m tests.test_bridge_args
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

from slackagent import bridge as bridge_mod
from slackagent.bridge import Bridge, PortPool
from slackagent.config import Config

failures: list[str] = []
captured: list[list[str]] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


class FakeStream:
    def __init__(self, lines=()) -> None:
        self._lines = list(lines)
        # Kept so a test can read the JSON job the daemon sent, not just the argv.
        self.written: list[bytes] = []

    def __aiter__(self):
        async def gen():
            for line in self._lines:
                yield line
        return gen()

    async def read(self, _n: int = -1) -> bytes:
        return b""

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        return None


class FakeProcess:
    def __init__(self) -> None:
        self.stdin = FakeStream([])
        self.stdout = FakeStream([b'{"type":"system","subtype":"init"}\n'])
        self.stderr = FakeStream([])
        self.returncode = 0

    async def wait(self) -> int:
        return 0

    async def communicate(self, _input: bytes | None = None):
        return b"", b""

    def kill(self) -> None:
        return None


def make_config(
    tmp: Path, forward_agent: bool = False, mcp_port: int = 9110
) -> Config:
    key = tmp / "key"
    key.write_text("x")
    return Config(
        bot_token="xoxb-real", app_token="xapp-real", authorized_user="U1",
        allowed_users=frozenset(),
        vm_host="10.9.9.9", vm_user="agent", vm_ssh_key=key,
        vm_domain="agent-vm", vm_workdir="/home/agent/work",
        libvirt_uri="qemu:///system",
        forward_agent=forward_agent,
        agent_policy="permissive",
        approval_host="127.0.0.1", approval_port=9100, approval_timeout_s=600,
        tunnel_port_low=9101, tunnel_port_high=9199,
        db_path=tmp / "s.sqlite3", update_interval_s=0.0,
        mcp_host="127.0.0.1", mcp_port=mcp_port,
        mcp_tunnel_port_low=9201, mcp_tunnel_port_high=9299,
    )


async def mcp_section(bridge_mod, captured, tmp, Bridge, Config) -> None:
    """The second reverse tunnel, and the generated MCP config in the job."""
    print("\n[4] the MCP tunnel is added only when there is something to reach")

    import json

    processes: list = []

    async def fake_exec(*args, **kwargs):
        captured.append(list(args))
        process = bridge_mod.asyncio.subprocess  # placeholder to keep linters quiet
        made = FakeProcess()
        processes.append(made)
        return made

    bridge_mod.asyncio.create_subprocess_exec = fake_exec  # type: ignore[assignment]

    config = make_config(tmp, mcp_port=9110)
    bridge = Bridge(config)

    captured.clear()
    processes.clear()
    async for _ in bridge.run(
        prompt="hi", session_id="s", resume=False, run_token="t"
    ):
        pass
    argv = captured[0]
    job = json.loads(b"".join(processes[0].stdin.written))
    check("no servers: exactly one -R, for approvals",
          argv.count("-R") == 1, [a for a in argv if a.startswith("127.0.0.1:")])
    check("no servers: no mcp port in the job", job.get("mcp_port") is None, job)
    check("no servers: nothing is sent as an mcp config", job.get("mcp_config") == "",
          job.get("mcp_config"))
    check("no servers: NOT strict, so a guest-configured MCP setup is left alone — "
          "this is what makes the change safe to deploy before the host file exists",
          job.get("mcp_strict") is False, job.get("mcp_strict"))

    captured.clear()
    processes.clear()
    async for _ in bridge.run(
        prompt="hi", session_id="s", resume=False, run_token="t",
        mcp_servers=("syslog", "varys"), mcp_strict=True,
    ):
        pass
    argv = captured[0]
    job = json.loads(b"".join(processes[0].stdin.written))
    forwards = [argv[i + 1] for i, a in enumerate(argv) if a == "-R"]
    check("with servers: two reverse tunnels", len(forwards) == 2, forwards)
    check("the second targets the mcp proxy",
          any(f.endswith(f":127.0.0.1:{config.mcp_port}") for f in forwards), forwards)
    check("both bind guest loopback only",
          all(f.startswith("127.0.0.1:") for f in forwards), forwards)
    check("the mcp port comes from its own pool, not the approval pool",
          config.mcp_tunnel_port_low <= job["mcp_port"] <= config.mcp_tunnel_port_high,
          job["mcp_port"])
    document = json.loads(job["mcp_config"])
    check("every server points at the root-owned relay",
          all(entry["command"] == bridge_mod.MCP_RELAY
              for entry in document["mcpServers"].values()), document)
    check("named so tool names stay mcp__<server>__<tool>, keeping existing grants "
          "valid", sorted(document["mcpServers"]) == ["syslog", "varys"], document)
    check("the relay is told the port and nothing else",
          document["mcpServers"]["syslog"]["env"] == {"AGENT_MCP_PORT": str(job["mcp_port"])},
          document["mcpServers"]["syslog"])
    check("no credential of any kind travels to the guest",
          "credential" not in job["mcp_config"] and "Cookie" not in job["mcp_config"])
    check("strict is set, so the guest's own MCP config is ignored",
          job["mcp_strict"] is True)

    print("\n[4b] a caller entitled to nothing still gets strict")
    captured.clear()
    processes.clear()
    async for _ in bridge.run(
        prompt="hi", session_id="s", resume=False, run_token="t",
        mcp_servers=(), mcp_strict=True,
    ):
        pass
    job = json.loads(b"".join(processes[0].stdin.written))
    check("an empty document is sent rather than no document — otherwise 'no servers "
          "for you' would fall back to whatever the VM has",
          json.loads(job["mcp_config"]) == {"mcpServers": {}}, job["mcp_config"])
    check("and still no second tunnel", captured[0].count("-R") == 1, captured[0])


async def main() -> int:
    import tempfile

    real_exec = bridge_mod.asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        captured.append(list(args))
        return FakeProcess()

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        config = make_config(tmp)
        bridge = Bridge(config)
        bridge_mod.asyncio.create_subprocess_exec = fake_exec  # type: ignore[assignment]
        try:
            print("\n[1] run() argv")
            captured.clear()
            async for _ in bridge.run(
                prompt="hi", session_id="sess-1", resume=False, run_token="tok"
            ):
                pass
            argv = captured[0]

            check("-F /dev/null is present", "-F" in argv and "/dev/null" in argv, argv)
            check("-F comes before the destination",
                  argv.index("-F") < argv.index("agent@10.9.9.9"), argv)
            check("identity is pinned", "-i" in argv and str(config.vm_ssh_key) in argv)
            check("IdentitiesOnly=yes", "IdentitiesOnly=yes" in argv)
            check("BatchMode=yes (never prompt from a daemon)",
                  "BatchMode=yes" in argv)
            check("known_hosts points at the writable state dir",
                  f"UserKnownHostsFile={config.known_hosts}" in argv, argv)
            check("ExitOnForwardFailure=yes (a lost tunnel must fail the run)",
                  "ExitOnForwardFailure=yes" in argv)
            check("a reverse tunnel is requested", "-R" in argv)
            tunnel = argv[argv.index("-R") + 1]
            check("tunnel targets the approval listener",
                  tunnel.endswith(f":127.0.0.1:{config.approval_port}"), tunnel)
            check("tunnel binds guest loopback only",
                  tunnel.startswith("127.0.0.1:"), tunnel)

            print("\n[2] agent forwarding is opt-in and explicit")
            check("forwarding OFF: no -A", "-A" not in argv, argv)
            check("forwarding OFF: ForwardAgent=no stated, not left to default",
                  "ForwardAgent=no" in argv, argv)

            captured.clear()
            fwd = Bridge(make_config(tmp, forward_agent=True))
            async for _ in fwd.run(
                prompt="hi", session_id="s", resume=False, run_token="t"
            ):
                pass
            fargv = captured[0]
            check("forwarding ON: -A present", "-A" in fargv, fargv)
            check("forwarding ON: no contradicting ForwardAgent=no",
                  "ForwardAgent=no" not in fargv, fargv)

            await mcp_section(bridge_mod, captured, tmp, Bridge, Config)
            check("forwarding ON: still no TTY", "-T" in fargv, fargv)

            print("\n[3] probe() argv")
            captured.clear()
            await bridge.probe()
            argv = captured[0]
            check("-F /dev/null is present in probe too",
                  "-F" in argv and "/dev/null" in argv, argv)
            check("probe does not request a tunnel", "-R" not in argv)

            print("\n[4] tunnel ports are not shared between concurrent runs")
            pool = PortPool(9101, 9102)
            a = await pool.acquire()
            b = await pool.acquire()
            check("two runs get distinct guest ports", a != b, (a, b))
            try:
                await pool.acquire()
                check("pool exhaustion raises", False, "it did not")
            except RuntimeError:
                check("pool exhaustion raises", True)
            await pool.release(a)
            check("a released port is reusable", await pool.acquire() == a)
        finally:
            bridge_mod.asyncio.create_subprocess_exec = real_exec  # type: ignore[assignment]

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
