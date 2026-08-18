#!/usr/bin/env bash
# Move the guest's MCP servers -- and their credentials -- onto the host.
#
#   ./bootstrap/60-migrate-mcp.sh <vm-ip>                 write the host config
#   ./bootstrap/60-migrate-mcp.sh <vm-ip> --remove-guest  then strip the guest's copies
#
# Servers already in the host config are LEFT ALONE; only new ones are added. A migrated
# entry nearly always needs finishing by hand -- a stdio command repointed at a host
# clone, an oauth token_url the guest's discovery blob did not carry, a rotated
# credential -- and a second run overwriting that is exactly how a working setup breaks
# (it happened during the first migration). OVERWRITE=1 regenerates from the guest.
#
# Why: an MCP server configured inside the VM carries its credential there, and that is
# outside both the VM boundary and the approval gate. One approved Bash call is
# arbitrary code as `agent`, and from there the credential can be read and the upstream
# called directly -- no hook, no buttons, no audit trail. On the host, the guest gets a
# socket that exists only for the lifetime of a run, and only the tools policy allows.
#
# The allowlist is derived from the standing grants already in the daemon's database, so
# the migration preserves exactly the tools you have already approved -- nothing more.
#
# Secrets are never printed. Values are read in the guest and written straight into a
# 0600 file on the host; only key names and counts appear on stdout.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VM_HOST="${1:-}"
REMOVE_GUEST="${2:-}"
ADMIN_KEY="${ADMIN_KEY:-$HOME/.ssh/agent_vm_admin_ed25519}"
MCP_CONFIG="${MCP_CONFIG:-$HOME/.config/slack-claude/mcp.json}"
DB_PATH="${DB_PATH:-$HOME/.local/share/slack-claude/state.sqlite3}"

if [[ -z "$VM_HOST" ]]; then
    echo "usage: $0 <vm-ip> [--remove-guest]" >&2
    exit 64
fi
if [[ ! -f "$ADMIN_KEY" ]]; then
    echo "ERROR: no admin key at $ADMIN_KEY" >&2
    exit 1
fi

SSH_OPTS=(-i "$ADMIN_KEY" -o IdentitiesOnly=yes -o BatchMode=yes
          -o StrictHostKeyChecking=accept-new)

echo "==> Reading the guest's MCP configuration"
GUEST_JSON="$(ssh "${SSH_OPTS[@]}" "admin@$VM_HOST" \
    'sudo cat /home/agent/.claude.json 2>/dev/null || echo "{}"' </dev/null)"
GUEST_OAUTH="$(ssh "${SSH_OPTS[@]}" "admin@$VM_HOST" \
    'sudo cat /home/agent/.claude/.credentials.json 2>/dev/null || echo "{}"' </dev/null)"

if [[ -z "$GUEST_JSON" ]]; then
    echo "ERROR: could not read the guest's config" >&2
    exit 1
fi

echo "==> Deriving the allowlist from the grants you have already approved"
GRANTS="$(sqlite3 "file:${DB_PATH}?mode=ro" \
    "select tool_name from grants where tool_name like 'mcp\\_\\_%' escape '\\';" \
    2>/dev/null || true)"

install -d -m 0700 "$(dirname "$MCP_CONFIG")"
if [[ -f "$MCP_CONFIG" ]]; then
    backup="${MCP_CONFIG}.$(date +%Y%m%d%H%M%S).bak"
    cp -p "$MCP_CONFIG" "$backup"
    echo "    kept a backup at $backup"
fi

umask 077
GUEST_JSON="$GUEST_JSON" GUEST_OAUTH="$GUEST_OAUTH" GRANTS="$GRANTS" \
    OUT="$MCP_CONFIG" python3 - <<'PY'
import json
import os
import sys

guest = json.loads(os.environ["GUEST_JSON"] or "{}")
try:
    with open(os.environ["OUT"]) as handle:
        existing = (json.load(handle) or {}).get("servers") or {}
except (OSError, ValueError):
    existing = {}
overwrite = os.environ.get("OVERWRITE") == "1"
oauth_store = json.loads(os.environ["GUEST_OAUTH"] or "{}")
grants = [g.strip() for g in os.environ["GRANTS"].splitlines() if g.strip()]
out = os.environ["OUT"]

# Tools the operator has already approved, per server: mcp__<server>__<tool>.
allowed: dict[str, set[str]] = {}
for name in grants:
    parts = name.split("__", 2)
    if len(parts) == 3:
        allowed.setdefault(parts[1], set()).add(parts[2])

# The guest lists servers globally and again per project; the global scope wins and the
# rest are duplicates of it.
sources = [guest.get("mcpServers") or {}]
for project in (guest.get("projects") or {}).values():
    sources.append(project.get("mcpServers") or {})

# OAuth grants are keyed "<server>|<hash>" in the guest's credential store.
oauth_by_server = {}
for key, grant in (oauth_store.get("mcpOAuth") or {}).items():
    oauth_by_server[key.split("|", 1)[0]] = grant

servers: dict[str, dict] = {}
for source in sources:
    for name, cfg in source.items():
        if name in servers:
            continue
        if name in existing and not overwrite:
            # Keep what is already here: it has been finished by hand, and the
            # credential may since have been rotated.
            servers[name] = existing[name]
            print(f"      {name}: kept the existing host entry, unchanged")
            continue
        kind = cfg.get("type") or ("stdio" if cfg.get("command") else "http")
        entry: dict = {"type": kind}
        credential: dict = {"mode": "shared"}

        if kind == "stdio":
            entry["command"] = cfg["command"]
            entry["args"] = cfg.get("args") or []
            if cfg.get("env"):
                credential["env"] = cfg["env"]
        else:
            entry["url"] = cfg["url"]
            if cfg.get("headers"):
                credential["headers"] = cfg["headers"]
            grant = oauth_by_server.get(name)
            if grant:
                discovery = grant.get("discoveryState") or {}
                metadata = discovery.get("authorizationServerMetadata") or discovery
                credential["oauth"] = {
                    # token_url is what the proxy needs; the rest of the discovery
                    # blob is not carried over.
                    "token_url": metadata.get("token_endpoint", ""),
                    "client_id": grant.get("clientId", ""),
                    "refresh_token": grant.get("refreshToken", ""),
                    "access_token": grant.get("accessToken", ""),
                    "expires_at": int(grant.get("expiresAt") or 0) // 1000,
                    "scope": grant.get("scope", ""),
                }

        entry["credential"] = credential
        entry["tools"] = {"allow": sorted(allowed.get(name, []))}
        servers[name] = entry

# Anything already configured that the guest no longer mentions is kept as well --
# after --remove-guest the guest mentions nothing, and a later run must not empty the
# host config.
for name, entry in existing.items():
    servers.setdefault(name, entry)

document = {"servers": servers}
with open(out, "w") as handle:
    json.dump(document, handle, indent=2)
    handle.write("\n")
os.chmod(out, 0o600)

print(f"    wrote {out} with {len(servers)} server(s), mode 0600")
for name, entry in sorted(servers.items()):
    credential = entry["credential"]
    shapes = []
    if credential.get("headers"):
        shapes.append(f"headers({', '.join(sorted(credential['headers']))})")
    if credential.get("env"):
        shapes.append(f"env({', '.join(sorted(credential['env']))})")
    if credential.get("oauth"):
        oauth = credential["oauth"]
        shapes.append("oauth" + ("" if oauth["token_url"] else " (NO token_url!)"))
    tools = entry["tools"]["allow"]
    print(
        f"      {name}: {entry['type']}, "
        f"credential={'+'.join(shapes) or 'none'}, "
        f"allow={', '.join(tools) if tools else 'NOTHING YET'}"
    )
    if not tools:
        print(f"        ^ no grants existed for {name}; allow tools with |mcp allow")
    if credential.get("oauth") and not credential["oauth"]["token_url"]:
        print("        ^ fill in token_url by hand, or re-authorise on the host")
PY

echo
echo "==> Point the daemon at it"
echo "    MCP_CONFIG=$MCP_CONFIG   in daemon/.env, then restart the daemon."
echo "    Until that line exists, nothing changes: the guest keeps using its own MCP."

if [[ "$REMOVE_GUEST" == "--remove-guest" ]]; then
    echo
    echo "==> Removing the guest's copies"
    # Do this only once the host path is proven, since it takes the guest's MCP away.
    ssh "${SSH_OPTS[@]}" "admin@$VM_HOST" 'sudo python3 -' <<'PY' || true
import json

for path, key in (
    ("/home/agent/.claude.json", "mcpServers"),
    ("/home/agent/.claude/.credentials.json", "mcpOAuth"),
):
    try:
        with open(path) as handle:
            document = json.load(handle)
    except (OSError, ValueError) as exc:
        print(f"    {path}: {exc}")
        continue
    removed = document.pop(key, None) is not None
    for project in (document.get("projects") or {}).values():
        if isinstance(project, dict) and project.pop("mcpServers", None) is not None:
            removed = True
    if not removed:
        print(f"    {path}: nothing to remove")
        continue
    with open(path, "w") as handle:
        json.dump(document, handle, indent=2)
    print(f"    {path}: removed {key}")
PY
    # A stdio MCP server can also cache its own credential OUTSIDE .claude.json --
    # varys keeps an Okta session under ~/.config/tibber-varys, which is a fourth
    # credential the first pass of this migration did not know about. Copy it to the
    # host first (same path, since the daemon runs as its own user) or the server
    # cannot authenticate once it runs out here.
    ssh "${SSH_OPTS[@]}" "admin@$VM_HOST" \
        'sudo rm -rf /home/agent/.config/tibber-varys' </dev/null || true
    echo "    removed the guest's cached varys Okta session"
    echo "    Now rotate the upstream credentials: the guest's copies existed, so"
    echo "    treat them as exposed. Reissue the syslog cookie, re-authorise"
    echo "    esp-crash on the host, and roll the varys token."
    echo
    echo "==> Verifying"
    "$REPO_DIR/bootstrap/verify-guest.sh" "$VM_HOST" || true
fi
