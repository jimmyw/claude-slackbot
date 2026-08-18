"""MCP server definitions, credentials and per-user policy.

The whole point of the proxy is that this file's contents never enter the VM, so
everything here runs on the host: which upstreams exist, which credential to present
for a given Slack user, and which tools that user may call.

No I/O beyond reading the config file, and no Slack — so the resolution rules, which
are the part that decides what a compromised guest can reach, are unit-testable on
their own.

Two rules govern policy, and they are deliberately boring:

  * **Deny always wins.** Denies from every level are unioned; one match refuses.
  * **The most specific CONFIGURED allow level wins.** A user with any `allow` of
    their own in the file uses it *instead of* the server's, so a per-user list can
    narrow as well as widen. Without that, a server-wide allow could never be
    tightened for one person.
  * **Runtime rules from `|mcp` are additive**, never a replacement. `|mcp allow x y`
    means "y as well", so adding one tool cannot silently revoke the rest — which is
    exactly what happened when runtime allows were treated as a per-user list.

Anything not matched is denied. A tool the operator has not thought about is not a
tool the agent may call.
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

STDIO = "stdio"
HTTP = "http"
TRANSPORTS = (STDIO, HTTP)

MODE_SHARED = "shared"
MODE_PER_USER = "per_user"

# Defaults for the two caps. Per run, because a run is one Slack message: a turn that
# wants more than this is either looping or exfiltrating.
DEFAULT_MAX_CALLS_PER_RUN = 40
DEFAULT_MAX_RESULT_BYTES = 256 * 1024

_SERVER_KEYS = {
    "type", "url", "command", "args", "credential", "tools",
    "max_calls_per_run", "max_result_bytes", "description",
}
_CREDENTIAL_KEYS = {"mode", "headers", "env", "oauth", "users", "shared_fallback"}
_TOOLS_KEYS = {"allow", "deny", "users"}
_USER_CREDENTIAL_KEYS = {"headers", "env", "oauth"}


class McpConfigError(RuntimeError):
    """The config file is unusable. Kept separate so callers can keep the last good
    copy rather than losing every MCP server to one typo."""


@dataclass(frozen=True)
class Credential:
    """What to present to an upstream, for one specific caller."""

    headers: dict[str, str] = field(default_factory=dict)
    env: dict[str, str] = field(default_factory=dict)
    oauth: dict[str, Any] | None = None
    # Who this credential belongs to: "" for the shared one, else a Slack user id.
    # Carried so the OAuth token store can key rotated grants per person.
    owner: str = ""


@dataclass(frozen=True)
class Server:
    name: str
    type: str
    url: str = ""
    command: str = ""
    args: tuple[str, ...] = ()
    description: str = ""
    mode: str = MODE_SHARED
    shared_fallback: bool = False
    shared: Credential = field(default_factory=Credential)
    per_user: dict[str, Credential] = field(default_factory=dict)
    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()
    user_allow: dict[str, tuple[str, ...]] = field(default_factory=dict)
    user_deny: dict[str, tuple[str, ...]] = field(default_factory=dict)
    max_calls_per_run: int = DEFAULT_MAX_CALLS_PER_RUN
    max_result_bytes: int = DEFAULT_MAX_RESULT_BYTES

    def credential_for(self, slack_user: str) -> Credential | None:
        """The credential to present for this caller, or None if they have none.

        A per-user server with no entry for the caller returns None, and the caller
        is then not offered the server at all. There is deliberately no implicit
        fallback to the shared credential: falling back would quietly let a guest act
        as the operator upstream, which is the opposite of what per-user is for.
        `shared_fallback` opts into it explicitly, per server.
        """
        if self.mode == MODE_SHARED:
            return self.shared
        found = self.per_user.get(slack_user)
        if found is not None:
            return found
        return self.shared if self.shared_fallback else None


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str


def matches(patterns: tuple[str, ...], tool: str) -> str | None:
    """The first pattern that matches, or None. Case-sensitive globs."""
    for pattern in patterns:
        if fnmatch.fnmatchcase(tool, pattern):
            return pattern
    return None


def decide(
    server: Server,
    slack_user: str,
    tool: str,
    *,
    extra_allow: tuple[str, ...] = (),
    extra_deny: tuple[str, ...] = (),
) -> Decision:
    """May `slack_user` call `tool` on `server`?

    `extra_allow` / `extra_deny` are the runtime patterns from `|mcp`, already
    narrowed to this server and caller by the caller of this function.
    """
    user_deny = server.user_deny.get(slack_user, ())
    denies = tuple(server.deny) + tuple(user_deny) + tuple(extra_deny)
    hit = matches(denies, tool)
    if hit is not None:
        return Decision(False, f"denied by pattern {hit!r}")

    # The most specific CONFIGURED level wins, so a per-user list in the file can be
    # narrower than the server's rather than only wider. Runtime patterns are then
    # added to whichever of those applies: `|mcp allow` means "this as well", and a
    # test caught it revoking everything else when it was treated as a per-user list.
    configured = server.user_allow.get(slack_user) or server.allow
    allows = tuple(configured) + tuple(extra_allow)
    hit = matches(allows, tool)
    if hit is not None:
        return Decision(True, f"allowed by pattern {hit!r}")

    return Decision(False, "not on the allowlist")


def _as_str_map(value: Any, where: str) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in value.items()
    ):
        raise McpConfigError(f"{where} must be an object of strings")
    return dict(value)


def _as_patterns(value: Any, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise McpConfigError(f"{where} must be a list of strings")
    return tuple(value)


def _unknown(keys: Any, allowed: set[str], where: str) -> None:
    """Reject unknown keys loudly.

    A typo in `allow` is not a harmless no-op: it silently means "no policy", i.e. a
    server whose tools are all denied, or a credential that is not presented. Better
    to refuse the file than to run a policy nobody wrote.
    """
    if not isinstance(keys, dict):
        raise McpConfigError(f"{where} must be an object")
    strange = sorted(set(keys) - allowed)
    if strange:
        raise McpConfigError(
            f"{where} has unknown key(s) {', '.join(strange)}; "
            f"allowed: {', '.join(sorted(allowed))}"
        )


def _credential(raw: Any, where: str, owner: str) -> Credential:
    _unknown(raw, _USER_CREDENTIAL_KEYS, where)
    oauth = raw.get("oauth")
    if oauth is not None and not isinstance(oauth, dict):
        raise McpConfigError(f"{where}.oauth must be an object")
    return Credential(
        headers=_as_str_map(raw.get("headers"), f"{where}.headers"),
        env=_as_str_map(raw.get("env"), f"{where}.env"),
        oauth=dict(oauth) if oauth else None,
        owner=owner,
    )


def parse_server(name: str, raw: Any) -> Server:
    """Validate and parse one server definition."""
    _unknown(raw, _SERVER_KEYS, f"server {name!r}")

    transport = raw.get("type")
    if transport not in TRANSPORTS:
        raise McpConfigError(
            f"server {name!r} has type {transport!r}; expected one of "
            f"{', '.join(TRANSPORTS)}"
        )
    if transport == HTTP and not isinstance(raw.get("url"), str):
        raise McpConfigError(f"server {name!r} is http and needs a url")
    if transport == STDIO and not isinstance(raw.get("command"), str):
        raise McpConfigError(f"server {name!r} is stdio and needs a command")

    args = raw.get("args") or []
    if not isinstance(args, list) or any(not isinstance(a, str) for a in args):
        raise McpConfigError(f"server {name!r}: args must be a list of strings")

    cred_raw = raw.get("credential") or {"mode": MODE_SHARED}
    _unknown(cred_raw, _CREDENTIAL_KEYS, f"server {name!r}.credential")
    mode = cred_raw.get("mode", MODE_SHARED)
    if mode not in (MODE_SHARED, MODE_PER_USER):
        raise McpConfigError(
            f"server {name!r}.credential.mode is {mode!r}; expected "
            f"{MODE_SHARED} or {MODE_PER_USER}"
        )

    shared = _credential(
        {k: v for k, v in cred_raw.items() if k in _USER_CREDENTIAL_KEYS},
        f"server {name!r}.credential",
        owner="",
    )
    per_user: dict[str, Credential] = {}
    for user, user_raw in (cred_raw.get("users") or {}).items():
        if not isinstance(user, str):
            raise McpConfigError(f"server {name!r}.credential.users key must be a id")
        per_user[user] = _credential(
            user_raw, f"server {name!r}.credential.users[{user}]", owner=user
        )
    if mode == MODE_PER_USER and not per_user and not cred_raw.get("shared_fallback"):
        raise McpConfigError(
            f"server {name!r} is per_user but lists no users, so nobody could ever "
            "use it; add users or set shared_fallback"
        )

    tools_raw = raw.get("tools") or {}
    _unknown(tools_raw, _TOOLS_KEYS, f"server {name!r}.tools")
    user_allow: dict[str, tuple[str, ...]] = {}
    user_deny: dict[str, tuple[str, ...]] = {}
    for user, user_tools in (tools_raw.get("users") or {}).items():
        _unknown(user_tools, {"allow", "deny"}, f"server {name!r}.tools.users[{user}]")
        user_allow[user] = _as_patterns(
            user_tools.get("allow"), f"server {name!r}.tools.users[{user}].allow"
        )
        user_deny[user] = _as_patterns(
            user_tools.get("deny"), f"server {name!r}.tools.users[{user}].deny"
        )

    return Server(
        name=name,
        type=transport,
        url=raw.get("url") or "",
        command=raw.get("command") or "",
        args=tuple(args),
        description=raw.get("description") or "",
        mode=mode,
        shared_fallback=bool(cred_raw.get("shared_fallback")),
        shared=shared,
        per_user=per_user,
        allow=_as_patterns(tools_raw.get("allow"), f"server {name!r}.tools.allow"),
        deny=_as_patterns(tools_raw.get("deny"), f"server {name!r}.tools.deny"),
        user_allow=user_allow,
        user_deny=user_deny,
        max_calls_per_run=int(
            raw.get("max_calls_per_run") or DEFAULT_MAX_CALLS_PER_RUN
        ),
        max_result_bytes=int(raw.get("max_result_bytes") or DEFAULT_MAX_RESULT_BYTES),
    )


def parse(document: Any) -> dict[str, Server]:
    _unknown(document, {"servers"}, "config")
    servers_raw = document.get("servers")
    if not isinstance(servers_raw, dict):
        raise McpConfigError("config needs a servers object")
    return {name: parse_server(name, raw) for name, raw in servers_raw.items()}


class Registry:
    """The config file, re-read when it changes.

    Hot-reloaded by mtime and read per run, the same way |auth reads the Bash policy
    per run: adding a server is an edit plus the next Slack message, never a restart.

    A broken file keeps the last good copy and logs loudly. That cannot grant anything
    new — parsing is the only way a permission appears — and losing every server to one
    stray comma would be worse than running a slightly stale policy.
    """

    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._servers: dict[str, Server] = {}
        self._mtime: float | None = None
        self._error: str = ""
        self._loaded_once = False

    @property
    def path(self) -> Path | None:
        return self._path

    @property
    def error(self) -> str:
        """Why the current copy may be stale, for |mcp to show."""
        return self._error

    def servers(self) -> dict[str, Server]:
        self._refresh()
        return dict(self._servers)

    def get(self, name: str) -> Server | None:
        return self.servers().get(name)

    def _refresh(self) -> None:
        if self._path is None:
            return
        try:
            info = self._path.stat()
        except FileNotFoundError:
            if self._servers or not self._loaded_once:
                self._error = f"{self._path} does not exist"
                log.warning("mcp config %s does not exist; no servers", self._path)
            self._servers = {}
            self._mtime = None
            self._loaded_once = True
            return

        if self._mtime is not None and info.st_mtime == self._mtime:
            return

        # It holds upstream credentials, so its mode is not a detail. Refuse rather
        # than warn: a token readable by anyone on this host is the situation this
        # whole feature exists to end.
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            self._servers = {}
            self._mtime = info.st_mtime
            self._error = (
                f"{self._path} is group/other accessible "
                f"({stat.filemode(info.st_mode)}); chmod 600 it"
            )
            log.error("refusing to load mcp config: %s", self._error)
            return

        try:
            document = json.loads(self._path.read_text())
            servers = parse(document)
        except (McpConfigError, json.JSONDecodeError, OSError) as exc:
            self._error = f"{self._path}: {exc}"
            log.error(
                "mcp config is unusable, keeping the previous copy (%d server(s)): %s",
                len(self._servers), exc,
            )
            self._mtime = info.st_mtime
            return

        self._servers = servers
        self._mtime = info.st_mtime
        self._loaded_once = True
        self._error = ""
        log.info(
            "loaded mcp config: %s", ", ".join(sorted(servers)) or "no servers"
        )


def available_for(
    registry: Registry, slack_user: str, disabled: set[str]
) -> dict[str, Server]:
    """The servers this caller may actually use this run.

    A server is offered only when it is enabled AND the caller has a credential for
    it. Omitting it is better than offering tools whose every call fails: the agent
    sees what it can do now, and nothing else.
    """
    out: dict[str, Server] = {}
    for name, server in registry.servers().items():
        if name in disabled:
            continue
        if server.credential_for(slack_user) is None:
            continue
        out[name] = server
    return out


def config_path_from_env(value: str | None) -> Path | None:
    if not value:
        return None
    return Path(os.path.expanduser(value))
