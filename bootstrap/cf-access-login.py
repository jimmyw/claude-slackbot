#!/usr/bin/env python3
"""Log in to a Cloudflare Access-protected MCP upstream from a headless host and store
the session cookie.

Why this exists
---------------
An MCP server behind Cloudflare Access has no dynamic client registration, so
`authorise-mcp-oauth.py` cannot create a grant for it. What Access does support is the
browser-less flow `cloudflared` uses: it prints a URL, you open it in ANY browser (your
laptop is fine) and finish the identity-provider login and MFA there, and the session
token is delivered back to the terminal that asked. That is why this runs on the
daemon's host, which has no display.

    cloudflared must be on PATH
    bootstrap/cf-access-login.py <server> [<server> ...]

For each server named in mcp.json it runs `cloudflared access login <url>` (you do the
browser step), reads the token back with `cloudflared access token`, and writes it to
`credential.headers.Cookie` as `CF_Authorization=<jwt>`. No password passes through this
script at all, and the token is never printed. The daemon re-reads the file, so no
restart is needed.

What you are accepting
----------------------
* The cookie is a bearer credential for YOUR identity at the identity provider, until the Access session
  expires (see the expiry printed). There is no refresh: re-run this when the server
  reports a logged-out upstream.
* It lives in mcp.json beside the other tokens, which the daemon already refuses to read
  unless the file is 0600; this keeps it 0600 and writes a timestamped backup first.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".config" / "slack-claude" / "mcp.json"
COOKIE = "CF_Authorization"


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def jwt_expiry(token: str) -> int | None:
    """`exp` from the unverified payload; display only."""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        return int(json.loads(base64.urlsafe_b64decode(part))["exp"])
    except Exception:  # noqa: BLE001
        return None


def login(cloudflared: str, url: str) -> str:
    # Inherit stdio: cloudflared prints the URL to open, and waits for the browser.
    subprocess.run([cloudflared, "access", "login", url], check=True)
    result = subprocess.run(
        [cloudflared, "access", "token", f"-app={url}"],
        check=True, capture_output=True, text=True,
    )
    token = result.stdout.strip()
    if token.count(".") != 2:
        die(f"cloudflared returned something that is not a JWT for {url}")
    return token


def write_config(path: Path, updates: dict[str, str]) -> None:
    document = json.loads(path.read_text())
    for name, token in updates.items():
        credential = document["servers"][name].setdefault("credential", {"mode": "shared"})
        headers = credential.setdefault("headers", {})
        headers["Cookie"] = f"{COOKIE}={token}"
    backup = path.with_name(f"{path.name}.{time.strftime('%Y%m%d%H%M%S')}.bak")
    shutil.copy2(path, backup)
    tmp = path.with_name(path.name + ".new")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(document, handle, indent=2)
        handle.write("\n")
    os.replace(tmp, path)
    print(f"wrote {path} (0600), backup {backup.name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("servers", nargs="+", help="server name(s) as in mcp.json")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()

    cloudflared = shutil.which("cloudflared")
    if not cloudflared:
        die("cloudflared is not installed:  sudo pacman -S cloudflared")

    document = json.loads(args.config.read_text())
    servers = document.get("servers", {})
    updates: dict[str, str] = {}
    for name in args.servers:
        entry = servers.get(name)
        if not entry or entry.get("type") != "http":
            die(f"{name!r} is not an http server in {args.config}")
        url = entry["url"]
        # Access is per-app: the app is the host, not the /mcp path.
        host = urllib.parse.urlsplit(url).netloc
        print(f"\n== {name} ({host}) -- open the URL below in any browser and finish login")
        token = login(cloudflared, f"https://{host}")
        exp = jwt_expiry(token)
        print(f"{name}: got a session, expires "
              f"{time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(exp)) if exp else 'unknown'}")
        updates[name] = token
        # Written per server, not at the end, so a failed second login keeps the first.
        write_config(args.config, {name: token})


if __name__ == "__main__":
    main()
