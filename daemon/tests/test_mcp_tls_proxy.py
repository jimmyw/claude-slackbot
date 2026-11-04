"""An MCP upstream reached through an authenticated https proxy with a PRIVATE CA.

Everything here runs for real over loopback: throwaway certificates from `openssl`, a fake
proxy that demands Basic credentials and tunnels CONNECT, and targets over plain http and
https. The case that matters is the negative one. The proxy's CA must be trusted for the
hop to the proxy and for NOTHING else, otherwise whoever holds that CA's key could
impersonate the upstream through the tunnel and read the credential being protected.

Run:  .venv/bin/python -m tests.test_mcp_tls_proxy
"""
from __future__ import annotations

import asyncio
import base64
import shutil
import ssl
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp
from aiohttp import web

from slackagent.mcpconfig import Server
from slackagent.mcpproxy import HttpUpstream, _ProxyTrustConnector

failures: list[str] = []
USER, PASSWORD = "proxyuser", "s3cret-pw"


def check(label: str, condition: bool, detail: object = "") -> None:
    if condition:
        print(f"  PASS  {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def make_ca_and_leaf(directory: Path, name: str) -> tuple[Path, Path, Path]:
    """A CA, and a 127.0.0.1 leaf signed by it. Returns (ca.crt, leaf.crt, leaf.key)."""
    def run(*args: str) -> None:
        subprocess.run(["openssl", *args], cwd=directory, check=True, capture_output=True)

    run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
        "-nodes", "-keyout", f"{name}-ca.key", "-out", f"{name}-ca.crt",
        "-subj", f"/CN={name} test CA", "-days", "2",
        "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
        "-keyout", f"{name}-leaf.key", "-out", f"{name}-leaf.csr", "-subj", "/CN=127.0.0.1")
    (directory / f"{name}-ext.cnf").write_text(
        "subjectAltName=IP:127.0.0.1\nbasicConstraints=CA:FALSE\n"
        "keyUsage=digitalSignature\nextendedKeyUsage=serverAuth\n")
    run("x509", "-req", "-in", f"{name}-leaf.csr", "-CA", f"{name}-ca.crt",
        "-CAkey", f"{name}-ca.key", "-CAcreateserial", "-out", f"{name}-leaf.crt",
        "-days", "2", "-extfile", f"{name}-ext.cnf")
    return (directory / f"{name}-ca.crt", directory / f"{name}-leaf.crt",
            directory / f"{name}-leaf.key")


def server_context(crt: Path, key: Path) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(crt, key)
    return context


class FakeTlsProxy:
    """A forward proxy that speaks TLS, wants Basic credentials, and records what it saw."""

    def __init__(self, context: ssl.SSLContext) -> None:
        self.context = context
        self.seen: list[tuple[str, str]] = []
        self.port = 0
        self._server: asyncio.AbstractServer | None = None

    async def __aenter__(self) -> "FakeTlsProxy":
        self._server = await asyncio.start_server(
            self._handle, "127.0.0.1", 0, ssl=self.context)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *_exc) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader,
                      writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            lines = head.decode().split("\r\n")
            method, target, _ = lines[0].split(" ", 2)
            headers = {k.lower(): v for k, v in
                       (line.split(": ", 1) for line in lines[1:] if ": " in line)}
            expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
            self.seen.append((method, target))
            if headers.get("proxy-authorization") != expected:
                writer.write(b"HTTP/1.1 407 Proxy Authentication Required\r\n"
                             b"Proxy-Authenticate: Basic realm=test\r\n"
                             b"Content-Length: 0\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return
            if method == "CONNECT":
                host, port = target.rsplit(":", 1)
                up_reader, up_writer = await asyncio.open_connection(host, int(port))
                writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                await writer.drain()
                await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
                return
            # Absolute-URI request to a plain-http origin: forward in origin form.
            from urllib.parse import urlsplit
            url = urlsplit(target)
            body = await reader.readexactly(int(headers.get("content-length", "0")))
            up_reader, up_writer = await asyncio.open_connection(url.hostname, url.port)
            kept = [f"{k}: {v}" for k, v in headers.items()
                    if k not in ("proxy-authorization", "proxy-connection", "connection")]
            up_writer.write((f"{method} {url.path or '/'} HTTP/1.1\r\n"
                             + "\r\n".join(kept) + "\r\nConnection: close\r\n\r\n").encode()
                            + body)
            await up_writer.drain()
            writer.write(await up_reader.read())
            await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except ConnectionError:
        pass
    finally:
        writer.close()


class FakeUpstream:
    """An MCP endpoint that counts the requests that actually reach it."""

    def __init__(self, context: ssl.SSLContext | None = None) -> None:
        self.context = context
        self.hits = 0
        self.port = 0
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"{'https' if self.context else 'http'}://127.0.0.1:{self.port}/mcp"

    async def __aenter__(self) -> "FakeUpstream":
        async def mcp(request: web.Request) -> web.Response:
            self.hits += 1
            body = await request.json()
            return web.json_response(
                {"jsonrpc": "2.0", "id": body.get("id"), "result": {"ok": True}})

        app = web.Application()
        app.router.add_post("/mcp", mcp)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0, ssl_context=self.context)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # noqa: SLF001
        return self

    async def __aexit__(self, *_exc) -> None:
        assert self._runner is not None
        await self._runner.cleanup()


async def ask(upstream: HttpUpstream) -> dict:
    await upstream.start()
    try:
        await upstream.send({"jsonrpc": "2.0", "id": 1, "method": "initialize"})
        return await asyncio.wait_for(upstream.receive(), 20)
    finally:
        await upstream.close()


def mcp_server(url: str, proxy_port: int, ca: Path | None, *, password: str = PASSWORD,
               user: str = USER) -> Server:
    return Server(name="t", type="http", url=url, proxy=f"https://127.0.0.1:{proxy_port}",
                  proxy_ca=str(ca) if ca else "", proxy_user=user,
                  proxy_password=password)


async def main() -> int:
    if not shutil.which("openssl"):
        print("  SKIP  openssl is not installed, so no certificates can be made")
        return 0
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        proxy_ca, proxy_crt, proxy_key = make_ca_and_leaf(tmp, "proxy")
        site_ca, site_crt, site_key = make_ca_and_leaf(tmp, "site")

        print("[1] a plain-http upstream through the https proxy")
        async with FakeTlsProxy(server_context(proxy_crt, proxy_key)) as proxy, \
                FakeUpstream() as origin:
            reply = await ask(HttpUpstream(mcp_server(origin.url, proxy.port, proxy_ca), {}))
            check("with the proxy's CA and the right credentials, the call succeeds",
                  reply.get("result") == {"ok": True}, reply)
            check("it really went through the proxy", len(proxy.seen) == 1, proxy.seen)

            before = origin.hits
            reply = await ask(HttpUpstream(
                mcp_server(origin.url, proxy.port, proxy_ca, password="wrong"), {}))
            check("a wrong password is a refused call, not a hang", "error" in reply, reply)
            check("and nothing reached the upstream", origin.hits == before)

            before = origin.hits
            reply = await ask(HttpUpstream(mcp_server(origin.url, proxy.port, None), {}))
            check("without the proxy's CA the proxy is NOT trusted by default",
                  "error" in reply and origin.hits == before, reply)

        print("\n[2] an https upstream tunnelled through it (TLS inside TLS)")
        async with FakeTlsProxy(server_context(proxy_crt, proxy_key)) as proxy, \
                FakeUpstream(server_context(site_crt, site_key)) as origin:
            target_trust = ssl.create_default_context(cafile=str(site_ca))
            session = aiohttp.ClientSession(connector=_ProxyTrustConnector(
                "127.0.0.1", proxy.port, ssl.create_default_context(cafile=str(proxy_ca)),
                ssl=target_trust))
            reply = await ask(HttpUpstream(
                mcp_server(origin.url, proxy.port, proxy_ca), {}, session=session))
            await session.close()
            check("each hop is verified against its own CA, and the call succeeds",
                  reply.get("result") == {"ok": True}, reply)
            check("the proxy saw a CONNECT, not the request",
                  [m for m, _ in proxy.seen] == ["CONNECT"], proxy.seen)

        print("\n[3] the proxy's CA is not trusted for the upstream behind it")
        async with FakeTlsProxy(server_context(proxy_crt, proxy_key)) as proxy, \
                FakeUpstream(server_context(proxy_crt, proxy_key)) as origin:
            # The upstream presents a certificate signed by the PROXY's CA: exactly what
            # a proxy impersonating the upstream would do.
            reply = await ask(HttpUpstream(mcp_server(origin.url, proxy.port, proxy_ca), {}))
            check("an upstream certificate from the proxy's CA is rejected",
                  "error" in reply and origin.hits == 0, (reply, origin.hits))

            # Control: the same setup does work if that CA is trusted everywhere, so the
            # rejection above is the scoping and not a broken fixture.
            everywhere = ssl.create_default_context(cafile=str(proxy_ca))
            session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=everywhere))
            reply = await ask(HttpUpstream(
                mcp_server(origin.url, proxy.port, proxy_ca), {}, session=session))
            await session.close()
            check("control: trusted everywhere, the same upstream is reachable",
                  reply.get("result") == {"ok": True}, reply)

    print()
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
