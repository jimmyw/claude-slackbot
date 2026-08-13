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
    def __init__(self, lines: list[bytes]) -> None:
        self._lines = lines

    def __aiter__(self):
        async def gen():
            for line in self._lines:
                yield line
        return gen()

    async def read(self, _n: int = -1) -> bytes:
        return b""

    def write(self, _data: bytes) -> None:
        return None

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


def make_config(tmp: Path, forward_agent: bool = False) -> Config:
    key = tmp / "key"
    key.write_text("x")
    return Config(
        bot_token="xoxb-real", app_token="xapp-real", authorized_user="U1",
        vm_host="10.9.9.9", vm_user="agent", vm_ssh_key=key,
        vm_domain="agent-vm", vm_workdir="/home/agent/work",
        libvirt_uri="qemu:///system",
        forward_agent=forward_agent,
        approval_host="127.0.0.1", approval_port=9100, approval_timeout_s=600,
        tunnel_port_low=9101, tunnel_port_high=9199,
        db_path=tmp / "s.sqlite3", update_interval_s=0.0,
    )


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
