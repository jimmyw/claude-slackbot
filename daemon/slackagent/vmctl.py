"""virsh helpers.

The VM is long-lived and set to autostart, so the daemon does not normally
manage its lifecycle — these exist so a Slack `status` command can report the
truth, and so a message that arrives while the domain is down can start it
instead of timing out on SSH.

Every call passes --connect explicitly. For an unprivileged user, `virsh uri`
resolves to qemu:///session, which is a *different, empty* hypervisor instance
from the qemu:///system one the VM lives in. Relying on the default would make
every lookup here silently report "missing" no matter what the VM was doing.
"""
from __future__ import annotations

import asyncio
import logging

log = logging.getLogger(__name__)

SYSTEM_URI = "qemu:///system"


class VmControl:
    def __init__(self, domain: str, uri: str = SYSTEM_URI) -> None:
        self._domain = domain
        self._uri = uri

    async def state(self) -> str:
        """One of: running, shut off, paused, pmsuspended, unknown, missing."""
        code, out, _ = await self._virsh("domstate", self._domain)
        if code != 0:
            return "missing"
        return out.strip() or "unknown"

    async def is_running(self) -> bool:
        return await self.state() == "running"

    async def start(self) -> bool:
        if await self.is_running():
            return True
        code, _, err = await self._virsh("start", self._domain)
        if code != 0:
            log.error("could not start %s: %s", self._domain, err.strip())
            return False
        return True

    async def ip_address(self) -> str | None:
        code, out, _ = await self._virsh("-q", "domifaddr", self._domain)
        if code != 0:
            return None
        for line in out.splitlines():
            fields = line.split()
            if len(fields) >= 4 and "ipv4" in fields[2]:
                return fields[3].split("/")[0]
        return None

    async def _virsh(self, *args: str) -> tuple[int, str, str]:
        process = await asyncio.create_subprocess_exec(
            "virsh",
            "--connect",
            self._uri,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await process.communicate()
        return (
            process.returncode or 0,
            out.decode(errors="replace"),
            err.decode(errors="replace"),
        )
