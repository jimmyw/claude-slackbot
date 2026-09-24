#!/usr/bin/env python3
"""Obtain an OAuth grant for an MCP upstream, on the host, and seed the config.

Why this exists
---------------
`mcpproxy.OAuth` can *refresh* a grant but it cannot *create* one. Both grants in
the config so far were lifted out of the guest's Claude Code by
`60-migrate-mcp.sh`, so when an upstream was rebuilt to speak OAuth, or when an
authorisation server revoked a grant outright (linear, 2026-09-19: every refresh
answered `invalid_grant`), there was no path back other than hand-editing the
config file. This is that path.

It speaks the same OAuth 2.1 profile the MCP spec requires and Claude Code uses:
protected-resource discovery (RFC 9728), authorization-server metadata (RFC 8414),
dynamic client registration (RFC 7591) for a public client, authorization code with
PKCE S256, and the `resource` indicator (RFC 8707) on both legs.

Two stale-state traps it clears, because both are silent
-------------------------------------------------------
* **The database shadows the file.** `OAuth._access_token` reads `mcp_tokens`
  first and only falls back to the config, so a freshly authorised grant written
  to the file is *ignored* while a dead row survives — the symptom is that the
  re-authorisation appears to have done nothing at all. The row is deleted.
* **A `|mcp login` header override outlives the credential it replaced.**
  `McpProxy._default_upstream` merges the override on top of the config's headers
  and sends it alongside the bearer. A server that has moved to OAuth has no use
  for last month's Cookie, and leaving it there keeps dead credential material in
  sqlite. It is deleted too, and `headers` is dropped from the credential.

Usage
-----
    ./authorise-mcp-oauth.py syslog
    ./authorise-mcp-oauth.py linear --user U012ABCDEF

The browser leg is interactive. If the browser is not on this host, either
forward the port (`ssh -L 45678:127.0.0.1:45678 terra`) or paste the URL the
browser was redirected to back into this script — it accepts whichever arrives
first.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_CONFIG = Path.home() / ".config" / "slack-claude" / "mcp.json"
DEFAULT_DB = Path.home() / ".local" / "share" / "slack-claude" / "state.sqlite3"
DEFAULT_PORT = 45678
CLIENT_NAME = "slack-claude daemon (terra)"
TIMEOUT = 30


def die(message: str) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def fetch_json(url: str, data: dict | None = None, *, method: str = "") -> dict:
    """GET, or POST form-encoded when `data` is given. Returns the parsed body.

    Token endpoints report failures as a 400 with a JSON body naming the reason,
    which is far more useful than the status alone — `invalid_grant` versus
    `invalid_client` is the difference between re-authorising and fixing the
    registration — so the body is surfaced either way.
    """
    body = None
    headers = {"Accept": "application/json"}
    if data is not None:
        if method == "json":
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        else:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            return json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:400]
        raise RuntimeError(f"{method or 'GET'} {url} -> HTTP {exc.code}: {detail}")


# -- discovery ---------------------------------------------------------------


def discover(resource_url: str) -> tuple[dict, str, list[str]]:
    """(authorization-server metadata, canonical resource URI, resource scopes).

    The well-known path for a resource at `/mcp` is
    `/.well-known/oauth-protected-resource/mcp`, not the bare one — RFC 9728
    inserts the path. Both are tried because implementations differ on it, and
    getting it wrong looks like a server with no OAuth at all.

    The *resource's* scope list is returned separately from the authorisation
    server's, and is the one to ask for. Linear's AS advertises
    `read write openid email` while the resource itself needs only `read write`;
    asking for the AS's full list would put an identity scope on a bot's grant
    for no reason.
    """
    parts = urllib.parse.urlsplit(resource_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    path = parts.path.rstrip("/")

    metadata = {}
    for candidate in (
        f"{origin}/.well-known/oauth-protected-resource{path}",
        f"{origin}/.well-known/oauth-protected-resource",
    ):
        try:
            metadata = fetch_json(candidate)
            break
        except RuntimeError:
            continue

    resource = metadata.get("resource") or resource_url
    resource_scopes = list(metadata.get("scopes_supported") or [])
    issuers = metadata.get("authorization_servers") or [origin]
    issuer = issuers[0]

    issuer_parts = urllib.parse.urlsplit(issuer)
    issuer_origin = f"{issuer_parts.scheme}://{issuer_parts.netloc}"
    issuer_path = issuer_parts.path.rstrip("/")
    for candidate in (
        f"{issuer_origin}/.well-known/oauth-authorization-server{issuer_path}",
        f"{issuer_origin}/.well-known/oauth-authorization-server",
        f"{issuer}/.well-known/openid-configuration",
    ):
        try:
            server_metadata = fetch_json(candidate)
        except RuntimeError:
            continue
        if server_metadata.get("authorization_endpoint"):
            return server_metadata, resource, resource_scopes

    die(f"no authorization-server metadata found for {issuer}")
    raise AssertionError("unreachable")


def register(server_metadata: dict, redirect_uri: str) -> dict:
    """Dynamic client registration for a public client.

    `token_endpoint_auth_method: none` is the point: a daemon that refreshes
    unattended should hold no client secret it would have to store beside the
    refresh token.
    """
    endpoint = server_metadata.get("registration_endpoint")
    if not endpoint:
        die(
            "this server has no registration_endpoint; register a client by hand "
            "and pass --client-id"
        )
    return fetch_json(
        endpoint,
        {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_uri],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        },
        method="json",
    )


# -- the browser leg ---------------------------------------------------------


class _Catcher(http.server.BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self) -> None:  # noqa: N802 - stdlib naming
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        if "code" in query or "error" in query:
            _Catcher.result = {k: v[0] for k, v in query.items()}
            message = b"Authorised. You can close this tab and return to the terminal."
        else:
            message = b"Waiting for the authorisation redirect."
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(message)))
        self.end_headers()
        self.wfile.write(message)

    def log_message(self, *args: object) -> None:
        """Silence the default stderr access log: the query string carries the
        authorization code, and a code in a scrollback is a code on the host."""


def await_redirect(port: int, timeout: int) -> dict:
    """Whichever arrives first: the loopback callback, or a pasted redirect URL.

    Both paths exist because the browser is usually not on the machine the daemon
    runs on, and telling someone to set up a port forward before they can fix a
    logged-out server is how a fix goes unused.
    """
    httpd = http.server.HTTPServer(("127.0.0.1", port), _Catcher)
    httpd.timeout = 1
    _Catcher.result = {}

    pasted: dict = {}

    def read_stdin() -> None:
        try:
            line = sys.stdin.readline().strip()
        except Exception:
            return
        if line:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(line).query)
            pasted.update({k: v[0] for k, v in query.items()})

    reader = threading.Thread(target=read_stdin, daemon=True)
    reader.start()

    deadline = time.time() + timeout
    while time.time() < deadline:
        if _Catcher.result:
            httpd.server_close()
            return _Catcher.result
        if pasted:
            httpd.server_close()
            return pasted
        httpd.handle_request()

    httpd.server_close()
    die("timed out waiting for the authorisation redirect")
    raise AssertionError("unreachable")


# -- stale state -------------------------------------------------------------


def clear_stale_rows(db_path: Path, server: str, owner: str) -> list[str]:
    """Delete the rows that would otherwise shadow the new grant."""
    cleared = []
    if not db_path.exists():
        return [f"{db_path} does not exist; nothing to clear"]
    connection = sqlite3.connect(db_path)
    try:
        with connection:
            for table, label in (
                ("mcp_tokens", "token"),
                ("mcp_header_overrides", "header override"),
            ):
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                ).fetchone()
                if not exists:
                    continue
                cursor = connection.execute(
                    f"DELETE FROM {table} WHERE server=? AND slack_user=?",
                    (server, owner),
                )
                if cursor.rowcount > 0:
                    cleared.append(
                        f"deleted the stale {label} row for "
                        f"{server}/{owner or 'shared'}"
                    )
    finally:
        connection.close()
    return cleared


def write_config(path: Path, server: str, owner: str, grant: dict) -> None:
    """Merge the grant into the config, atomically, 0600, preserving the rest."""
    document = json.loads(path.read_text())
    entry = document.get("servers", {}).get(server)
    if entry is None:
        die(f"{path} has no server called {server!r}")

    credential = entry.setdefault("credential", {})
    if owner:
        target = credential.setdefault("users", {}).setdefault(owner, {})
    else:
        target = credential

    target["oauth"] = grant
    # A server that speaks OAuth has no use for the header it used to authenticate
    # with, and leaving it in place keeps a dead credential in the file and sends
    # it on every call.
    dropped = target.pop("headers", None)
    if dropped:
        print("  dropped the now-unused `headers` from the credential")

    temporary = path.with_suffix(".json.new")
    temporary.write_text(json.dumps(document, indent=2) + "\n")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


# -- main --------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("server", help="the server name as it appears in mcp.json")
    parser.add_argument(
        "--user", default="",
        help="a Slack user id for a per-user grant (default: the shared one)",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument(
        "--client-id", default="",
        help="skip dynamic registration and use this client id",
    )
    parser.add_argument(
        "--register", action="store_true",
        help="always register a fresh client, ignoring the configured client_id",
    )
    parser.add_argument(
        "--url", default="",
        help="the resource URL, when the server is not in the config file yet",
    )
    parser.add_argument(
        "--scope", default="",
        help="space-separated scopes to request (default: keep the existing "
             "grant's, else what the resource metadata asks for)",
    )
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument(
        "--keep-stale", action="store_true",
        help="do NOT delete the shadowing token / header-override rows",
    )
    args = parser.parse_args()

    if not args.config.exists():
        die(f"{args.config} does not exist")

    document = json.loads(args.config.read_text())
    entry = document.get("servers", {}).get(args.server)
    if entry is None and not args.url:
        available = ", ".join(sorted(document.get("servers", {})))
        die(f"no server called {args.server!r} in {args.config} (have: {available})")

    resource_url = args.url or (entry or {}).get("url", "")
    if not resource_url:
        die(f"{args.server} has no url; pass --url")

    print(f"discovering OAuth metadata for {resource_url}")
    server_metadata, resource, resource_scopes = discover(resource_url)
    print(f"  issuer:        {server_metadata.get('issuer')}")
    print(f"  authorize:     {server_metadata['authorization_endpoint']}")
    print(f"  token:         {server_metadata['token_endpoint']}")
    print(f"  resource:      {resource}")

    methods = server_metadata.get("code_challenge_methods_supported") or []
    if methods and "S256" not in methods:
        die(f"this server does not support PKCE S256 (offers {methods})")

    redirect_uri = f"http://127.0.0.1:{args.port}/callback"

    client_id = args.client_id
    client_secret = ""
    if not client_id and not args.register:
        existing = ((entry or {}).get("credential", {}).get("oauth") or {})
        candidate = existing.get("client_id", "")
        # A client_id that is a URL is a client-ID metadata document (RFC 7591bis),
        # and linear's is Claude Code's: `https://claude.ai/oauth/...`. That document
        # lists ITS redirect URIs, not ours, so /authorize would refuse our loopback.
        # Reuse only an opaque id, which is one we registered.
        if candidate.startswith(("http://", "https://")):
            print(
                f"  not reusing {candidate} — that is another client's metadata\n"
                "  document, so our redirect URI is not in it; registering our own"
            )
        elif candidate:
            client_id = candidate
            print(f"  reusing the configured client_id ({client_id[:24]}…)")
    if not client_id:
        print("registering a client dynamically")
        registration = register(server_metadata, redirect_uri)
        client_id = registration.get("client_id", "")
        client_secret = registration.get("client_secret", "")
        if not client_id:
            die(f"registration returned no client_id: {str(registration)[:200]}")
        print(f"  client_id:     {client_id}")

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).decode().rstrip("=")
    challenge = (
        base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    state = secrets.token_urlsafe(24)

    # Narrowest defensible set, in order: what was asked for, what the grant
    # already had (re-authorising should not silently widen it), what the resource
    # says it needs, and only then the authorisation server's full menu.
    existing_scope = ((entry or {}).get("credential", {}).get("oauth") or {}).get(
        "scope", ""
    )
    if args.scope:
        scope = args.scope
        source = "--scope"
    elif existing_scope:
        scope = existing_scope
        source = "the existing grant"
    elif resource_scopes:
        scope = " ".join(resource_scopes)
        source = "the resource metadata"
    else:
        scope = " ".join(server_metadata.get("scopes_supported") or [])
        source = "the authorisation server"
    if scope:
        print(f"  scope:         {scope}  (from {source})")

    parameters = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "resource": resource,
    }
    if scope:
        parameters["scope"] = scope
    authorize_url = (
        server_metadata["authorization_endpoint"]
        + "?"
        + urllib.parse.urlencode(parameters)
    )

    print("\nOpen this in a browser and approve:\n")
    print(f"  {authorize_url}\n")
    print(
        f"Listening on {redirect_uri} for up to {args.timeout}s.\n"
        "If the browser is on another machine, forward the port\n"
        f"  ssh -L {args.port}:127.0.0.1:{args.port} terra\n"
        "or paste the full URL the browser was redirected to here and press enter."
    )

    redirect = await_redirect(args.port, args.timeout)
    if redirect.get("error"):
        die(
            f"the authorisation server refused: {redirect['error']} "
            f"{redirect.get('error_description', '')}"
        )
    if redirect.get("state") and redirect["state"] != state:
        die("state mismatch; refusing the response")
    # RFC 9207. When the server says it returns `iss`, a response without it — or
    # with someone else's — is the mix-up attack the parameter exists to catch.
    issuer = server_metadata.get("issuer", "")
    if server_metadata.get("authorization_response_iss_parameter_supported") and issuer:
        if redirect.get("iss") != issuer:
            die(
                f"issuer mismatch: the redirect carried {redirect.get('iss')!r}, "
                f"expected {issuer!r}"
            )
    code = redirect.get("code")
    if not code:
        die(f"no code in the redirect: {redirect}")

    print("\nexchanging the code for tokens")
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
        "resource": resource,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    try:
        tokens = fetch_json(server_metadata["token_endpoint"], payload)
    except RuntimeError as exc:
        die(str(exc))
        raise AssertionError("unreachable")

    access = tokens.get("access_token", "")
    refresh = tokens.get("refresh_token", "")
    if not access:
        die(f"no access_token in the response: {str(tokens)[:200]}")
    if not refresh:
        print(
            "  WARNING: no refresh_token was issued. The daemon can only refresh a\n"
            "  grant it can re-mint, so this credential will die when the access\n"
            "  token expires and will need authorising again by hand."
        )

    expires_in = int(tokens.get("expires_in") or 0)
    grant = {
        "token_url": server_metadata["token_endpoint"],
        "client_id": client_id,
        "refresh_token": refresh,
        "access_token": access,
        "expires_at": int(time.time()) + expires_in if expires_in else 0,
        "scope": tokens.get("scope", scope),
        # Sent on refresh too: an authorisation server that binds tokens to an
        # audience can otherwise hand back a token for the wrong one, which reads
        # downstream as an upstream that rejects a credential it just issued.
        "resource": resource,
    }
    if client_secret:
        grant["client_secret"] = client_secret

    print(f"  access token:  {len(access)} chars, expires_in={expires_in or 'unset'}")
    print(f"  refresh token: {len(refresh) if refresh else 'NONE'}")

    write_config(args.config, args.server, args.user, grant)
    print(f"  wrote the grant to {args.config}")

    if args.keep_stale:
        print("  left the existing token / header-override rows in place (--keep-stale)")
    else:
        for line in clear_stale_rows(args.db, args.server, args.user):
            print(f"  {line}")

    print(
        "\nDone. The registry hot-reloads by mtime, so the next Slack message picks\n"
        f"this up — no restart. Verify with `|mcp tools {args.server}`."
    )


if __name__ == "__main__":
    main()
