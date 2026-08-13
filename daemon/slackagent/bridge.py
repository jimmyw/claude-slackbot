"""The daemon -> VM bridge.

One SSH invocation per Slack message. The same invocation carries a reverse
tunnel (-R) so the VM's approval hook can reach the daemon's approval listener:
the tunnel's lifetime is exactly the CLI run's lifetime, which is exactly when
the hook can fire. The VM therefore never holds a Slack token and never talks to
Slack at all.

The VM's authorized_keys pins the key to /usr/local/bin/agent-exec, so this key
cannot obtain a shell even though it can start a run.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass

from .config import Config

log = logging.getLogger(__name__)

# stream-json lines can carry a whole file's contents in a tool_result.
_STREAM_LINE_LIMIT = 32 * 1024 * 1024


class PortPool:
    """Hands out a distinct guest-side tunnel port per concurrent run.

    Every run reuses the same daemon-side listener, so without distinct guest
    ports a second concurrent run would fail to bind and silently ride the first
    run's tunnel — which then dies underneath it when that run finishes.
    """

    def __init__(self, low: int, high: int) -> None:
        self._free = list(range(low, high + 1))
        self._lock = asyncio.Lock()

    async def acquire(self) -> int:
        async with self._lock:
            if not self._free:
                raise RuntimeError("no free tunnel ports; too many concurrent runs")
            return self._free.pop(0)

    async def release(self, port: int) -> None:
        async with self._lock:
            self._free.append(port)


@dataclass
class RunResult:
    exit_code: int
    stderr: str


class Bridge:
    def __init__(self, config: Config) -> None:
        self._config = config
        self._ports = PortPool(config.tunnel_port_low, config.tunnel_port_high)

    async def run(
        self,
        *,
        prompt: str,
        session_id: str,
        resume: bool,
        run_token: str,
    ) -> AsyncIterator[dict]:
        """Start a run and yield each stream-json event as it arrives.

        The final item yielded is always a synthetic
        {"type": "_bridge", ...} event carrying the exit code and any stderr, so
        callers can report transport failures without a separate channel.
        """
        cfg = self._config
        port = await self._ports.acquire()
        process: asyncio.subprocess.Process | None = None
        stderr_chunks: list[bytes] = []

        try:
            command = [
                "ssh",
                "-T",
                "-i",
                str(cfg.vm_ssh_key),
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                f"UserKnownHostsFile={cfg.known_hosts}",
                "-o",
                "ServerAliveInterval=15",
                "-o",
                "ServerAliveCountMax=4",
                "-o",
                "ExitOnForwardFailure=yes",
                "-R",
                f"127.0.0.1:{port}:{cfg.approval_host}:{cfg.approval_port}",
                f"{cfg.vm_user}@{cfg.vm_host}",
            ]

            job = json.dumps(
                {
                    "prompt": prompt,
                    "session_id": session_id,
                    "resume": resume,
                    "run_token": run_token,
                    "approval_port": port,
                    "cwd": cfg.vm_workdir,
                }
            ).encode()

            log.info(
                "starting run session=%s resume=%s tunnel_port=%s",
                session_id,
                resume,
                port,
            )

            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LINE_LIMIT,
            )

            assert process.stdin is not None
            assert process.stdout is not None
            assert process.stderr is not None

            process.stdin.write(job)
            await process.stdin.drain()
            process.stdin.close()

            drain_stderr = asyncio.create_task(
                _drain(process.stderr, stderr_chunks)
            )

            async for line in process.stdout:
                text = line.strip()
                if not text:
                    continue
                try:
                    yield json.loads(text)
                except json.JSONDecodeError:
                    # Not fatal: a stray non-JSON line (a warning from the guest,
                    # say) should not kill an otherwise healthy run.
                    log.warning("unparseable stream line: %s", text[:400])

            exit_code = await process.wait()
            with contextlib.suppress(asyncio.CancelledError):
                await drain_stderr

            stderr = b"".join(stderr_chunks).decode(errors="replace").strip()
            if exit_code != 0:
                log.error("run failed exit=%s stderr=%s", exit_code, stderr[-2000:])

            yield {"type": "_bridge", "exit_code": exit_code, "stderr": stderr}

        finally:
            if process is not None and process.returncode is None:
                process.kill()
                with contextlib.suppress(ProcessLookupError):
                    await process.wait()
            await self._ports.release(port)

    async def probe(self) -> RunResult:
        """Check the SSH path without starting a Claude run.

        The forced command rejects an empty job with exit 64, which is a positive
        signal: SSH authenticated and agent-exec ran.
        """
        cfg = self._config
        process = await asyncio.create_subprocess_exec(
            "ssh",
            "-T",
            "-i",
            str(cfg.vm_ssh_key),
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={cfg.known_hosts}",
            "-o",
            "ConnectTimeout=10",
            f"{cfg.vm_user}@{cfg.vm_host}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await process.communicate()
        return RunResult(
            exit_code=process.returncode or 0,
            stderr=stderr.decode(errors="replace").strip(),
        )


async def _drain(stream: asyncio.StreamReader, into: list[bytes]) -> None:
    while chunk := await stream.read(8192):
        into.append(chunk)
