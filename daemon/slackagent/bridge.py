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


# ssh is invoked with -F /dev/null throughout. Two reasons, one of them a hard
# requirement:
#
#   * This runs as a `systemctl --user` unit, and any mount-namespace sandboxing
#     there (ProtectSystem, ProtectHome, PrivateTmp) places the service in a user
#     namespace where root-owned files appear as nobody:nobody. ssh validates the
#     ownership of its config files and aborts with "Bad owner or permissions on
#     /etc/ssh/ssh_config.d/...", exit 255, before ever opening a connection.
#     Passing -F also makes ssh skip the system-wide config, which sidesteps it.
#   * Independently: a daemon should not inherit ambient host ssh config. Every
#     option it needs is passed explicitly below, so a stray ProxyCommand or
#     Host * block in /etc/ssh/ssh_config cannot change its behaviour.
_NO_SSH_CONFIG = ("-F", "/dev/null")

# Root-owned in the guest, so the agent cannot edit its own route to the host.
MCP_RELAY = "/usr/local/bin/mcp-relay"


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
        # Separate pool for the MCP forward: sharing one would let a run take the port
        # another run needs for approvals, which is the more important of the two.
        self._mcp_ports = PortPool(
            config.mcp_tunnel_port_low, config.mcp_tunnel_port_high
        )

    async def run(
        self,
        *,
        prompt: str,
        session_id: str,
        resume: bool,
        run_token: str,
        policy: str | None = None,
        system_append: str = "",
        mcp_servers: tuple[str, ...] = (),
        mcp_strict: bool = False,
    ) -> AsyncIterator[dict]:
        """Start a run and yield each stream-json event as it arrives.

        The final item yielded is always a synthetic
        {"type": "_bridge", ...} event carrying the exit code and any stderr, so
        callers can report transport failures without a separate channel.
        """
        cfg = self._config
        port = await self._ports.acquire()
        # No MCP servers for this run means no second forward at all. ssh runs with
        # ExitOnForwardFailure, so an unused port that happens to be taken would
        # otherwise be able to kill a run for a feature nobody is using.
        mcp_port = await self._mcp_ports.acquire() if mcp_servers else None
        process: asyncio.subprocess.Process | None = None
        stderr_chunks: list[bytes] = []

        try:
            command = [
                "ssh",
                "-T",
                *_NO_SSH_CONFIG,
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
                # Agent forwarding lets the guest clone private repos without any
                # credential of its own. The key stays in the host's ssh-agent and
                # is destination-constrained to git@github.com via this hop, so a
                # compromised guest can neither steal it nor aim it elsewhere.
                *(("-A",) if cfg.forward_agent else ("-o", "ForwardAgent=no")),
                "-R",
                f"127.0.0.1:{port}:{cfg.approval_host}:{cfg.approval_port}",
                *(
                    (
                        "-R",
                        f"127.0.0.1:{mcp_port}:{cfg.mcp_host}:{cfg.mcp_port}",
                    )
                    if mcp_port is not None
                    else ()
                ),
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
                    # Per run, not per process: |auth changes this and the next
                    # run picks it up without a restart.
                    "policy": policy or cfg.agent_policy,
                    # Standing rules and the bot's own Slack identity, for
                    # --append-system-prompt. In the system prompt rather than the
                    # turn so it survives context compaction in a long thread.
                    "system_append": system_append,
                    # The MCP servers this caller may use, already resolved on the
                    # host. Every one of them points at the relay: the guest gets a
                    # port and a run token, never a credential.
                    "mcp_port": mcp_port,
                    "mcp_config": _mcp_config(mcp_servers, mcp_port, mcp_strict),
                    # Strict means the guest's own ~/.claude.json MCP servers are
                    # ignored entirely. Set as soon as the host has any server
                    # configured, INCLUDING for a caller who may use none of them —
                    # otherwise "no servers for you" would silently fall back to
                    # whatever the VM has lying around.
                    "mcp_strict": mcp_strict,
                }
            ).encode()

            log.info(
                "starting run session=%s resume=%s tunnel_port=%s policy=%s",
                session_id,
                resume,
                port,
                policy or cfg.agent_policy,
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
            if mcp_port is not None:
                await self._mcp_ports.release(mcp_port)

    async def probe(self) -> RunResult:
        """Check the SSH path without starting a Claude run.

        The forced command rejects an empty job with exit 64, which is a positive
        signal: SSH authenticated and agent-exec ran.
        """
        cfg = self._config
        process = await asyncio.create_subprocess_exec(
            "ssh",
            "-T",
            *_NO_SSH_CONFIG,
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


def _mcp_config(
    servers: tuple[str, ...], mcp_port: int | None, strict: bool = False
) -> str:
    """The --mcp-config document for one run, or "" for no MCP at all.

    Every entry is the same relay with a different argument, so tool names stay
    `mcp__<server>__<tool>` — Claude Code takes the prefix from the config key, and the
    grants already in the database are written against those names.

    An empty document is still worth sending when strict is set: paired with
    --strict-mcp-config it is what says "this caller gets no MCP servers", rather than
    leaving the CLI to fall back on the guest's own configuration.
    """
    if mcp_port is None:
        return json.dumps({"mcpServers": {}}) if strict else ""
    return json.dumps(
        {
            "mcpServers": {
                name: {
                    "type": "stdio",
                    "command": MCP_RELAY,
                    "args": [name],
                    "env": {"AGENT_MCP_PORT": str(mcp_port)},
                }
                for name in servers
            }
        }
    )


async def _drain(stream: asyncio.StreamReader, into: list[bytes]) -> None:
    while chunk := await stream.read(8192):
        into.append(chunk)
