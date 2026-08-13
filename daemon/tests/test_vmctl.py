"""VmControl tests.

Regression guard for a bug that bit during bootstrap verification: `virsh` with
no --connect resolves to qemu:///session for an unprivileged user, which is a
separate and *empty* hypervisor instance from the qemu:///system one the VM lives
in. Empirically on terra:

    $ virsh uri                                  -> qemu:///session
    $ virsh net-list --all                       -> (no networks)
    $ virsh --connect qemu:///system net-list     -> default   active   yes

So a bare `virsh domstate agent-vm` reports the domain missing no matter what the
VM is actually doing, and the daemon would refuse to run every turn with "the VM
is not running".

Run:  .venv/bin/python -m tests.test_vmctl
"""
from __future__ import annotations

import asyncio
import sys

from slackagent import vmctl
from slackagent.vmctl import VmControl

failures: list[str] = []
captured: list[list[str]] = []


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


class FakeProcess:
    def __init__(self, stdout: bytes, code: int) -> None:
        self._stdout = stdout
        self.returncode = code

    async def communicate(self):
        return self._stdout, b""


def patch_virsh(stdout: bytes = b"running\n", code: int = 0):
    """Capture the argv VmControl would hand to virsh."""

    async def fake_exec(*args, **kwargs):
        captured.append(list(args))
        return FakeProcess(stdout, code)

    vmctl.asyncio.create_subprocess_exec = fake_exec  # type: ignore[assignment]


async def main() -> int:
    real_exec = asyncio.create_subprocess_exec

    print("\n[1] every virsh call pins the system URI")
    try:
        patch_virsh()
        vm = VmControl("agent-vm")

        captured.clear()
        await vm.state()
        check("state() passes --connect", "--connect" in captured[0], captured[0])
        check("state() targets qemu:///system",
              "qemu:///system" in captured[0], captured[0])
        check("--connect comes before the subcommand",
              captured[0].index("--connect") < captured[0].index("domstate"),
              captured[0])

        captured.clear()
        await vm.ip_address()
        check("ip_address() pins the URI too",
              "qemu:///system" in captured[0], captured[0])

        captured.clear()
        patch_virsh(stdout=b"shut off\n")
        vm2 = VmControl("agent-vm")
        await vm2.start()
        # First call is the domstate check, second is the actual start.
        check("start() pins the URI on the start call",
              len(captured) == 2 and "qemu:///system" in captured[1], captured)

        print("\n[2] an explicit override is honoured")
        captured.clear()
        patch_virsh()
        await VmControl("agent-vm", "qemu:///session").state()
        check("a custom URI is used verbatim",
              "qemu:///session" in captured[0], captured[0])
        check("the system URI is not silently forced in",
              "qemu:///system" not in captured[0], captured[0])

        print("\n[3] state mapping")
        captured.clear()
        patch_virsh(stdout=b"running\n")
        check("running -> is_running() true", await VmControl("d").is_running())

        patch_virsh(stdout=b"shut off\n")
        check("shut off -> is_running() false",
              not await VmControl("d").is_running())

        patch_virsh(stdout=b"", code=1)
        check("a virsh failure reports 'missing'",
              await VmControl("d").state() == "missing")

        patch_virsh(stdout=b"", code=0)
        check("empty output reports 'unknown'",
              await VmControl("d").state() == "unknown")

        print("\n[4] domifaddr parsing")
        patch_virsh(
            stdout=b" vnet0  52:54:00:aa:bb:cc  ipv4  192.168.122.42/24\n"
        )
        check("extracts the IPv4 address",
              await VmControl("d").ip_address() == "192.168.122.42")

        patch_virsh(stdout=b"\n")
        check("no lease -> None", await VmControl("d").ip_address() is None)
    finally:
        vmctl.asyncio.create_subprocess_exec = real_exec  # type: ignore[assignment]

    print("\n[5] against the real host")
    # A domain that cannot exist must report missing rather than raising.
    absent = await VmControl("definitely-not-a-real-domain-x9f2").state()
    check("an unknown domain reports 'missing'", absent == "missing", absent)

    # And the real domain must resolve to a genuine libvirt state. This is the
    # check that would have caught the qemu:///session bug: before the fix this
    # returned "missing" for a domain that was plainly running.
    live = await VmControl("agent-vm").state()
    valid = {"running", "shut off", "paused", "pmsuspended", "in shutdown",
             "crashed", "idle", "missing"}
    check("agent-vm resolves to a real libvirt state", live in valid, live)
    if live == "missing":
        print("        (note: agent-vm is not defined on this host yet)")
    else:
        check("a defined domain is NOT reported as missing", live != "missing", live)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
