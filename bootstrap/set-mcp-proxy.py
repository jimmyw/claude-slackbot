#!/usr/bin/env python3
"""Route one MCP upstream through an authenticated https proxy, and store its credentials.

    ./set-mcp-proxy.py <server> --proxy https://proxy.example:8443 \\
                       --ca ~/proxy-root.crt --user alice

The credentials come from a hidden prompt, or from an env file (`--env-file`, plain
KEY=VALUE lines: it is parsed, never sourced, because sourcing would run whatever is in
it). Either way the password never appears in shell history, in `ps`, or in a chat, and
is never printed. Before writing anything it makes a real CONNECT to the
server's own host through the proxy, with the CA and password you gave it, and refuses to
save a combination that does not work. Then it updates the server's entry in mcp.json
(0600, timestamped backup) and copies the CA next to it.

The CA is trusted for the connection to the PROXY only, never for the upstream behind it
(see `_ProxyTrustConnector` in mcpproxy.py for why that matters). The daemon only needs a
restart to learn the new keys.
"""
from __future__ import annotations

import argparse
import base64
import getpass
import json
import os
import shutil
import socket
import ssl
import sys
import time
import urllib.parse
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".config" / "slack-claude" / "mcp.json"


def read_env_file(path: Path) -> dict[str, str]:
    """KEY=VALUE lines, optionally prefixed `export `, optionally quoted. Never executed."""
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    return values


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


NO_TUNNEL = 599


def _tunnel_is_real(tls: ssl.SSLSocket, host: str) -> bool:
    """TLS handshake with `host` THROUGH the open CONNECT tunnel, verified normally.

    A proxy that hides itself from callers it does not trust (probe resistance) answers
    a wrong password with the same `200` a real tunnel gets, and then serves nothing. A
    status code alone therefore cannot prove the credentials work; completing a handshake
    with the real target can, because only a real tunnel has a server on the other end.
    """
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    inner = ssl.create_default_context().wrap_bio(
        incoming, outgoing, server_hostname=host)
    tls.settimeout(10)
    try:
        while True:
            try:
                inner.do_handshake()
            except ssl.SSLWantReadError:
                pending = outgoing.read()
                if pending:
                    tls.sendall(pending)
                chunk = tls.recv(65536)
                if not chunk:
                    return False
                incoming.write(chunk)
                continue
            pending = outgoing.read()
            if pending:
                tls.sendall(pending)
            return True
    except (ssl.SSLError, OSError):
        return False


def check_connect(proxy: str, ca: Path | None, user: str, password: str,
                  target_host: str) -> int:
    """Status of `CONNECT <target_host>:443` through the proxy, verified against `ca`.

    200 means a REAL tunnel (see `_tunnel_is_real`); NO_TUNNEL means the proxy said 200
    but nothing was behind it, which is how a probe-resistant proxy rejects a wrong
    password. Raises ssl.SSLError / OSError when the proxy itself cannot be reached or
    trusted.
    """
    parts = urllib.parse.urlsplit(proxy)
    context = ssl.create_default_context(cafile=str(ca)) if ca else ssl.create_default_context()
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    with socket.create_connection((parts.hostname, parts.port or 443), timeout=10) as raw:
        with context.wrap_socket(raw, server_hostname=parts.hostname) as tls:
            tls.sendall((f"CONNECT {target_host}:443 HTTP/1.1\r\nHost: {target_host}:443\r\n"
                         f"Proxy-Authorization: Basic {token}\r\n\r\n").encode())
            status_line = tls.recv(256).split(b"\r\n", 1)[0].decode(errors="replace")
            try:
                status = int(status_line.split(" ")[1])
            except (IndexError, ValueError):
                die(f"the proxy answered something that is not HTTP: {status_line[:60]!r}")
            if status == 200 and not _tunnel_is_real(tls, target_host):
                return NO_TUNNEL
            return status


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("server", help="the server name as it appears in mcp.json")
    parser.add_argument("--proxy", required=True, help="https://host:port of the proxy")
    parser.add_argument("--ca", type=Path, help="PEM CA that signed the proxy's certificate")
    parser.add_argument("--user", help="proxy username (or take it from --env-file)")
    parser.add_argument("--env-file", type=Path,
                        help="read credentials from KEY=VALUE lines in this file")
    parser.add_argument("--user-var", default="PROXY_USER")
    parser.add_argument("--password-var", default="PROXY_PASS")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--force", action="store_true",
                        help="save even if the live check fails")
    args = parser.parse_args()

    if not args.proxy.startswith(("http://", "https://")):
        die("--proxy must be an http:// or https:// URL")
    if args.ca and not args.proxy.startswith("https://"):
        die("--ca only makes sense with an https:// proxy")
    if args.ca and not args.ca.is_file():
        die(f"no such CA file: {args.ca}")

    document = json.loads(args.config.read_text())
    entry = document.get("servers", {}).get(args.server)
    if not entry or entry.get("type") != "http":
        die(f"{args.server!r} is not an http server in {args.config}")
    target_host = urllib.parse.urlsplit(entry["url"]).hostname

    user, password = args.user or "", ""
    if args.env_file:
        if not args.env_file.is_file():
            die(f"no such env file: {args.env_file}")
        if args.env_file.stat().st_mode & 0o077:
            print(f"warning: {args.env_file} is readable by other users; chmod 600 it",
                  file=sys.stderr)
        values = read_env_file(args.env_file)
        user = user or values.get(args.user_var, "")
        password = values.get(args.password_var, "")
        if not password:
            die(f"{args.password_var} is not set in {args.env_file}")
    if not user:
        die("no username: pass --user, or --env-file with " + args.user_var)
    if not password:
        password = getpass.getpass(f"Proxy password for {user} (not echoed): ")
    if not password:
        die("empty password")

    print(f"checking: CONNECT {target_host}:443 via {args.proxy} ...")
    try:
        status = check_connect(args.proxy, args.ca, user, password, target_host)
    except (ssl.SSLError, OSError) as exc:
        status = 0
        print(f"  could not reach or trust the proxy: {exc}", file=sys.stderr)
    verdict = {200: "ok -- the proxy accepted the credentials and opened a working tunnel",
               407: "REFUSED -- the proxy rejected the username/password",
               NO_TUNNEL: "REFUSED -- the proxy said 200 but no real tunnel followed; a proxy "
                          "that hides itself does this for a wrong password",
               0: "FAILED -- see above"}.get(status, f"unexpected status {status}")
    print(f"  {verdict}")
    if status != 200 and not args.force:
        die("nothing was written (use --force to save it anyway)")

    entry["proxy"] = args.proxy
    entry.pop("proxy_ca", None)
    if args.ca:
        stored = args.config.with_name(f"{args.server}-proxy-ca.crt")
        shutil.copyfile(args.ca, stored)
        os.chmod(stored, 0o600)
        entry["proxy_ca"] = str(stored)
    entry["proxy_auth"] = {"username": user, "password": password}

    backup = args.config.with_name(f"{args.config.name}.{time.strftime('%Y%m%d%H%M%S')}.bak")
    shutil.copy2(args.config, backup)
    tmp = args.config.with_name(args.config.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, args.config)
    print(f"wrote {args.config} (0600), backup {backup.name}")
    print("restart the daemon so it learns the new keys:  "
          "systemctl --user restart slack-claude-daemon")


if __name__ == "__main__":
    main()
