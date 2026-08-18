"""The MCP proxy: the guest's only route to an MCP server, and the host's chokepoint.

The guest runs `mcp-relay`, a ~40-line stdio↔TCP relay, as its MCP server. The relay
reaches this proxy over a reverse-forwarded loopback port on the same ssh invocation
that carries the run, exactly as the approval hook reaches the approval listener. So:

  * **Credentials terminate here.** The VM never holds a header, an env token or an
    OAuth grant, and there is nothing in it to steal.
  * **Identity comes from the run token, not from the guest.** The token maps to the
    run, and the run knows which Slack user sent the message. The guest never states
    who it is, so it cannot claim to be someone else.
  * **Policy is enforced here.** A tool the caller may not use is filtered out of
    `tools/list` so it is never even visible, and a call to it never leaves the host.
  * **The audit trail is written here**, naming a person, out of the agent's reach.

MCP's stdio framing is newline-delimited JSON-RPC, which is why the guest side can be a
relay rather than an implementation: this module passes messages through verbatim and
intercepts exactly two things — `tools/list` responses, which it filters, and
`tools/call` requests, which it decides on. Everything else about the protocol stays
between the CLI and the upstream.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import aiohttp

from . import mcpconfig
from .mcpconfig import Registry, Server
from .store import Store

log = logging.getLogger(__name__)

PROTOCOL = 1

# The handshake is one short line. A client that sends neither promptly nor briefly is
# not our relay.
HANDSHAKE_TIMEOUT_S = 10.0
HANDSHAKE_LIMIT = 4096
LINE_LIMIT = 16 * 1024 * 1024

# Namespaces the proxy refuses outright. None of the servers this was built for uses
# them, and default-deny is the right posture for a capability nobody has asked for:
# `resources/read` in particular is an arbitrary-read primitive.
REFUSED_PREFIXES = ("resources/", "prompts/", "completion/")


@dataclass
class RunContext:
    """Who is asking, resolved on the host from the run token."""

    slack_user: str = ""
    channel_id: str = ""
    thread_ts: str = ""
    session_id: str = ""


@dataclass
class _Pending:
    """A request we are waiting for the upstream to answer."""

    method: str
    tool: str = ""
    started: float = field(default_factory=time.monotonic)


def _digest(arguments: Any) -> str:
    """A short digest of a call's arguments for the audit trail.

    The arguments themselves are not stored: they can be large, and they can carry
    whatever the agent put in them. A digest is enough to tell two calls apart.
    """
    try:
        raw = json.dumps(arguments, sort_keys=True, default=str)
    except (TypeError, ValueError):
        raw = repr(arguments)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]


def tool_error(request_id: Any, text: str) -> dict:
    """A refusal the model can read.

    Deliberately a tool *result* with isError, not a JSON-RPC error: a protocol error
    reads to the CLI as a broken server, and the agent would report "the MCP server
    failed" instead of "I was not allowed to do that".
    """
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "result": {"isError": True, "content": [{"type": "text", "text": text}]},
    }


def protocol_error(request_id: Any, message: str, code: int = -32601) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


# -- upstreams ---------------------------------------------------------------


class Upstream:
    """One MCP server as seen from the host."""

    async def start(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def send(self, message: dict) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    async def receive(self) -> dict | None:  # pragma: no cover - interface
        """The next message from the server, or None when it is finished."""
        raise NotImplementedError

    async def close(self) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class StdioUpstream(Upstream):
    """A local MCP server run as a subprocess, with its credentials in the env.

    The credential is passed to a process on the HOST, so the agent can neither read it
    nor rewrite the server that holds it — which it could when this ran in the VM from a
    script inside its own workspace.
    """

    def __init__(self, server: Server, env: dict[str, str]) -> None:
        self._server = server
        self._env = env
        self._process: asyncio.subprocess.Process | None = None

    async def start(self) -> None:
        self._process = await asyncio.create_subprocess_exec(
            self._server.command,
            *self._server.args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env,
            limit=LINE_LIMIT,
        )
        asyncio.create_task(self._drain_stderr())

    async def _drain_stderr(self) -> None:
        """An MCP server's stderr is its log; keep it in ours rather than a pipe that
        fills up and blocks the server mid-session."""
        assert self._process is not None and self._process.stderr is not None
        while chunk := await self._process.stderr.readline():
            log.debug("mcp[%s] stderr: %s", self._server.name,
                      chunk.decode(errors="replace").rstrip())

    async def send(self, message: dict) -> None:
        assert self._process is not None and self._process.stdin is not None
        self._process.stdin.write(json.dumps(message).encode() + b"\n")
        await self._process.stdin.drain()

    async def receive(self) -> dict | None:
        assert self._process is not None and self._process.stdout is not None
        while True:
            line = await self._process.stdout.readline()
            if not line:
                return None
            text = line.strip()
            if not text:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                # Some servers print banners to stdout. The CLI would choke on them,
                # so they stop here rather than being relayed.
                log.warning(
                    "mcp[%s] dropped a non-JSON stdout line: %s",
                    self._server.name, text[:200],
                )

    async def close(self) -> None:
        if self._process is None:
            return
        if self._process.returncode is None:
            self._process.kill()
            with contextlib.suppress(ProcessLookupError):
                await self._process.wait()


class OAuth:
    """Access tokens for an OAuth-protected MCP upstream, refreshed on the host.

    This is the credential the VM must never hold. The guest's copy of the esp-crash
    grant included a REFRESH token: long-lived and re-mintable, so stealing it buys
    access that outlives the run, the VM and any policy. Here it lives in the daemon's
    sqlite, per (server, owner), and the guest sees only tool results.

    The rotation is the part that is easy to get wrong. Authorisation servers commonly
    issue a NEW refresh token with each refresh and invalidate the old one, so a grant
    kept in the config file would be dead after the first refresh — silently, and in a
    way that looks like the upstream is broken. The store is therefore the authority
    once seeded, and the config file only bootstraps.
    """

    # Refresh a little early: a token that expires between the check and the request
    # would otherwise cost a 401 and a retry on every call at the boundary.
    SKEW_S = 60

    def __init__(
        self,
        store: Store,
        server_name: str,
        credential: mcpconfig.Credential,
        *,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        self._store = store
        self._server = server_name
        self._owner = credential.owner
        self._config = dict(credential.oauth or {})
        self._session = session
        self._lock = asyncio.Lock()

    @property
    def configured(self) -> bool:
        return bool(self._config.get("token_url"))

    async def header(self, force: bool = False) -> dict[str, str]:
        token = await self._access_token(force)
        return {"Authorization": f"Bearer {token}"} if token else {}

    async def _access_token(self, force: bool) -> str:
        async with self._lock:
            row = self._store.mcp_token(self._server, self._owner)
            access = (row["access_token"] if row else "") or self._config.get(
                "access_token", ""
            )
            expires_at = int(
                (row["expires_at"] if row else 0) or self._config.get("expires_at") or 0
            )
            fresh_enough = access and (
                not expires_at or expires_at - self.SKEW_S > time.time()
            )
            if fresh_enough and not force:
                return access
            return await self._refresh(row)

    async def _refresh(self, row: Any) -> str:
        refresh_token = (
            (row["refresh_token"] if row else "") or self._config.get("refresh_token")
        )
        if not refresh_token:
            raise RuntimeError(
                f"no refresh token for {self._server}; re-authorise it on the host"
            )

        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": self._config.get("client_id", ""),
        }
        if self._config.get("client_secret"):
            payload["client_secret"] = self._config["client_secret"]
        if self._config.get("scope"):
            payload["scope"] = self._config["scope"]

        session = self._session or aiohttp.ClientSession()
        try:
            async with session.post(
                self._config["token_url"], data=payload
            ) as response:
                body = await response.json(content_type=None)
                if response.status >= 400:
                    raise RuntimeError(
                        f"refreshing {self._server} failed with HTTP "
                        f"{response.status}: {str(body)[:200]}"
                    )
        finally:
            if self._session is None:
                await session.close()

        access = body.get("access_token") or ""
        if not access:
            raise RuntimeError(f"refreshing {self._server} returned no access_token")
        expires_in = int(body.get("expires_in") or 0)
        self._store.save_mcp_token(
            self._server,
            self._owner,
            access_token=access,
            # Keep the old one when the server does not rotate; store the new one
            # when it does. Dropping a rotated token is what bricks the grant.
            refresh_token=body.get("refresh_token") or refresh_token,
            expires_at=int(time.time()) + expires_in if expires_in else 0,
        )
        log.info(
            "refreshed the %s oauth token for %s",
            self._server, self._owner or "everyone",
        )
        return access


class HttpUpstream(Upstream):
    """A streamable-HTTP MCP server, with the credential injected here.

    Deliberately does NOT open the optional server→client GET stream. For tool servers,
    progress and log notifications arrive on the POST's own SSE body, which is handled;
    unsolicited server-initiated requests (sampling, roots, elicitation) are not
    supported and would need that stream. Stated here rather than discovered later.
    """

    def __init__(
        self,
        server: Server,
        headers: dict[str, str],
        *,
        session: aiohttp.ClientSession | None = None,
        auth: OAuth | None = None,
    ) -> None:
        self._server = server
        self._headers = dict(headers)
        self._session = session
        self._owns_session = session is None
        self._auth = auth
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._mcp_session_id: str | None = None
        self._protocol_version: str | None = None

    async def start(self) -> None:
        if self._session is None:
            self._session = aiohttp.ClientSession()

    async def _request_headers(self, force_auth: bool = False) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            **self._headers,
        }
        if self._mcp_session_id:
            headers["Mcp-Session-Id"] = self._mcp_session_id
        if self._protocol_version:
            headers["MCP-Protocol-Version"] = self._protocol_version
        if self._auth is not None:
            headers.update(await self._auth.header(force=force_auth))
        return headers

    async def send(self, message: dict) -> None:
        assert self._session is not None
        try:
            await self._post(message)
        except Exception as exc:  # noqa: BLE001
            log.warning("mcp[%s] upstream POST failed: %s", self._server.name, exc)
            request_id = message.get("id")
            if request_id is not None:
                await self._queue.put(
                    tool_error(request_id, f"the upstream MCP server failed: {exc}")
                )

    async def _post(self, message: dict, *, retry_auth: bool = True) -> None:
        assert self._session is not None
        async with self._session.post(
            self._server.url,
            json=message,
            headers=await self._request_headers(force_auth=not retry_auth),
        ) as response:
            if response.status == 401 and retry_auth and self._auth is not None:
                # One retry: an access token can expire mid-session, and the provider
                # refreshes on demand.
                log.info("mcp[%s] 401, refreshing and retrying once",
                         self._server.name)
                await response.release()
                return await self._post(message, retry_auth=False)

            if message.get("method") == "initialize":
                self._mcp_session_id = response.headers.get("Mcp-Session-Id")

            if response.status == 202:
                # A notification. Nothing comes back, and nothing should be relayed.
                return
            response.raise_for_status()

            content_type = (response.headers.get("Content-Type") or "").lower()
            if content_type.startswith("text/event-stream"):
                await self._read_sse(response)
                return
            body = await response.json()
            for item in body if isinstance(body, list) else [body]:
                self._note_protocol(item)
                await self._queue.put(item)

    async def _read_sse(self, response: aiohttp.ClientResponse) -> None:
        """Minimal SSE: accumulate `data:` lines until a blank line, parse, queue."""
        data: list[str] = []
        async for raw in response.content:
            line = raw.decode(errors="replace").rstrip("\r\n")
            if not line:
                if data:
                    payload = "\n".join(data)
                    data = []
                    try:
                        message = json.loads(payload)
                    except json.JSONDecodeError:
                        log.warning("mcp[%s] unparseable SSE payload",
                                    self._server.name)
                        continue
                    self._note_protocol(message)
                    await self._queue.put(message)
                continue
            if line.startswith(":"):
                continue
            field, _, value = line.partition(":")
            if field == "data":
                data.append(value.lstrip())

    def _note_protocol(self, message: dict) -> None:
        version = ((message.get("result") or {}) if isinstance(message, dict) else {})
        if isinstance(version, dict) and version.get("protocolVersion"):
            self._protocol_version = version["protocolVersion"]

    async def receive(self) -> dict | None:
        return await self._queue.get()

    async def close(self) -> None:
        await self._queue.put(None)
        if self._session is not None and self._owns_session:
            await self._session.close()


# -- the proxy ---------------------------------------------------------------


class McpProxy:
    """A loopback TCP listener that fronts every MCP server for the guest."""

    def __init__(
        self,
        config: Any,
        store: Store,
        registry: Registry,
        resolve_run: Callable[[str], RunContext | None],
        *,
        upstream_factory: Callable[[Server, mcpconfig.Credential], Upstream] | None = None,
    ) -> None:
        self._config = config
        self._store = store
        self._registry = registry
        self._resolve_run = resolve_run
        self._upstream_factory = upstream_factory or self._default_upstream
        self._server: asyncio.Server | None = None

    # -- lifecycle ------------------------------------------------------------

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle, self._config.mcp_host, self._config.mcp_port,
            limit=LINE_LIMIT,
        )
        log.info(
            "mcp proxy on %s:%s (%d server(s) configured)",
            self._config.mcp_host, self._config.mcp_port,
            len(self._registry.servers()),
        )

    @property
    def port(self) -> int:
        """The bound port. With mcp_port 0 the OS picks one, which is how a test gets
        a free port without guessing."""
        if self._server is None or not self._server.sockets:
            return self._config.mcp_port
        return int(self._server.sockets[0].getsockname()[1])

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # -- per connection -------------------------------------------------------

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            hello = await self._handshake(reader, writer)
            if hello is None:
                return
            server, credential, run = hello
            upstream = self._upstream_factory(server, credential)
            try:
                await upstream.start()
                await _Session(
                    server=server,
                    run=run,
                    store=self._store,
                    registry=self._registry,
                    reader=reader,
                    writer=writer,
                    upstream=upstream,
                ).pump()
            finally:
                await upstream.close()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("mcp proxy connection failed")
        finally:
            with contextlib.suppress(Exception):
                writer.close()
                await writer.wait_closed()

    async def _handshake(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> tuple[Server, mcpconfig.Credential, RunContext] | None:
        """Authenticate one relay, or refuse it.

        Refusals are written as a plain object rather than JSON-RPC: nothing has been
        requested yet, so there is no id to answer. The relay prints it to stderr and
        exits non-zero, which the CLI reports as a server that failed to start.
        """
        async def refuse(reason: str) -> None:
            log.warning("mcp proxy refused a connection: %s", reason)
            with contextlib.suppress(Exception):
                writer.write(json.dumps({"error": reason}).encode() + b"\n")
                await writer.drain()

        try:
            line = await asyncio.wait_for(
                reader.readline(), timeout=HANDSHAKE_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            await refuse("no handshake")
            return None
        if not line or len(line) > HANDSHAKE_LIMIT:
            await refuse("bad handshake")
            return None
        try:
            hello = json.loads(line)
        except json.JSONDecodeError:
            await refuse("unparseable handshake")
            return None

        if hello.get("protocol") != PROTOCOL:
            await refuse(f"unsupported protocol {hello.get('protocol')!r}")
            return None

        # The token is the identity. An unknown one is a stale relay from a finished
        # run, or something else on this host's loopback — same reasoning as the
        # approval listener, which 403s and posts nothing.
        run = self._resolve_run(hello.get("run_token") or "")
        if run is None:
            await refuse("unknown run token")
            return None

        name = hello.get("server") or ""
        available = mcpconfig.available_for(
            self._registry, run.slack_user, self._store.mcp_disabled()
        )
        server = available.get(name)
        if server is None:
            # Either not configured, disabled, or this person has no credential for
            # it. The guest is told the same thing in each case: nothing about the
            # servers it may not use.
            await refuse(f"no MCP server {name!r} available for this run")
            return None

        credential = server.credential_for(run.slack_user)
        if credential is None:  # pragma: no cover - available_for already checked
            await refuse(f"no credential for {name!r}")
            return None

        log.info(
            "mcp proxy: %s attached to %r (thread=%s)",
            run.slack_user or "unknown user", name, run.thread_ts,
        )
        return server, credential, run

    def _default_upstream(
        self, server: Server, credential: mcpconfig.Credential
    ) -> Upstream:
        if server.type == mcpconfig.STDIO:
            # A deliberately minimal environment: the upstream gets what the config
            # gives it plus PATH, not the daemon's whole environment (which holds the
            # Slack tokens).
            env = {"PATH": "/usr/local/bin:/usr/bin:/bin", **credential.env}
            return StdioUpstream(server, env)
        auth = OAuth(self._store, server.name, credential)
        return HttpUpstream(
            server, credential.headers, auth=auth if auth.configured else None
        )


class _Session:
    """One guest connection to one upstream, with the policy in the middle."""

    def __init__(
        self,
        *,
        server: Server,
        run: RunContext,
        store: Store,
        registry: Registry,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        upstream: Upstream,
    ) -> None:
        self._server = server
        self._run = run
        self._store = store
        self._registry = registry
        self._reader = reader
        self._writer = writer
        self._upstream = upstream
        self._pending: dict[str, _Pending] = {}
        self._calls = 0
        self._extra_allow, self._extra_deny = self._runtime_patterns()

    def _runtime_patterns(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Allow/deny patterns added at runtime with |mcp, for this caller."""
        allow: list[str] = []
        deny: list[str] = []
        for row in self._store.mcp_policy(self._server.name):
            if row["slack_user"] not in ("", self._run.slack_user):
                continue
            (allow if row["effect"] == "allow" else deny).append(row["pattern"])
        return tuple(allow), tuple(deny)

    # -- pumping --------------------------------------------------------------

    async def pump(self) -> None:
        to_upstream = asyncio.create_task(self._guest_to_upstream())
        to_guest = asyncio.create_task(self._upstream_to_guest())
        done, pending = await asyncio.wait(
            {to_upstream, to_guest}, return_when=asyncio.FIRST_COMPLETED
        )
        for task in pending:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        for task in done:
            with contextlib.suppress(asyncio.CancelledError):
                task.result()

    async def _send_guest(self, message: dict) -> None:
        self._writer.write(json.dumps(message).encode() + b"\n")
        await self._writer.drain()

    async def _guest_to_upstream(self) -> None:
        while line := await self._reader.readline():
            text = line.strip()
            if not text:
                continue
            try:
                message = json.loads(text)
            except json.JSONDecodeError:
                log.warning("mcp[%s] dropped unparseable guest line", self._server.name)
                continue
            if not await self._intercept(message):
                await self._upstream.send(message)

    async def _upstream_to_guest(self) -> None:
        while True:
            message = await self._upstream.receive()
            if message is None:
                return
            await self._send_guest(self._filter_response(message))

    # -- the two interceptions ------------------------------------------------

    async def _intercept(self, message: dict) -> bool:
        """True when the proxy answered instead of forwarding."""
        method = message.get("method")
        request_id = message.get("id")

        if method is None:
            return False  # a response or a notification from the client: pass on

        if any(method.startswith(prefix) for prefix in REFUSED_PREFIXES):
            if request_id is not None:
                await self._send_guest(
                    protocol_error(
                        request_id,
                        f"{method} is not available through this proxy",
                    )
                )
            return True

        if method == "tools/list" and request_id is not None:
            self._pending[str(request_id)] = _Pending(method)
            return False

        if method != "tools/call":
            return False

        params = message.get("params") or {}
        tool = params.get("name") or ""
        decision = mcpconfig.decide(
            self._server, self._run.slack_user, tool,
            extra_allow=self._extra_allow, extra_deny=self._extra_deny,
        )
        digest = _digest(params.get("arguments"))

        if not decision.allowed:
            self._audit(tool, "denied", decision.reason, digest)
            await self._send_guest(
                tool_error(
                    request_id,
                    f"Blocked by host policy: {self._server.name}.{tool} "
                    f"({decision.reason}). Ask the operator to allow it with "
                    f"`|mcp allow {self._server.name} {tool}` if it is needed.",
                )
            )
            return True

        self._calls += 1
        if self._calls > self._server.max_calls_per_run:
            self._audit(tool, "capped", "per-run call cap", digest)
            await self._send_guest(
                tool_error(
                    request_id,
                    f"Blocked: this message has already made "
                    f"{self._server.max_calls_per_run} calls to "
                    f"{self._server.name}, which is the per-run cap. Ask again in a "
                    "new message if that was genuinely needed.",
                )
            )
            return True

        if request_id is not None:
            self._pending[str(request_id)] = _Pending(method, tool=tool)
        return False

    def _filter_response(self, message: dict) -> dict:
        request_id = message.get("id")
        if request_id is None:
            return message
        pending = self._pending.pop(str(request_id), None)
        if pending is None:
            return message

        if pending.method == "tools/list":
            return self._filter_tools(message)

        # A completed tools/call: audit it, and enforce the size cap.
        elapsed = int((time.monotonic() - pending.started) * 1000)
        blob = json.dumps(message.get("result") or message.get("error") or {})
        if len(blob) > self._server.max_result_bytes:
            self._audit(
                pending.tool, "capped", "result larger than the cap",
                "", len(blob), elapsed,
            )
            return tool_error(
                request_id,
                f"The result was {len(blob)} bytes, over the "
                f"{self._server.max_result_bytes}-byte cap for "
                f"{self._server.name}, so it was not passed on. Narrow the query and "
                "try again.",
            )
        self._audit(pending.tool, "allowed", "", "", len(blob), elapsed)
        return message

    def _filter_tools(self, message: dict) -> dict:
        """Drop tools this caller may not call.

        Filtered rather than merely blocked at call time: a tool the agent cannot use
        should not be in its context at all — it stops the agent planning around it,
        and it keeps the capability list itself out of the VM. `nextCursor` is passed
        through untouched, so a filtered page does not break pagination.
        """
        result = message.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
            return message

        kept = []
        hidden = []
        for tool in result["tools"]:
            name = (tool or {}).get("name") or ""
            if mcpconfig.decide(
                self._server, self._run.slack_user, name,
                extra_allow=self._extra_allow, extra_deny=self._extra_deny,
            ).allowed:
                kept.append(tool)
            else:
                hidden.append(name)

        if hidden:
            log.info(
                "mcp[%s] hid %d tool(s) from %s: %s",
                self._server.name, len(hidden), self._run.slack_user or "?",
                ", ".join(sorted(hidden)),
            )
        filtered = dict(message)
        filtered["result"] = {**result, "tools": kept}
        return filtered

    def _audit(
        self,
        tool: str,
        decision: str,
        reason: str = "",
        digest: str = "",
        result_bytes: int = 0,
        duration_ms: int = 0,
    ) -> None:
        try:
            self._store.record_mcp_call(
                slack_user=self._run.slack_user,
                channel_id=self._run.channel_id,
                thread_ts=self._run.thread_ts,
                session_id=self._run.session_id,
                server=self._server.name,
                tool=tool,
                decision=decision,
                reason=reason,
                args_digest=digest,
                result_bytes=result_bytes,
                duration_ms=duration_ms,
            )
        except Exception:
            # The audit trail must never take a call down with it.
            log.exception("could not record an mcp call")
